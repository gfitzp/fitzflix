"""Test that a series delete and a catalog exclude remove their archived originals (#464).

The foreign keys are ON DELETE SET NULL. The rows stayed with no owner,
kept claiming their S3 keys, and the objects were never pruned."""

from app import db
from tests.conftest import csrf_token_from
from tests.factories import make_movie, make_movie_file, make_tv_file, make_tv_series


def _original(**owner):
    from app.models import ArchivedOriginal

    name = f"Orphan Original {sorted(owner.items())} - [Bluray-2160p].mkv"
    original = ArchivedOriginal(
        media_library="TV Shows" if "series_id" in owner else "Movies",
        quality_title="Bluray-2160p",
        untouched_basename=name,
        aws_key=f"untouched/{name}",
        reason="archive only",
        **owner,
    )
    db.session.add(original)
    db.session.flush()
    return original.aws_key


def _deletes(app):
    return [
        job.args[0]
        for queue in (app.request_queue, app.handoff_queue, app.file_queue)
        for job in queue.jobs
        if job.func_name == "app.videos.aws_delete"
    ]


def test_a_series_delete_removes_its_archived_originals(app, admin_client):
    from app.models import ArchivedOriginal

    with app.app_context():
        series = make_tv_series("Original Delete Show", tmdb_id=464001)
        make_tv_file(series, 1, 1, "Bluray-1080p")
        db.session.flush()
        key = _original(series_id=series.id, season=1, episode=1)
        db.session.commit()
        series_id = series.id

    token = csrf_token_from(admin_client.get(f"/tv/{series_id}").get_data(as_text=True))
    response = admin_client.post(
        f"/tv/{series_id}/delete",
        data={"csrf_token": token, "delete_submit": "Delete"},
    )
    assert response.status_code == 302
    with app.app_context():
        assert ArchivedOriginal.query.filter_by(aws_key=key).count() == 0
    assert key in _deletes(app)


def test_a_catalog_exclude_removes_the_archived_originals(app):
    from app import cli as app_cli
    from app.models import ArchivedOriginal

    if "catalog" not in app.cli.commands:
        app_cli.register(app)
    with app.app_context():
        movie = make_movie("Original Exclude Film", 1999, tmdb_id=464002)
        key = _original(movie_id=movie.id)
        db.session.commit()
        movie_id = movie.id

    result = app.test_cli_runner().invoke(args=["catalog", "exclude", str(movie_id)])
    assert result.exit_code == 0, result.output
    with app.app_context():
        assert ArchivedOriginal.query.filter_by(aws_key=key).count() == 0
    assert key in _deletes(app)


def test_a_key_that_a_file_still_claims_is_not_deleted(app):
    """The claimed-key check runs after the commit, as for the file keys."""

    from app import cli as app_cli
    from tests.factories import make_movie_file

    if "catalog" not in app.cli.commands:
        app_cli.register(app)
    with app.app_context():
        movie = make_movie("Shared Key Film", 1998, tmdb_id=464003)
        key = _original(movie_id=movie.id)
        other = make_movie_file(make_movie("Claimant Film", 1997), "DVD")
        other.aws_key = key
        db.session.commit()
        movie_id = movie.id

    result = app.test_cli_runner().invoke(args=["catalog", "exclude", str(movie_id)])
    assert result.exit_code == 0, result.output
    assert key not in _deletes(app)


def test_a_file_purge_deletes_the_converted_and_target_keys(app, admin_client):
    """A purge queues the transcoded key and a pending move target (#515).

    Both count as claimed only while the row exists. Before, the purge
    deleted only the untouched key."""

    from tests.factories import make_movie_file

    with app.app_context():
        movie = make_movie("Converted Purge", 1995, tmdb_id=515001)
        file = make_movie_file(movie, "Bluray-1080p")
        file.aws_key = "untouched/Converted Purge (1995).mkv"
        file.aws_transcoded_key = "transcoded/Converted Purge (1995).mkv"
        file.aws_key_target = "untouched/Converted Purge (1995) - [HDR].mkv"
        db.session.commit()
        file_id = file.id

    token = csrf_token_from(admin_client.get(f"/file/{file_id}").get_data(as_text=True))
    response = admin_client.post(
        f"/file/{file_id}",
        data={"csrf_token": token, "delete_submit": "Delete File"},
    )
    assert response.status_code == 302
    assert sorted(_deletes(app)) == sorted(
        [
            "untouched/Converted Purge (1995).mkv",
            "transcoded/Converted Purge (1995).mkv",
            "untouched/Converted Purge (1995) - [HDR].mkv",
        ]
    )


