"""Deferred-retry scheduling: deterministic job ids that replace rather than
stack, scheduled jobs that actually bind to their target functions (the
enqueue_in kwarg-leak bug class), the native ScheduledJobRegistry defers
(#22), the cron table, and the import-directory watchdog.
"""

import inspect
import json
import os
import threading
import time

import pytest

from rq.exceptions import NoSuchJobError
from rq.job import Job

from app import cron_table, retry_job_id, safe_job_id
from app.videos import (
    acquire_lock_or_defer,
    localization_task,
    sync_aws_s3_storage_task,
)


def scheduled_ids(queue):
    """Job ids in the queue's native ScheduledJobRegistry (#22)."""

    from rq.registry import ScheduledJobRegistry

    return ScheduledJobRegistry(queue=queue).get_job_ids()


def scheduled_jobs(queue):
    """The scheduled jobs themselves, skipping any already-expired ids."""

    return [
        job
        for job in (queue.fetch_job(job_id) for job_id in scheduled_ids(queue))
        if job is not None
    ]


def assert_binds(job):
    """The scheduled call must match the target function's signature."""

    inspect.signature(job.func).bind(*(job.args or ()), **(job.kwargs or {}))


@pytest.fixture
def held_lock(app):
    """Hold a redlock for the duration of a test."""

    locks = []

    def hold(resource, ttl_ms=60000):
        lock = app.lock_manager.lock(resource, ttl_ms)
        assert lock, f"could not take test lock on {resource}"
        locks.append(lock)
        return lock

    yield hold
    for lock in locks:
        app.lock_manager.unlock(lock)


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

        # Scheduling kwargs must not leak into the function call (the
        # enqueue_in bug class: unrecognized kwargs pass through to the task)

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
    """The real localization path: a held title lock schedules one retry."""

    basename = "Jaws (1975) - [DVD].mkv"
    file_path = os.path.join(incoming_dir, basename)
    with open(file_path, "wb") as f:
        f.write(b"not a real video")

    # Backdate past the completeness gate so this test reaches the lock

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
    finally:
        os.remove(file_path)


def test_localization_defers_while_file_is_growing(app, incoming_dir):
    """A file still being copied is rescheduled, not processed or rejected."""

    basename = "Growing (2020) - [DVD].mkv"
    file_path = os.path.join(incoming_dir, basename)
    with open(file_path, "wb") as f:
        f.write(b"start")

    stop = threading.Event()

    def writer():
        while not stop.is_set():
            with open(file_path, "ab") as f:
                f.write(b"more bytes")
            time.sleep(0.5)

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
            assert retries == [retry_job_id("localization_task", f"'{basename}'", 0, 0)]
            assert_binds(Job.fetch(retries[0], connection=app.redis))
    finally:
        stop.set()
        thread.join()
        os.remove(file_path)

    # The file must not have been moved to rejects (empty reason
    # subdirectories may exist; only actual files count)
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


def test_cron_table_entries_are_well_formed(app):
    """Every cron-table row names a resolvable function, a five-field
    cron string, the maintenance queue, and a description — the
    scheduler process trusts the table blindly (#22)."""

    import importlib

    with app.app_context():
        entries = cron_table(app.config)

    assert len(entries) >= 10
    for entry in entries:
        assert len(entry["cron"].split()) == 5, entry
        module_name, func_name = entry["func"].rsplit(".", 1)
        assert hasattr(importlib.import_module(module_name), func_name), entry
        assert entry["queue"] == "fitzflix-maintenance"
        assert entry["description"]
        assert isinstance(entry["timeout"], int)


def test_cron_frequency_sort_orders_by_class_then_parameter():
    """The System page's ordering (#22): every-X-minutes by X, hourly by
    minute, daily by time, weekly by day and time, monthly by
    day-of-month and time."""

    from app.main.admin import _cron_frequency_key

    ordered = [
        "*/10 * * * *",
        "*/15 * * * *",
        "0 * * * *",
        "30 * * * *",
        "0 0 * * *",
        "45 1 * * *",
        "15 2 * * *",
        "15 4 * * 1",
        "0 4 1 * *",
        "0 3 18 * *",
    ]
    assert sorted(reversed(ordered), key=_cron_frequency_key) == ordered


