"""Test the guards against a second localization of 1 file (#287).

A fixed job id (the file basename) can reach the import queue more than
once. rq pushes the id again and runs each entry. A stale finished or
failed entry of the id deletes a new job with the same id when it
expires. A late run can import a file that is already in the library.
"""

from datetime import timedelta

from rq.registry import FinishedJobRegistry, ScheduledJobRegistry

from app import db, retry_job_id, safe_job_id
from tests.factories import make_movie, make_movie_file

NAME = "Heat (1995) - [WEBDL-1080p].mkv"


def test_enqueue_skips_a_file_that_already_waits(app, tmp_path):
    from app.importing import enqueue_localization

    path = str(tmp_path / NAME)
    with app.app_context():
        assert enqueue_localization(path) is not None
        assert enqueue_localization(path) is None
        assert app.import_queue.job_ids == [safe_job_id(NAME)]


def test_enqueue_skips_a_file_with_a_scheduled_retry(app, tmp_path):
    """A retry of the file has its own id. It still counts as waiting."""

    from app.importing import enqueue_localization

    with app.app_context():
        retry = app.import_queue.enqueue_in(
            timedelta(minutes=5),
            "app.videos.localization_task",
            args=(str(tmp_path / NAME),),
            job_id=retry_job_id("localization_task", f"'{NAME}'", 1, 0),
        )
        assert retry.id in ScheduledJobRegistry(queue=app.import_queue).get_job_ids()
        assert enqueue_localization(str(tmp_path / NAME)) is None
        assert app.import_queue.job_ids == []


def test_enqueue_removes_a_stale_finished_entry(app, tmp_path):
    """The expiry of an old finished entry must not delete the new job."""

    from app.importing import enqueue_localization

    job_id = safe_job_id(NAME)
    with app.app_context():
        registry = FinishedJobRegistry(queue=app.import_queue)
        app.redis.zadd(registry.key, {job_id: 1})
        job = enqueue_localization(str(tmp_path / NAME))
        assert job is not None
        assert app.redis.zscore(registry.key, job_id) is None

        # The cleanup of expired entries now finds nothing to delete.
        registry.cleanup()
        assert app.redis.exists(f"rq:job:{job_id}")
        assert app.import_queue.job_ids == [job_id]


def test_already_cataloged_needs_the_same_path_and_size(app, monkeypatch, tmp_path):
    from app.importing import already_cataloged

    monkeypatch.setitem(app.config, "LIBRARY_DIR", str(tmp_path))
    folder = tmp_path / "Movies" / "Heat (1995)"
    folder.mkdir(parents=True)
    library_file = folder / "Heat (1995) - [WEBDL-1080p].mkv"
    library_file.write_bytes(b"x" * 100)

    with app.app_context():
        movie = make_movie("Heat", 1995)
        row = make_movie_file(movie, "WEBDL-1080p", filesize_bytes=100)
        db.session.commit()
        assert row.file_path == "Movies/Heat (1995)/Heat (1995) - [WEBDL-1080p].mkv"
        assert already_cataloged(str(library_file)) is True

        # A new download at the same path has another size.
        library_file.write_bytes(b"x" * 200)
        assert already_cataloged(str(library_file)) is False

        # A file outside the library is never cataloged.
        outside = tmp_path.parent / NAME
        outside.write_bytes(b"x" * 100)
        assert already_cataloged(str(outside)) is False


def test_localization_skips_a_cataloged_file(app, monkeypatch, tmp_path, caplog):
    """A late run returns at once and changes nothing."""

    import app.importing as importing

    monkeypatch.setitem(app.config, "LIBRARY_DIR", str(tmp_path))
    monkeypatch.setattr(importing, "already_cataloged", lambda path: True)
    monkeypatch.setattr("app.videos._dead_volumes", lambda paths: [])
    path = tmp_path / NAME
    path.write_bytes(b"movie")

    with caplog.at_level("INFO"):
        assert importing.localization_task(str(path)) is False
    assert "Is already in the library; nothing to import" in caplog.text
    assert path.exists()


def test_enqueue_skips_a_source_that_a_later_stage_holds(app, tmp_path):
    """A conversion, move, or cataloging job still holds the source (#296).

    The source stays in the import directory until the cataloging. A
    conversion keeps it there for hours. The hourly sweep must not queue
    it again."""

    from app.importing import enqueue_localization

    path = str(tmp_path / NAME)
    other = str(tmp_path / "Ronin (1998) - [WEBDL-1080p].mkv")
    details = {"basename": NAME}
    with app.app_context():
        for queue, func in (
            (app.transcode_queue, "app.videos.convert_localized_file"),
            (app.handoff_queue, "app.videos.move_localized_file"),
            (app.sql_queue, "app.videos.finalize_localization"),
        ):
            job = queue.enqueue(func, args=(path, details, None, "/staging/x"))
            assert enqueue_localization(path) is None
            assert app.import_queue.job_ids == []
            job.delete()

        # A retry that waits in the scheduled registry holds the source too.
        retry = app.handoff_queue.enqueue_in(
            timedelta(minutes=5),
            "app.videos.move_localized_file",
            path,
            details,
            None,
            "/staging/x",
        )
        assert enqueue_localization(path) is None
        retry.delete()

        # Another file is not held.
        app.transcode_queue.enqueue(
            "app.videos.convert_localized_file", args=(other, details, None, "/x")
        )
        assert enqueue_localization(path) is not None


def test_a_quick_reimport_gets_a_job_that_does_not_expire(app, tmp_path):
    """The finished hash of the id keeps its expiry. It must go first (#371)."""

    from app.importing import enqueue_localization

    path = str(tmp_path / NAME)
    job_id = safe_job_id(NAME)
    with app.app_context():
        first = enqueue_localization(path)
        app.import_queue.remove(first)
        first.set_status("finished")
        FinishedJobRegistry(queue=app.import_queue).add(first, 500)
        app.redis.expire(first.key, 500)

        second = enqueue_localization(path)
        assert second.id == job_id
        assert app.redis.ttl(second.key) == -1
        assert job_id not in FinishedJobRegistry(queue=app.import_queue)


def test_a_failed_first_run_stays_for_the_sweep(app, tmp_path):
    """The failed job keeps its id. The new job gets a time tag and counts (#371)."""

    from rq.registry import FailedJobRegistry

    from app.importing import enqueue_localization

    path = str(tmp_path / NAME)
    job_id = safe_job_id(NAME)
    failed = FailedJobRegistry(queue=app.import_queue)
    with app.app_context():
        first = enqueue_localization(path)
        app.import_queue.remove(first)
        first.set_status("failed")
        failed.add(first, 500)

        second = enqueue_localization(path)
        assert second.id.startswith(job_id + "_")
        assert job_id in failed.get_job_ids()
        assert enqueue_localization(path) is None
