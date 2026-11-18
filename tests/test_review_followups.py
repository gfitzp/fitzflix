"""Test the follow-up fixes of the review of #455, #456, #457, #460 and #463."""

import os
import time
from datetime import datetime

from app import db
from tests.factories import make_movie, make_movie_file
from tests.fakes import ArchiveS3


def test_a_cron_started_sync_holds_the_archive_move(app):
    """The scheduler queues the function object. rq names the job app.archive.sync (#456)."""

    from rq.registry import StartedJobRegistry

    from app.archive import moves
    from app.archive.sync import sync_aws_s3_storage_task

    with app.app_context():
        job = app.maintenance_queue.enqueue(sync_aws_s3_storage_task)
        assert job.func_name == "app.archive.sync.sync_aws_s3_storage_task"
        registry = StartedJobRegistry(queue=app.maintenance_queue)
        app.maintenance_queue.remove(job)
        # StartedJobRegistry.add raises NotImplementedError in rq 2. Thus,
        # the test writes the sorted set of the registry directly.
        app.redis.zadd(registry.key, {job.id: int(time.time()) + 300})
        try:
            assert moves._sync_is_running() is True
        finally:
            app.redis.zrem(registry.key, job.id)
            job.delete()
        assert moves._sync_is_running() is False


def _lost_upload_row(app, monkeypatch, tmp_path, title):
    from app.archive import aws_clients

    fake = ArchiveS3({})
    monkeypatch.setattr(aws_clients, "aws_s3_client", lambda **kw: fake)
    monkeypatch.setitem(app.config, "AWS_BUCKET", "test-bucket")
    monkeypatch.setitem(app.config, "LIBRARY_DIR", str(tmp_path))
    basename = f"{title} (2024) - [DVD].mkv"
    file = make_movie_file(make_movie(title, 2024), "DVD")
    file.untouched_basename = basename
    file.aws_key = f"pending-untouched/run9/{basename}"
    file.aws_key_target = f"untouched/{basename}"
    file.aws_date_uploaded = datetime(2026, 1, 1)
    db.session.commit()
    return fake, file.id, file.aws_key


def test_a_dead_library_volume_makes_the_lost_upload_move_wait(
    app, monkeypatch, tmp_path
):
    """The library file can come back. The move waits, and the target stays (follow-up #455)."""

    from app.archive import moves
    from app.models import File

    monkeypatch.setattr(
        "app.media.videos._dead_volumes", lambda paths: ["/Volumes/Movies"]
    )
    with app.app_context():
        fake, file_id, key = _lost_upload_row(app, monkeypatch, tmp_path, "Dead Vol")
        assert moves.move_archive_object(file_id) == moves.MOVE_WAITING
        db.session.expire_all()
        row = db.session.get(File, file_id)
        assert row.aws_key_target is not None
        assert fake.uploads == []
        assert not app.redis.hexists(moves.ARCHIVE_LOST_KEY, key)


def test_a_lost_upload_is_reported_once_as_needing_a_re_rip(app, monkeypatch, tmp_path):
    from types import SimpleNamespace

    from app.archive import moves
    from app.archive import sync

    sent = []
    monkeypatch.setattr(
        sync, "notify_admin_text", lambda subject, *a: sent.append(subject)
    )
    with app.app_context():
        fake, file_id, key = _lost_upload_row(app, monkeypatch, tmp_path, "Lost Rip")
        assert moves.move_archive_object(file_id) is False
        assert app.redis.hexists(moves.ARCHIVE_LOST_KEY, key)

        listing = SimpleNamespace(keys=set())
        sync._report_dangling_claims(listing)
        assert "Rip archives lost before their move" in sent
        sent.clear()
        sync._report_dangling_claims(listing)
        assert "Rip archives lost before their move" not in sent
        app.redis.delete(moves.ARCHIVE_LOST_KEY)


def test_a_resumed_download_renews_its_sidecar(app, monkeypatch, tmp_path):
    """The sweep reads the age of the sidecar. A 206 resume keeps it fresh (follow-up #457)."""

    from app.archive import cdn

    class Response:
        status_code = 206
        headers = {"Content-Length": "1"}

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def iter_content(self, chunk_size):
            yield b"c"

    destination = str(tmp_path / ".restore-untouched-Resume.mkv")
    with open(destination, "wb") as f:
        f.write(b"ab")
    with app.app_context():
        cdn._write_partial_etag(destination, "etag-1", 3)
    old = time.time() - 8 * 86400
    os.utime(destination + ".etag", (old, old))
    monkeypatch.setattr(cdn, "cdn_signed_url", lambda key: "https://cdn/x")
    monkeypatch.setattr(cdn, "CDN_HTTP_GET", lambda *a, **k: Response())
    with app.app_context():
        monkeypatch.setattr(cdn, "_remove_partial_etag", lambda d: None)
        cdn.cdn_download(
            "untouched/Resume.mkv", destination, lambda n: None, 3, "etag-1"
        )
    assert os.path.getmtime(destination + ".etag") > old + 86400


def test_the_sweep_keeps_a_complete_copy_whose_sidecar_stayed(app, monkeypatch):
    import app.operations.maintenance as maintenance
    from app.archive import cdn

    monkeypatch.setitem(app.config, "MAIL_SERVER", None)
    imports = app.config["IMPORT_DIR"]
    os.makedirs(imports, exist_ok=True)
    complete = os.path.join(imports, ".restore-untouched-Whole (2020).mkv")
    with open(complete, "wb") as f:
        f.write(b"abc")
    with app.app_context():
        cdn._write_partial_etag(complete, "etag-2", 3)
    old = time.time() - 30 * 86400
    for path in (complete, complete + ".etag"):
        os.utime(path, (old, old))
    try:
        maintenance.cleanup_orphaned_files()
        assert os.path.exists(complete)
        assert os.path.exists(complete + ".etag")
    finally:
        for path in (complete, complete + ".etag"):
            if os.path.exists(path):
                os.remove(path)


def test_a_catalog_exclude_removes_the_custom_poster_tree(app, monkeypatch, tmp_path):
    from app import cli as app_cli

    if "catalog" not in app.cli.commands:
        app_cli.register(app)
    monkeypatch.setitem(app.config, "CUSTOM_ARTWORK_DIR", str(tmp_path))
    with app.app_context():
        movie = make_movie("Excluded Poster Film", 1996, tmdb_id=463010)
        movie.custom_poster = "poster.jpg"
        db.session.commit()
        movie_id = movie.id
    tree = tmp_path / "movie" / str(movie_id) / "w185"
    tree.mkdir(parents=True)
    (tree / "poster.jpg").write_bytes(b"jpeg")

    result = app.test_cli_runner().invoke(args=["catalog", "exclude", str(movie_id)])
    assert result.exit_code == 0, result.output
    assert not (tmp_path / "movie" / str(movie_id)).exists()