def test_watchdog_enqueues_new_import_files(app):
    """A file appearing in IMPORT_DIR gets a localization job automatically."""

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
    """Catch-all for the enqueue_in bug class across every defer produced above."""

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


def test_finalize_transcoding_transient_rename_defers_with_lock_held(app, monkeypatch):
    """A flaky mount during the transcode rename retries just that step,
    keeping the title lock so nothing else touches the file meanwhile."""

    import errno

    import app.videos as videos

    from app import db
    from app.models import File
    from app.videos import finalize_transcoding
    from tests.factories import make_movie, make_movie_file

    with app.app_context():
        movie = make_movie("Transcode Retry", 2021)
        file = make_movie_file(movie, "Bluray-1080p")
        db.session.commit()
        file_id = file.id
        identifier = file.file_identifier()

        lock = app.lock_manager.lock(identifier, 60000)
        assert lock

        def flaky_rename(src, dst):
            raise OSError(errno.ENOTCONN, "Socket is not connected")

        monkeypatch.setattr(videos.os, "rename", flaky_rename)

        assert finalize_transcoding(file_id, lock) is False

        retries = [
            job
            for job in scheduled_jobs(app.sql_queue)
            if job.id.startswith("retry_finalize_transcoding")
        ]
        assert [job.id for job in retries] == [
            retry_job_id("finalize_transcoding", file_id, 1)
        ]
        job = retries[0]
        assert list(job.args) == [file_id, lock]
        assert job.kwargs == {"transient_retries": 1}
        assert_binds(job)

        # The lock is still held for the retry, and the transcode date was
        # rolled back

        assert not app.lock_manager.lock(identifier, 1000)
        db.session.expire_all()
        assert db.session.get(File, file_id).date_transcoded is None

        app.lock_manager.unlock(lock)


def test_finalize_transcoding_releases_lock_after_max_retries(app, monkeypatch):
    """Once the budget is spent, the failure is logged and the lock freed."""

    import errno

    import app.videos as videos

    from app import db
    from app.videos import finalize_transcoding
    from tests.factories import make_movie, make_movie_file

    with app.app_context():
        movie = make_movie("Transcode Hopeless", 2021)
        file = make_movie_file(movie, "Bluray-1080p")
        db.session.commit()
        file_id = file.id
        identifier = file.file_identifier()

        lock = app.lock_manager.lock(identifier, 60000)
        assert lock

        def flaky_rename(src, dst):
            raise OSError(errno.ENOTCONN, "Socket is not connected")

        monkeypatch.setattr(videos.os, "rename", flaky_rename)

        finalize_transcoding(
            file_id, lock, transient_retries=videos.MAX_TRANSIENT_RETRIES
        )

        assert not any(
            job.id.startswith("retry_finalize_transcoding")
            for job in scheduled_jobs(app.sql_queue)
        )

        # The lock was released by the finally

        relock = app.lock_manager.lock(identifier, 1000)
        assert relock
        app.lock_manager.unlock(relock)


def test_mkvpropedit_transient_error_defers_and_releases_lock(app, monkeypatch):
    """A transient mount error before the file is restructured reschedules
    the edit with the same arguments; the retry re-acquires the lock."""

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

        assert videos.mkvpropedit_task(file_id, "2", None, []) is False

        retries = [
            job
            for job in scheduled_jobs(app.file_queue)
            if job.id.startswith(safe_job_id("retry:mkvpropedit_task"))
        ]
        assert [job.id for job in retries] == [
            retry_job_id("mkvpropedit_task", file_id, 1)
        ]
        job = retries[0]
        assert list(job.args) == [file_id, "2", None, []]
        assert job.kwargs == {"transient_retries": 1}
        assert_binds(job)

        # The lock was released so the retry can take it fresh

        relock = app.lock_manager.lock(identifier, 1000)
        assert relock
        app.lock_manager.unlock(relock)