def test_a_series_purge_deletes_the_converted_keys(app, admin_client):
    """The series delete queues each claimed key of its files (#515)."""

    with app.app_context():
        series = make_tv_series("Converted Show", tmdb_id=515002)
        file = make_tv_file(series, 1, 1, "Bluray-1080p")
        file.aws_key = "untouched/Converted Show S01E01.mkv"
        file.aws_transcoded_key = "transcoded/Converted Show S01E01.mkv"
        db.session.commit()
        series_id = series.id

    token = csrf_token_from(admin_client.get(f"/tv/{series_id}").get_data(as_text=True))
    admin_client.post(
        f"/tv/{series_id}/delete",
        data={"csrf_token": token, "delete_submit": "Delete"},
    )
    assert sorted(_deletes(app)) == [
        "transcoded/Converted Show S01E01.mkv",
        "untouched/Converted Show S01E01.mkv",
    ]


def test_a_wedged_share_at_the_unlink_still_queues_the_archive_deletes(
    app, admin_client, monkeypatch
):
    """An EIO at the unlink after the commit is logged (follow-up #513).

    It answered 500, and the AWS deletes and the folder cleanup never ran."""

    import errno
    import os

    from tests.factories import make_movie_file

    with app.app_context():
        movie = make_movie("Wedged Purge", 1996, tmdb_id=513001)
        file = make_movie_file(movie, "DVD")
        file.aws_key = "untouched/Wedged Purge (1996).mkv"
        db.session.commit()
        file_id = file.id
        library = os.path.join(app.config["LIBRARY_DIR"], file.file_path)
    os.makedirs(os.path.dirname(library), exist_ok=True)
    with open(library, "wb") as handle:
        handle.write(b"video")
    real_remove = os.remove

    def wedged(path, *args, **kwargs):
        if path == library:
            raise OSError(errno.EIO, "Input/output error", path)
        return real_remove(path, *args, **kwargs)

    token = csrf_token_from(admin_client.get(f"/file/{file_id}").get_data(as_text=True))
    monkeypatch.setattr(os, "remove", wedged)
    response = admin_client.post(
        f"/file/{file_id}",
        data={"csrf_token": token, "delete_submit": "Delete File"},
    )
    monkeypatch.undo()
    assert response.status_code == 302
    assert _deletes(app) == ["untouched/Wedged Purge (1996).mkv"]
    real_remove(library)


def test_the_purge_delete_skips_a_key_with_no_object(app, monkeypatch):
    """A pending move target has no object. No delete marker goes there (follow-up #515)."""

    from app import aws_storage
    from tests.fakes import ArchiveS3

    s3 = ArchiveS3(objects={"untouched/Here.mkv": {"ContentLength": 1}})
    monkeypatch.setattr(aws_storage, "aws_s3_client", lambda *a, **k: s3)
    monkeypatch.setitem(app.config, "AWS_BUCKET", "test-bucket")
    aws_storage.aws_delete("untouched/Not Yet.mkv", only_if_present=True)
    assert s3.deletes == []
    assert "untouched/Not Yet.mkv" not in s3.markers
    aws_storage.aws_delete("untouched/Here.mkv", only_if_present=True)
    assert [key for key, _ in s3.deletes] == ["untouched/Here.mkv"]


# The claim check and the delete of an archive are one step (#686)


