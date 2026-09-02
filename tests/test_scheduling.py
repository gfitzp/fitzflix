"""Test the deferred-retry scheduling.

The tests cover the deterministic job ids that replace and do not stack.
They cover the scheduled jobs that bind to their target functions (the
enqueue_in kwarg-leak bug class). They also cover the native
ScheduledJobRegistry defers, the cron table, and the import-directory
watchdog.
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
    finally:
        os.remove(file_path)


def test_localization_defers_while_file_is_growing(app, incoming_dir):
    """Test that the task reschedules a file that a copy still writes.

    The task does not process or reject the file."""

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


def test_cron_table_entries_are_well_formed(app):
    """Test that each cron-table row is well formed.

    Each row names a function that resolves, a 5-field cron string, the
    maintenance queue, and a description. The scheduler process trusts
    the table without a check."""

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


def test_every_cron_table_schedule_gets_a_written_description():
    """Test that each cron-table schedule gets a written description.

    The schedule column of the System page never falls back to a raw
    cron string. The description came from a hardcoded map before. That
    map was 9 schedules behind the table. This test fails when the
    schedule of a new job is outside the grammar of the generator.

    The test switches on each config-dependent row. Thus, the test also
    covers the jobs that the test config leaves out of the table."""

    from app.main.admin import _cron_description

    entries = cron_table(
        {"AWS_SQS_URL": "sqs", "PLEX_URL": "plex", "PLEX_TOKEN": "token"}
    )

    raw = {
        entry["cron"]
        for entry in entries
        if _cron_description(entry["cron"]) == entry["cron"]
    }
    assert not raw, f"no written description for {sorted(raw)}"


def test_cron_descriptions_read_as_english():
    """Test the house phrasing for each frequency class.

    This includes the 2 times of day that have names, and the
    Oxford-comma list."""

    from app.main.admin import _cron_description

    assert _cron_description("* * * * *") == "Every minute"
    assert _cron_description("*/10 * * * *") == "Every 10 minutes"
    assert _cron_description("0 * * * *") == "Hourly"
    assert _cron_description("30 * * * *") == "Hourly at :30"
    assert _cron_description("7,37 * * * *") == "Twice hourly at :07 and :37"
    assert (
        _cron_description("3,18,33,48 * * * *")
        == "Four times hourly at :03, :18, :33, and :48"
    )
    assert _cron_description("0 0 * * *") == "Daily at midnight"
    assert _cron_description("0 12 * * *") == "Daily at noon"
    assert _cron_description("45 1 * * *") == "Daily at 1:45 AM"
    assert _cron_description("30 4 * * *") == "Daily at 4:30 AM"
    assert _cron_description("15 4 * * 1") == "Weekly on Monday at 4:15 AM"
    assert _cron_description("0 1 * * 0") == "Weekly on Sunday at 1:00 AM"
    assert _cron_description("0 4 1 * *") == "Monthly on the 1st at 4:00 AM"
    assert _cron_description("0 3 18 * *") == "Monthly on the 18th at 3:00 AM"


def test_cron_descriptions_fall_back_to_the_raw_string():
    """Test that an unsupported cron string shows as it is.

    The generator shows the cron string itself, not a wrong sentence."""

    from app.main.admin import _cron_description

    for cron_string in (
        "0 */6 * * *",  # an hour step
        "0-30 * * * *",  # a range
        "0 5 * * 1,4",  # 2 times weekly is not "weekly on"
        "0 0 1 1 *",  # yearly
        "0 0 1 * 1",  # day-of-month and day-of-week together
        "61 * * * *",  # out of range
        "nonsense",
    ):
        assert _cron_description(cron_string) == cron_string


def test_cron_frequency_sort_orders_by_class_then_parameter():
    """Test the ordering of the System page.

    The order is: every-X-minutes by X, hourly by minute, daily by time,
    weekly by day and time, monthly by day-of-month and time."""

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


def test_finalize_transcoding_transient_rename_defers_with_lock_held(app, monkeypatch):
    """Test that a transient rename error in the transcode defers with the lock.

    A flaky mount during the transcode rename retries only that step.
    The task keeps the title lock. Thus, no other task touches the file
    in the meantime."""

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

        # The task still holds the lock for the retry. The task rolled back
        # the transcode date

        assert not app.lock_manager.lock(identifier, 1000)
        db.session.expire_all()
        assert db.session.get(File, file_id).date_transcoded is None

        app.lock_manager.unlock(lock)


def test_finalize_transcoding_releases_lock_after_max_retries(app, monkeypatch):
    """Test that the task logs the failure and frees the lock after the budget."""

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

        # The finally block released the lock

        relock = app.lock_manager.lock(identifier, 1000)
        assert relock
        app.lock_manager.unlock(relock)


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
            retry_job_id("mkvpropedit_task", file_id, 1)
        ]
        job = retries[0]
        assert list(job.args) == [file_id, "2", None, [], {"a1": "eng"}]
        assert job.kwargs == {"transient_retries": 1}
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


def test_download_transient_error_defers(app, monkeypatch):
    """Test that a transient error defers the download.

    A transient import-volume error during an S3 download reschedules
    the download. The S3 object and the SQS message do not change."""

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
    """Test that the download stops after the maximum retries.

    After the budget is used, the task logs the failure. It leaves the
    SQS message for redelivery, as for each other download failure."""

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
    """Test that aws_download raises the transient volume errors again.

    The in-place retry loop of the download must not swallow a mount
    error. The error escapes immediately, and the function removes the
    partial file. Thus, the caller can defer."""

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


def _client_error(code, status, operation):
    """Make a botocore ClientError with the shape of a real S3 error response."""

    import botocore.exceptions

    return botocore.exceptions.ClientError(
        {
            "Error": {"Code": code, "Message": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        operation,
    )


class _FakeSQS:
    """Record the SQS deletions. Do not perform them."""

    def __init__(self):
        self.deleted = []

    def delete_message(self, QueueUrl=None, ReceiptHandle=None):
        self.deleted.append(ReceiptHandle)
        return {}


def test_aws_download_missing_object_is_not_retried(app, monkeypatch):
    """Test that a missing object does not cause a retry.

    A 404 means that the object is gone permanently. There are no
    download retries. The function deletes the SQS message. Thus, SQS
    cannot deliver it again. No partial file remains."""

    import app.videos as videos

    from app import aws_storage

    class FakeS3:
        def head_object(self, Bucket, Key):
            raise _client_error("404", 404, "HeadObject")

        def download_file(self, bucket, key, filename, Callback=None):
            raise _client_error("404", 404, "GetObject")

    sqs = _FakeSQS()
    monkeypatch.setattr(aws_storage, "aws_s3_client", lambda **kwargs: FakeS3())
    monkeypatch.setattr(aws_storage, "aws_sqs_client", lambda: sqs)

    with app.app_context():
        assert (
            videos.aws_download("untouched/x.mkv", "x.mkv", "receipt-404")
            == aws_storage.DOWNLOAD_OBJECT_MISSING
        )
        assert not os.path.exists(os.path.join(app.config["IMPORT_DIR"], ".x.mkv"))

        # Without a receipt handle, there is no message to remove

        assert (
            videos.aws_download("untouched/x.mkv", "x.mkv")
            == aws_storage.DOWNLOAD_OBJECT_MISSING
        )

    assert sqs.deleted == ["receipt-404"]


def test_aws_download_expired_restore_requests_new_restore(app, monkeypatch):
    """Test that an expired restore causes a new restore request.

    The restored copy can expire before the download. Then the function
    requests a new restore and deletes the stale SQS message. The
    completion notification of the restore starts the download again."""

    import app.videos as videos

    from app import aws_storage

    class FakeS3:
        def head_object(self, Bucket, Key):
            # No Restore header. The object is back in cold storage
            return {"ContentLength": 100}

        def download_file(self, bucket, key, filename, Callback=None):
            raise _client_error("InvalidObjectState", 403, "GetObject")

    sqs = _FakeSQS()
    restored = []
    monkeypatch.setattr(aws_storage, "aws_s3_client", lambda **kwargs: FakeS3())
    monkeypatch.setattr(aws_storage, "aws_sqs_client", lambda: sqs)
    monkeypatch.setattr(aws_storage, "aws_restore", lambda key: restored.append(key))

    with app.app_context():
        assert (
            videos.aws_download("untouched/x.mkv", "x.mkv", "receipt-stale")
            == aws_storage.DOWNLOAD_RESTORE_PENDING
        )

    assert restored == ["untouched/x.mkv"]
    assert sqs.deleted == ["receipt-stale"]


def test_aws_download_waits_for_restore_already_underway(app, monkeypatch):
    """Test that the download waits for a restore that is in progress.

    The function makes no duplicate restore request. It still deletes
    the stale SQS message."""

    import app.videos as videos

    from app import aws_storage

    class FakeS3:
        def head_object(self, Bucket, Key):
            return {"ContentLength": 100, "Restore": 'ongoing-request="true"'}

        def download_file(self, bucket, key, filename, Callback=None):
            raise _client_error("InvalidObjectState", 403, "GetObject")

    sqs = _FakeSQS()
    restored = []
    monkeypatch.setattr(aws_storage, "aws_s3_client", lambda **kwargs: FakeS3())
    monkeypatch.setattr(aws_storage, "aws_sqs_client", lambda: sqs)
    monkeypatch.setattr(aws_storage, "aws_restore", lambda key: restored.append(key))

    with app.app_context():
        assert (
            videos.aws_download("untouched/x.mkv", "x.mkv", "receipt-stale")
            == aws_storage.DOWNLOAD_RESTORE_PENDING
        )

    assert restored == []
    assert sqs.deleted == ["receipt-stale"]


def test_aws_download_failed_status_check_spends_a_retry(app, monkeypatch):
    """Test that a failed status check uses a retry.

    The restore-status check in the InvalidObjectState handler can fail.
    That failure uses a retry, as each other error does. It does not
    escape the loop. The function leaves the SQS message for
    redelivery."""

    import app.videos as videos

    from app import aws_storage

    class FakeS3:
        def __init__(self):
            self.head_calls = 0

        def head_object(self, Bucket, Key):
            # The odd calls come from the progress callback that sizes the
            # download. The even calls are the restore-status check of the
            # handler. That check fails
            self.head_calls += 1
            if self.head_calls % 2 == 0:
                raise _client_error("ServiceUnavailable", 503, "HeadObject")
            return {"ContentLength": 100}

        def download_file(self, bucket, key, filename, Callback=None):
            raise _client_error("InvalidObjectState", 403, "GetObject")

    s3 = FakeS3()
    sqs = _FakeSQS()
    monkeypatch.setattr(aws_storage, "aws_s3_client", lambda **kwargs: s3)
    monkeypatch.setattr(aws_storage, "aws_sqs_client", lambda: sqs)
    monkeypatch.setattr(aws_storage, "DOWNLOAD_RETRY_SLEEP", lambda seconds: None)

    with app.app_context():
        assert videos.aws_download("untouched/x.mkv", "x.mkv", "receipt-503") is False

    assert s3.head_calls == 20  # 10 retries, 2 head_object calls each
    assert sqs.deleted == []


def test_aws_download_exhausted_retries_clean_up_partial_file(app, monkeypatch):
    """Test that the download removes the partial file after the retries.

    The import scans skip the dotfiles. Thus, a download that uses its
    complete retry budget must remove its partial file. If not, the
    file leaks and is invisible."""

    import app.videos as videos

    from app import aws_storage

    class FakeS3:
        def head_object(self, Bucket, Key):
            return {"ContentLength": 100}

        def download_file(self, bucket, key, filename, Callback=None):
            with open(filename, "wb") as f:
                f.write(b"partial")
            raise RuntimeError("connection reset")

    sqs = _FakeSQS()
    monkeypatch.setattr(aws_storage, "aws_s3_client", lambda **kwargs: FakeS3())
    monkeypatch.setattr(aws_storage, "aws_sqs_client", lambda: sqs)
    monkeypatch.setattr(aws_storage, "DOWNLOAD_RETRY_SLEEP", lambda seconds: None)

    with app.app_context():
        assert videos.aws_download("untouched/x.mkv", "x.mkv", "receipt-reset") is False

        assert not os.path.exists(os.path.join(app.config["IMPORT_DIR"], ".x.mkv"))

    assert sqs.deleted == []


def test_aws_download_backs_off_between_retries(app, monkeypatch):
    """Test that the retries back off between attempts.

    The retries back off exponentially, with a limit of 60 seconds.
    They do not hit S3 back-to-back. The final failure does not sleep."""

    import app.videos as videos

    from app import aws_storage

    class FakeS3:
        def head_object(self, Bucket, Key):
            return {"ContentLength": 100}

        def download_file(self, bucket, key, filename, Callback=None):
            raise RuntimeError("connection reset")

    delays = []
    monkeypatch.setattr(aws_storage, "aws_s3_client", lambda **kwargs: FakeS3())
    monkeypatch.setattr(aws_storage, "aws_sqs_client", lambda: _FakeSQS())
    monkeypatch.setattr(aws_storage, "DOWNLOAD_RETRY_SLEEP", delays.append)

    with app.app_context():
        assert videos.aws_download("untouched/x.mkv", "x.mkv") is False

    assert delays == [1, 2, 4, 8, 16, 32, 60, 60, 60]


def test_aws_download_gives_up_immediately_on_auth_errors(app, monkeypatch):
    """Test that the download stops immediately on an auth error.

    A credentials or permissions error fails the same on each attempt.
    Thus, the function does not use the retry budget on it. There is 1
    attempt and no backoff. The function leaves the SQS message for
    redelivery after the operator repairs the account."""

    import app.videos as videos

    from app import aws_storage

    class FakeS3:
        def __init__(self):
            self.attempts = 0

        def head_object(self, Bucket, Key):
            return {"ContentLength": 100}

        def download_file(self, bucket, key, filename, Callback=None):
            self.attempts += 1
            raise _client_error("AccessDenied", 403, "GetObject")

    s3 = FakeS3()
    sqs = _FakeSQS()
    delays = []
    monkeypatch.setattr(aws_storage, "aws_s3_client", lambda **kwargs: s3)
    monkeypatch.setattr(aws_storage, "aws_sqs_client", lambda: sqs)
    monkeypatch.setattr(aws_storage, "DOWNLOAD_RETRY_SLEEP", delays.append)

    with app.app_context():
        assert (
            videos.aws_download("untouched/x.mkv", "x.mkv", "receipt-denied") is False
        )

    assert s3.attempts == 1
    assert delays == []
    assert sqs.deleted == []


def test_download_task_reports_download_outcome(app, monkeypatch):
    """Test that download_task reports the download outcome.

    download_task must not report success when aws_download used its
    complete retry budget. A used budget is not a transient error.
    Thus, the task schedules no retry."""

    import app.videos as videos

    from app import aws_storage

    monkeypatch.setattr(aws_storage, "aws_download", lambda *args, **kwargs: False)

    with app.app_context():
        result = videos.download_task(
            "untouched/Thing (2021) - [DVD].mkv",
            "Thing (2021) - [DVD].mkv",
            "receipt-123",
        )
    assert result is False

    assert not any(
        job.id.startswith(safe_job_id("retry:download_task"))
        for job in scheduled_jobs(app.file_queue)
    )

    # Each truthy status means that the task handled the message

    for status in (
        aws_storage.DOWNLOAD_COMPLETE,
        aws_storage.DOWNLOAD_OBJECT_MISSING,
        aws_storage.DOWNLOAD_RESTORE_PENDING,
    ):
        monkeypatch.setattr(
            aws_storage, "aws_download", lambda *args, s=status, **kwargs: s
        )

        with app.app_context():
            result = videos.download_task(
                "untouched/Thing (2021) - [DVD].mkv",
                "Thing (2021) - [DVD].mkv",
                "receipt-123",
            )
        assert result is True


def test_tmdb_refresh_hands_off_to_sql_queue(app):
    """Test that the TMDB refresh hands off to the sql queue.

    The fetch phase runs on the multi-worker request queue. Each
    database write occurs in apply_tmdb_refresh on the single-worker
    sql queue. Thus, concurrent fetches cannot cause concurrent writes."""

    from app import db
    from app.videos import refresh_tmdb_info
    from tests.factories import make_movie

    with app.app_context():
        movie = make_movie("Handoff Film", 2003)
        db.session.commit()
        movie_id = movie.id

        # With no TMDB_API_KEY, the fetch finds nothing. But the task must
        # still enqueue the apply job. That job also rewrites file paths

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
    """Test that the payload survives the round trip.

    The fetch phase builds the compressed payload. The database phase
    decompresses and applies it."""

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
    """Test that the TMDB apply defers when the title is locked.

    The TMDB apply of a movie rewrites file paths and can merge records.
    Thus, it must wait for an import chain that holds a lock of the
    title."""

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
    """Test that the TMDB apply also locks the merge target.

    A TMDB id can point at an existing movie. Then the apply merges the
    records. Thus, a lock on the files of the *target* movie defers the
    apply."""

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


def test_cron_table_refreshes_streaming_availability_nightly(app):
    """Test that the cron table refreshes the streaming availability nightly.

    The availability refresh runs nightly (2026-08). The watchlist, the
    Criterion, and the filmography pages read the cache that it fills.
    They never fetch inline."""

    with app.app_context():
        entries = {entry["func"]: entry for entry in cron_table(app.config)}

    entry = entries["app.streaming.refresh_availability"]
    assert entry["cron"] == "30 4 * * *"
    assert entry["timeout"] >= 3600


def test_apply_tmdb_refresh_saves_the_payload_when_apply_raises(app, monkeypatch):
    """Test that apply_tmdb_refresh saves the payload when the apply raises.

    The task writes the payload next to the log as gzipped JSON named
    for the record. Thus, you can examine a transient upstream glitch
    (the malformed aggregate credits of 2026-08-22) after TMDB serves
    clean data again."""

    import glob
    import gzip
    import zlib

    from app import db
    from app.models import TVSeries
    from app.videos import apply_tmdb_refresh
    from tests.factories import make_tv_series

    with app.app_context():
        tv = make_tv_series("Glitch Show")
        db.session.commit()
        tv_id = tv.id

        def explode(self, tmdb_info):
            raise AttributeError("'list' object has no attribute 'get'")

        monkeypatch.setattr(TVSeries, "tmdb_tv_apply", explode)

        info = {"id": 4242, "aggregate_credits": {"cast": [{"roles": [["bad"]]}]}}
        payload = zlib.compress(json.dumps(info).encode("utf-8"))
        assert apply_tmdb_refresh("TV Shows", tv_id, tmdb_payload=payload) is False

        pattern = f"{app.config['LOG_FILE']}.tmdb-payload.tv-{tv_id}.*.json.gz"
        dumps = glob.glob(pattern)
        assert len(dumps) == 1
        with gzip.open(dumps[0], "rb") as saved:
            assert json.loads(saved.read()) == info