def test_mkvpropedit_does_not_retry_once_file_was_restructured(app, monkeypatch):
    """After the reorder remux lands, the original track numbers no longer
    match the file, so even a transient error must not schedule a retry."""

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


def test_download_transient_error_defers(app, monkeypatch):
    """A transient import-volume error during an S3 download reschedules the
    download; the S3 object and SQS message are unaffected."""

    import errno

    import app.videos as videos

    from app import aws_storage

    def flaky_download(key, basename, sqs_receipt_handle=None):
        raise OSError(errno.EBADF, "Bad file descriptor")

    monkeypatch.setattr(aws_storage, "aws_download", flaky_download)

    with app.app_context():
        result = videos.download_task(
            "untouched/Thing (2021) - [DVD].mkv",
            "Thing (2021) - [DVD].mkv",
            "receipt-123",
        )
    assert result is False

    retries = [
        job
        for job in scheduled_jobs(app.file_queue)
        if job.id.startswith(safe_job_id("retry:download_task"))
    ]
    assert [job.id for job in retries] == [
        retry_job_id("download_task", "'Thing (2021) - [DVD].mkv'", 1)
    ]
    job = retries[0]
    assert list(job.args) == [
        "untouched/Thing (2021) - [DVD].mkv",
        "Thing (2021) - [DVD].mkv",
        "receipt-123",
    ]
    assert job.kwargs == {"transient_retries": 1}
    assert_binds(job)


def test_download_gives_up_after_max_retries(app, monkeypatch):
    """Once the budget is spent, the failure is logged and the SQS message
    left for redelivery, like any other download failure."""

    import errno

    import app.videos as videos

    from app import aws_storage

    def flaky_download(key, basename, sqs_receipt_handle=None):
        raise OSError(errno.EBADF, "Bad file descriptor")

    monkeypatch.setattr(aws_storage, "aws_download", flaky_download)

    with app.app_context():
        result = videos.download_task(
            "untouched/Thing (2021) - [DVD].mkv",
            "Thing (2021) - [DVD].mkv",
            "receipt-123",
            transient_retries=videos.MAX_TRANSIENT_RETRIES,
        )
    assert result is not True

    assert not any(
        job.id.startswith(safe_job_id("retry:download_task"))
        for job in scheduled_jobs(app.file_queue)
    )


def test_aws_download_reraises_transient_volume_errors(app, monkeypatch):
    """The download's in-place retry loop must not eat mount errors: they
    escape immediately (partial file cleaned up) so the caller can defer."""

    import errno

    import app.videos as videos

    from app import aws_storage

    class FakeS3:
        def head_object(self, Bucket, Key):
            return {"ContentLength": 100}

        def download_file(self, bucket, key, filename, Callback=None):
            with open(filename, "wb") as f:
                f.write(b"partial")
            raise OSError(errno.EBADF, "Bad file descriptor")

    monkeypatch.setattr(aws_storage, "aws_s3_client", lambda **kwargs: FakeS3())
    monkeypatch.setattr(aws_storage, "aws_sqs_client", lambda: None)

    with app.app_context():
        with pytest.raises(OSError):
            videos.aws_download("untouched/x.mkv", "x.mkv")

        hidden = os.path.join(app.config["IMPORT_DIR"], ".x.mkv")
        assert not os.path.exists(hidden)


def test_tmdb_refresh_hands_off_to_sql_queue(app):
    """The fetch phase runs on the multi-worker request queue; every
    database write happens in apply_tmdb_refresh on the single-worker sql
    queue, so concurrent fetches can't produce concurrent writes."""

    from app import db
    from app.videos import refresh_tmdb_info
    from tests.factories import make_movie

    with app.app_context():
        movie = make_movie("Handoff Film", 2003)
        db.session.commit()
        movie_id = movie.id

        # With no TMDB_API_KEY configured the fetch finds nothing, but the
        # apply job must still be enqueued (it also rewrites file paths)

        assert refresh_tmdb_info("Movies", movie_id) is True

        jobs = [
            job
            for job in app.sql_queue.jobs
            if job.func_name == "app.videos.apply_tmdb_refresh"
            and job.kwargs.get("id") == movie_id
        ]
        assert len(jobs) == 1
        assert jobs[0].kwargs["library"] == "Movies"
        assert jobs[0].kwargs["tmdb_payload"] is None
        assert_binds(jobs[0])