def test_a_queued_delete_keeps_a_key_that_a_record_claims_before_it_runs(
    app, monkeypatch, caplog
):
    """A page queues the delete of an unclaimed key. A re-import claims it first."""

    import logging

    from app import aws_storage
    from app.main.library import _queue_archive_deletes
    from tests.fakes import ArchiveS3

    key = "untouched/Claimed Later (1997) - [Bluray-1080p].mkv"
    s3 = ArchiveS3(objects={key: {"ContentLength": 1}})
    monkeypatch.setattr(aws_storage, "aws_s3_client", lambda *a, **k: s3)
    monkeypatch.setitem(app.config, "AWS_BUCKET", "test-bucket")
    with app.app_context():
        _queue_archive_deletes([key])
    jobs = [
        job
        for job in app.request_queue.jobs
        if job.func_name == "app.videos.aws_delete" and job.args == (key,)
    ]
    assert len(jobs) == 1

    # The same rip comes in again before the job runs. Its row claims the key.

    with app.app_context():
        movie = make_movie("Claimed Later", 1997)
        make_movie_file(movie, "Bluray-1080p", aws_key=key)
        db.session.commit()

    with caplog.at_level(logging.INFO):
        aws_storage.aws_delete(*jobs[0].args, **jobs[0].kwargs)
    assert s3.deletes == []
    assert key in s3.objects
    kept = [r for r in caplog.records if "claims it now" in r.message]
    assert len(kept) == 1


def test_a_delete_for_an_unclaimed_key_still_deletes(app, monkeypatch):
    """With no claim, the option deletes as before. only_if_present still works."""

    from app import aws_storage
    from tests.fakes import ArchiveS3

    s3 = ArchiveS3(objects={"untouched/Nobody (1990).mkv": {"ContentLength": 1}})
    monkeypatch.setattr(aws_storage, "aws_s3_client", lambda *a, **k: s3)
    monkeypatch.setitem(app.config, "AWS_BUCKET", "test-bucket")
    aws_storage.aws_delete(
        "untouched/Not There (1990).mkv", only_if_present=True, only_if_unclaimed=True
    )
    assert s3.deletes == []
    assert "untouched/Not There (1990).mkv" not in s3.markers
    aws_storage.aws_delete(
        "untouched/Nobody (1990).mkv", only_if_present=True, only_if_unclaimed=True
    )
    assert [key for key, _ in s3.deletes] == ["untouched/Nobody (1990).mkv"]


def test_a_delete_with_no_option_deletes_a_claimed_key_as_before(app, monkeypatch):
    """A job that the older code queued has no option. It deletes as before."""

    from app import aws_storage
    from tests.fakes import ArchiveS3

    key = "untouched/Old Job (1991) - [DVD].mkv"
    s3 = ArchiveS3(objects={key: {"ContentLength": 1}})
    monkeypatch.setattr(aws_storage, "aws_s3_client", lambda *a, **k: s3)
    monkeypatch.setitem(app.config, "AWS_BUCKET", "test-bucket")
    with app.app_context():
        make_movie_file(make_movie("Old Job", 1991), "DVD", aws_key=key)
        db.session.commit()
    aws_storage.aws_delete(key, only_if_present=True)
    assert [deleted for deleted, _ in s3.deletes] == [key]


def test_a_delete_keeps_a_key_that_an_archived_original_claims(app, monkeypatch):
    """The task uses the same claim rule as the pages. An original counts."""

    from app import aws_storage
    from tests.fakes import ArchiveS3

    with app.app_context():
        movie = make_movie("Original Claim", 1992)
        key = _original(movie_id=movie.id)
        db.session.commit()
    s3 = ArchiveS3(objects={key: {"ContentLength": 1}})
    monkeypatch.setattr(aws_storage, "aws_s3_client", lambda *a, **k: s3)
    monkeypatch.setitem(app.config, "AWS_BUCKET", "test-bucket")
    aws_storage.aws_delete(key, only_if_present=True, only_if_unclaimed=True)
    assert s3.deletes == []


def test_a_wedged_share_at_the_unlink_still_removes_the_triage_aids(app, monkeypatch):
    """The EIO branch returned before the aids went (#579).

    The row is gone after the commit. No sweep removes the aids later."""

    import errno
    import os

    from app.models import File
    from app.triage import triage_snapshot_dir

    def wedged(path, *args, **kwargs):
        raise OSError(errno.EIO, "Input/output error", path)

    with app.app_context():
        aids = triage_snapshot_dir(579001)
        os.makedirs(os.path.join(aids, "s1"), exist_ok=True)
        monkeypatch.setattr(os, "remove", wedged)
        File.delete_library_copy("Movies/Wedged (1996)/Wedged (1996).mkv", 579001)
        monkeypatch.undo()
    assert not os.path.exists(aids)
