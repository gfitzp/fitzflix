"""Test the deferred-retry scheduling.

The tests cover the deterministic job ids that replace and do not stack.
They cover the scheduled jobs that bind to their target functions (the
enqueue_in kwarg-leak bug class). They also cover the native
ScheduledJobRegistry defers and the import-directory watchdog. The
cron table is in test_cron_table, the S3 download in test_aws_download,
and the TMDB refresh phases in test_tmdb_apply_phases.
"""

import inspect
import json
import os
import threading
import time

import pytest

from rq.exceptions import NoSuchJobError
from rq.job import Job

from app import safe_job_id
from app.videos import (
    acquire_lock_or_defer,
    localization_task,
    sync_aws_s3_storage_task,
)


def requeue_id(*args, **kwargs):
    """Return a requeue id. The import of app.tasks waits for the test app."""

    from app.tasks import requeue_job_id

    return requeue_job_id(*args, **kwargs)


def scheduled_ids(queue):
    """Return the job ids in the native ScheduledJobRegistry of the queue."""

    from rq.registry import ScheduledJobRegistry

    return ScheduledJobRegistry(queue=queue).get_job_ids()


def scheduled_jobs(queue):
    """Return the scheduled jobs. This skips the ids that already expired."""

    return [
        job
        for job in (queue.fetch_job(job_id) for job_id in scheduled_ids(queue))
        if job is not None
    ]


def assert_binds(job):
    """Assert that the scheduled call matches the signature of the target function."""

    inspect.signature(job.func).bind(*(job.args or ()), **(job.kwargs or {}))


def test_defer_uses_deterministic_id_and_replaces(app, held_lock):
    with app.app_context():
        held_lock("test-title-lock")

        for _ in range(3):
            result = acquire_lock_or_defer(
                "test-title-lock",
                30000,
                app.import_queue,
                "app.videos.localization_task",
                minutes=(45, 75),
                timeout=3600,
                description="'Jaws (1975) - [DVD].mkv'",
                kwargs={"file_path": "/incoming/Jaws (1975) - [DVD].mkv"},
            )
            assert result is None

        retries = [
            job_id
            for job_id in scheduled_ids(app.import_queue)
            if job_id.startswith("retry_")
        ]
        assert retries == [
            safe_job_id("retry:localization_task:'Jaws (1975) - [DVD].mkv'")
        ]

        job = Job.fetch(retries[0], connection=app.redis)
        assert_binds(job)

        # The scheduling kwargs must not leak into the function call. This
        # is the enqueue_in bug class. Unknown kwargs pass through to the
        # task

        assert job.kwargs == {"file_path": "/incoming/Jaws (1975) - [DVD].mkv"}
        assert not job.args
        assert job.description == "'Jaws (1975) - [DVD].mkv'"


def test_defer_with_positional_args_binds(app, held_lock):
    with app.app_context():
        held_lock("test-args-lock")

        acquire_lock_or_defer(
            "test-args-lock",
            30000,
            app.import_queue,
            "app.videos.localization_task",
            minutes=(5, 15),
            timeout=3600,
            description="'args-form.mkv'",
            args=("/incoming/args-form.mkv",),
        )

        job = Job.fetch(
            safe_job_id("retry:localization_task:'args-form.mkv'"), connection=app.redis
        )
        assert_binds(job)
        assert job.args == ("/incoming/args-form.mkv",)


def test_acquire_returns_lock_when_free(app):
    with app.app_context():
        lock = acquire_lock_or_defer(
            "test-free-lock",
            30000,
            app.import_queue,
            "app.videos.localization_task",
            minutes=(5, 15),
            timeout=3600,
            description="'free.mkv'",
            kwargs={"file_path": "/incoming/free.mkv"},
        )
        assert lock
        app.lock_manager.unlock(lock)
        assert safe_job_id("retry:localization_task:'free.mkv'") not in scheduled_ids(
            app.import_queue
        )


def test_localization_defers_while_title_is_locked(app, held_lock, incoming_dir):
    """Test the real localization path. A held title lock schedules 1 retry."""

    basename = "Jaws (1975) - [DVD].mkv"
    file_path = os.path.join(incoming_dir, basename)
    with open(file_path, "wb") as f:
        f.write(b"not a real video")

    # Set the file time before the completeness gate. Thus, the test
    # reaches the lock

    stamp = time.time() - 3600
    os.utime(file_path, (stamp, stamp))

    identifier = json.dumps(
        {
            "title": "Jaws",
            "year": 1975,
            "feature_type": None,
            "plex_title": "Jaws (1975)",
            "edition": None,
        }
    )
    try:
        with app.app_context():
            held_lock(identifier)
            for _ in range(2):
                localization_task(file_path)

            retries = [
                job_id
                for job_id in scheduled_ids(app.import_queue)
                if job_id.startswith("retry_")
            ]
            assert retries == [safe_job_id(f"retry:localization_task:'{basename}'")]
            assert_binds(Job.fetch(retries[0], connection=app.redis))

            # The waiting retry counts as pending. A sweep must not queue
            # the file again next to it.

            from app.importing import localization_pending

            assert localization_pending(app.import_queue, file_path)
    finally:
        os.remove(file_path)