def test_apply_tmdb_refresh_round_trips_payload(app):
    """The compressed payload built by the fetch phase decompresses and
    applies in the database phase."""

    import zlib

    from app import db
    from app.models import TVSeries
    from app.videos import apply_tmdb_refresh
    from tests.factories import make_tv_series

    with app.app_context():
        tv = make_tv_series("Payload Show")
        db.session.commit()
        tv_id = tv.id

        payload = zlib.compress(
            json.dumps({"id": 999, "name": "Payload Show Canonical"}).encode("utf-8")
        )
        assert apply_tmdb_refresh("TV Shows", tv_id, tmdb_payload=payload) is True

        db.session.expire_all()
        refreshed = TVSeries.query.filter_by(id=tv_id).first()
        assert refreshed.tmdb_id == 999
        assert refreshed.tmdb_name == "Payload Show Canonical"


def test_tmdb_apply_defers_while_title_is_locked(app, held_lock):
    """A movie's TMDb apply rewrites file paths and can merge records, so
    it must wait for any import chain holding one of the title's locks."""

    from app import db
    from app.videos import apply_tmdb_refresh
    from tests.factories import make_movie, make_movie_file

    with app.app_context():
        movie = make_movie("Locked Film", 1999)
        file = make_movie_file(movie, "DVD")
        db.session.commit()
        movie_id = movie.id

        held_lock(file.file_identifier())

        assert apply_tmdb_refresh("Movies", movie_id) is False

        retries = [
            job
            for job in scheduled_jobs(app.sql_queue)
            if job.id.startswith("retry_apply_tmdb_refresh")
        ]
        assert [job.id for job in retries] == [
            safe_job_id(f"retry:apply_tmdb_refresh:Movies:{movie_id}")
        ]
        job = retries[0]
        assert job.kwargs["library"] == "Movies"
        assert job.kwargs["id"] == movie_id
        assert_binds(job)


def test_tmdb_apply_locks_the_merge_target_too(app, held_lock):
    """When a TMDb id points at an existing movie, the apply will merge
    records — so a lock held on the *target* movie's files defers it."""

    from app import db
    from app.videos import apply_tmdb_refresh
    from tests.factories import make_movie, make_movie_file

    with app.app_context():
        source = make_movie("Duplicate Entry", 2001)
        make_movie_file(source, "DVD")
        target = make_movie("Canonical Entry", 2001, tmdb_id=4242)
        target_file = make_movie_file(target, "Bluray-1080p")
        db.session.commit()
        source_id = source.id

        held_lock(target_file.file_identifier())

        assert apply_tmdb_refresh("Movies", source_id, tmdb_id=4242) is False
        assert any(
            job.id == safe_job_id(f"retry:apply_tmdb_refresh:Movies:{source_id}")
            for job in scheduled_jobs(app.sql_queue)
        )


def test_tmdb_apply_releases_locks_when_done(app):
    from app import db
    from app.videos import apply_tmdb_refresh
    from tests.factories import make_movie, make_movie_file

    with app.app_context():
        movie = make_movie("Free Film", 2002)
        file = make_movie_file(
            movie, "DVD", untouched_basename="Free Film (2002) - [DVD].mkv"
        )
        db.session.commit()
        movie_id = movie.id
        identifier = file.file_identifier()

        assert apply_tmdb_refresh("Movies", movie_id) is True

        lock = app.lock_manager.lock(identifier, 1000)
        assert lock, "apply did not release the title lock"
        app.lock_manager.unlock(lock)