def test_localization_defers_while_file_is_growing(app, incoming_dir, monkeypatch):
    """Test that the task reschedules a file that a copy still writes.

    The task does not process or reject the file."""

    from app import importing

    # The writer adds bytes each 0.1 second. Thus, a wait of 1 second
    # always sees the file grow.

    monkeypatch.setattr(importing, "GROWTH_CHECK_SECONDS", 1)

    basename = "Growing (2020) - [DVD].mkv"
    file_path = os.path.join(incoming_dir, basename)
    with open(file_path, "wb") as f:
        f.write(b"start")

    stop = threading.Event()

    def writer():
        while not stop.is_set():
            with open(file_path, "ab") as f:
                f.write(b"more bytes")
            time.sleep(0.1)

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    try:
        with app.app_context():
            localization_task(file_path)
            retries = [
                job_id
                for job_id in scheduled_ids(app.import_queue)
                if job_id.startswith("retry_")
            ]
            assert retries == [
                requeue_id(
                    "app.videos.localization_task",
                    file_path,
                    {
                        "transient_retries": 0,
                        "completeness_retries": 0,
                        "space_waits": 0,
                        "tmdb_retries": 0,
                    },
                )
            ]
            assert_binds(Job.fetch(retries[0], connection=app.redis))
    finally:
        stop.set()
        thread.join()
        os.remove(file_path)

    # The task must not move the file to the rejects. Empty reason
    # subdirectories can exist. Only actual files count
    rejected = [
        name for _, _, files in os.walk(app.config["REJECTS_DIR"]) for name in files
    ]
    assert not rejected


def test_sync_defers_while_queues_are_busy(app):
    with app.app_context():
        marker = app.file_queue.enqueue(
            "app.videos.localization_task",
            args=("/nonexistent",),
            description="busy marker",
        )
        try:
            sync_aws_s3_storage_task()

            job = Job.fetch(
                safe_job_id("retry:sync_aws_s3_storage_task"), connection=app.redis
            )
            assert_binds(job)
            assert not job.args and not job.kwargs
        finally:
            marker.delete()


def test_watchdog_enqueues_new_import_files(app):
    """Test that a new file in IMPORT_DIR gets a localization job automatically."""

    basename = "Watched (2021) - [DVD].mkv"
    file_path = os.path.join(app.config["IMPORT_DIR"], basename)
    with open(file_path, "wb") as f:
        f.write(b"not a real video")

    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if safe_job_id(basename) in app.import_queue.job_ids:
                break
            time.sleep(0.5)
        else:
            pytest.fail("watchdog never enqueued the new file")

        job = app.import_queue.fetch_job(safe_job_id(basename))
        assert job.args == (file_path,)
        assert_binds(job)
    finally:
        os.remove(file_path)


def test_every_scheduled_job_binds(app, held_lock, incoming_dir):
    """Test each defer from above for the enqueue_in bug class."""

    with app.app_context():
        held_lock("catchall-lock")
        acquire_lock_or_defer(
            "catchall-lock",
            30000,
            app.file_queue,
            "app.videos.localization_task",
            minutes=(5, 15),
            timeout=3600,
            description="'catchall.mkv'",
            kwargs={"file_path": "/incoming/catchall.mkv"},
        )

        checked = 0
        for job in scheduled_jobs(app.file_queue):
            try:
                assert_binds(job)
            except NoSuchJobError:
                continue
            checked += 1
        assert checked >= 1


def test_mkvpropedit_transient_error_defers_and_releases_lock(app, monkeypatch):
    """Test that a transient mkvpropedit error defers and releases the lock.

    A transient mount error can occur before the task restructures the
    file. Then the task reschedules the edit with the same arguments.
    This includes the language corrections, because the task applied
    nothing yet. The retry takes the lock again."""

    import errno

    import app.videos as videos

    from app import tracks

    from app import db
    from tests.factories import make_movie, make_movie_file

    with app.app_context():
        movie = make_movie("Propedit Retry", 2021)
        file = make_movie_file(movie, "Bluray-1080p")
        db.session.commit()
        file_id = file.id
        identifier = file.file_identifier()

        def flaky_unlocked(*args, **kwargs):
            raise OSError(errno.EBADF, "Bad file descriptor")

        monkeypatch.setattr(tracks, "mkvpropedit_unlocked", flaky_unlocked)

        assert videos.mkvpropedit_task(file_id, "2", None, [], {"a1": "eng"}) is False

        retries = [
            job
            for job in scheduled_jobs(app.file_queue)
            if job.id.startswith(safe_job_id("retry:mkvpropedit_task"))
        ]
        assert [job.id for job in retries] == [
            requeue_id("app.videos.mkvpropedit_task", file_id, {"transient_retries": 1})
        ]
        job = retries[0]
        assert list(job.args) == [file_id, "2", None, [], {"a1": "eng"}]
        with app.app_context():
            layout = tracks._track_layout(file_id)
        assert job.kwargs == {"transient_retries": 1, "layout": layout}
        assert_binds(job)

        # The task released the lock. Thus, the retry can take it again

        relock = app.lock_manager.lock(identifier, 1000)
        assert relock
        app.lock_manager.unlock(relock)


def test_mkvpropedit_does_not_retry_once_file_was_restructured(app, monkeypatch):
    """Test that mkvpropedit does not retry after the file restructure.

    After the reorder remux completes, the original track numbers no
    longer match the file. Thus, even a transient error must not
    schedule a retry."""

    import errno

    import app.videos as videos

    from app import tracks

    from app import db
    from tests.factories import make_movie, make_movie_file

    with app.app_context():
        movie = make_movie("Propedit Unsafe", 2021)
        file = make_movie_file(movie, "Bluray-1080p")
        db.session.commit()
        file_id = file.id

        def unsafe_unlocked(*args, **kwargs):
            error = OSError(errno.EBADF, "Bad file descriptor")
            error.retry_unsafe = True
            raise error

        monkeypatch.setattr(tracks, "mkvpropedit_unlocked", unsafe_unlocked)

        with pytest.raises(OSError):
            videos.mkvpropedit_task(file_id, "2", None, [])

        assert not any(
            job.id.startswith(safe_job_id("retry:mkvpropedit_task"))
            for job in scheduled_jobs(app.file_queue)
        )
