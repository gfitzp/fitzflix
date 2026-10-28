"""Test the S3 download path of a restore (aws_download and download_task).

The tests cover the deferral of a transient error, the retry budget
and its backoff, and a missing object. They also cover an expired or
pending restore, the auth errors, and a restore that an import
overtook. The CloudFront
path is in test_cdn_download.
"""

import os

import pytest

from tests.fakes import S3, SQS
from app import safe_job_id
from tests.test_scheduling import assert_binds, requeue_id, scheduled_jobs


def test_download_transient_error_defers(app, monkeypatch):
    """Test that a transient error defers the download.

    A transient import-volume error during an S3 download reschedules
    the download. The S3 object and the SQS message do not change."""

    import errno

    import app.videos as videos

    from app import aws_storage

    def flaky_download(key, basename, sqs_receipt_handle=None, **kwargs):
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
        requeue_id(
            "app.videos.download_task",
            "untouched/Thing (2021) - [DVD].mkv",
            {"transient_retries": 1, "volume_waits": 0},
        )
    ]
    job = retries[0]
    assert list(job.args) == [
        "untouched/Thing (2021) - [DVD].mkv",
        "Thing (2021) - [DVD].mkv",
        "receipt-123",
    ]
    assert job.kwargs == {"transient_retries": 1, "volume_waits": 0}
    assert_binds(job)


def test_download_gives_up_after_max_retries(app, monkeypatch):
    """Test that the download stops after the maximum retries.

    After the budget is used, the task logs the failure. It leaves the
    SQS message for redelivery, as for each other download failure."""

    import errno

    import app.videos as videos

    from app import aws_storage

    def flaky_download(key, basename, sqs_receipt_handle=None, **kwargs):
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

    s3 = S3(
        head={"ContentLength": 100},
        errors=OSError(errno.EBADF, "Bad file descriptor"),
        partial=b"partial",
    )
    monkeypatch.setattr(aws_storage, "aws_s3_client", lambda **kwargs: s3)
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


def test_aws_download_missing_object_is_not_retried(app, monkeypatch):
    """Test that a missing object does not cause a retry.

    A 404 means that the object is gone permanently. There are no
    download retries. The function deletes the SQS message. Thus, SQS
    cannot deliver it again. No partial file remains."""

    import app.videos as videos

    from app import aws_storage

    s3 = S3(
        head=_client_error("404", 404, "HeadObject"),
        errors=_client_error("404", 404, "GetObject"),
    )
    sqs = SQS()
    monkeypatch.setattr(aws_storage, "aws_s3_client", lambda **kwargs: s3)
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

    # No Restore header. The object is back in cold storage

    s3 = S3(
        head={"ContentLength": 100},
        errors=_client_error("InvalidObjectState", 403, "GetObject"),
    )
    sqs = SQS()
    restored = []
    monkeypatch.setattr(aws_storage, "aws_s3_client", lambda **kwargs: s3)
    monkeypatch.setattr(aws_storage, "aws_sqs_client", lambda: sqs)
    monkeypatch.setattr(
        aws_storage, "aws_restore", lambda key, **kwargs: restored.append(key)
    )

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

    s3 = S3(
        head={"ContentLength": 100, "Restore": 'ongoing-request="true"'},
        errors=_client_error("InvalidObjectState", 403, "GetObject"),
    )
    sqs = SQS()
    restored = []
    monkeypatch.setattr(aws_storage, "aws_s3_client", lambda **kwargs: s3)
    monkeypatch.setattr(aws_storage, "aws_sqs_client", lambda: sqs)
    monkeypatch.setattr(
        aws_storage, "aws_restore", lambda key, **kwargs: restored.append(key)
    )

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

    def head(call):
        # The odd calls come from the progress callback that sizes the
        # download. The even calls are the restore-status check of the
        # handler. That check fails
        if call % 2 == 0:
            raise _client_error("ServiceUnavailable", 503, "HeadObject")
        return {"ContentLength": 100}

    s3 = S3(head=head, errors=_client_error("InvalidObjectState", 403, "GetObject"))
    sqs = SQS()
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

    s3 = S3(
        head={"ContentLength": 100},
        errors=RuntimeError("connection reset"),
        partial=b"partial",
    )
    sqs = SQS()
    monkeypatch.setattr(aws_storage, "aws_s3_client", lambda **kwargs: s3)
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

    s3 = S3(head={"ContentLength": 100}, errors=RuntimeError("connection reset"))
    delays = []
    monkeypatch.setattr(aws_storage, "aws_s3_client", lambda **kwargs: s3)
    monkeypatch.setattr(aws_storage, "aws_sqs_client", lambda: SQS())
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

    s3 = S3(
        head={"ContentLength": 100},
        errors=_client_error("AccessDenied", 403, "GetObject"),
    )
    sqs = SQS()
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


def _overtaken_fixture(app, title, localized, present=True):
    """Make an archived movie file, and optionally its library copy."""

    from app import db
    from tests.factories import make_movie, make_movie_file

    key = f"untouched/{title} (1950) - [WEBDL-1080p].mkv"
    file = make_movie_file(
        make_movie(title, 1950),
        "WEBDL-1080p",
        aws_key=key,
        date_localized=localized,
    )
    db.session.commit()
    if present:
        path = os.path.join(app.config["LIBRARY_DIR"], file.file_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(b"x")
    return key


def test_aws_restore_records_the_request_time(app, monkeypatch):
    """Test that a restore request stores its time for download_task.

    A request again for an expired restored copy keeps the first time."""

    from datetime import datetime, timezone

    import app.videos as videos

    from app import aws_storage

    class FakeS3:
        def list_objects(self, **kwargs):
            return {
                "Contents": [{"Key": kwargs["Prefix"], "StorageClass": "DEEP_ARCHIVE"}]
            }

        def head_object(self, **kwargs):
            return {}

        def restore_object(self, **kwargs):
            return {}

    monkeypatch.setattr(aws_storage, "aws_s3_client", lambda **kwargs: FakeS3())
    key = "untouched/Recorded (1950) - [DVD].mkv"
    first = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
    with app.app_context():
        aws_storage.record_restore_request(app.redis, key, first)
        videos.aws_restore(key, record_request=False)
        assert aws_storage.restore_requested_at(app.redis, key) == first

        videos.aws_restore(key)
        assert aws_storage.restore_requested_at(app.redis, key) > first


def test_download_task_skips_a_restore_that_an_import_overtook(app, monkeypatch):
    """Test that download_task drops a restore of a replaced file.

    The record was imported after the restore request, and its file is
    present. The download would replace the new file with the old one."""

    from datetime import datetime, timezone

    import app.videos as videos

    from app import aws_storage

    downloads = []
    sqs = SQS()
    monkeypatch.setattr(
        aws_storage, "aws_download", lambda *args, **kwargs: downloads.append(args)
    )
    monkeypatch.setattr(aws_storage, "aws_sqs_client", lambda: sqs)
    with app.app_context():
        key = _overtaken_fixture(app, "Overtaken", datetime(2026, 10, 20, 18, 0))
        aws_storage.record_restore_request(
            app.redis, key, datetime(2026, 10, 20, 13, 0, tzinfo=timezone.utc)
        )
        assert videos.download_task(key, os.path.basename(key), "receipt-1") is True

    assert downloads == []
    assert sqs.deleted == ["receipt-1"]


def test_download_task_keeps_a_restore_of_a_file_imported_before(app, monkeypatch):
    """Test that a restore of a present file continues when it is on purpose.

    The record was imported before the restore request. For example, the
    operator replaces a bad local copy."""

    from datetime import datetime, timezone

    import app.videos as videos

    from app import aws_storage

    downloads = []
    monkeypatch.setattr(
        aws_storage,
        "aws_download",
        lambda *args, **kwargs: downloads.append(args) or aws_storage.DOWNLOAD_COMPLETE,
    )
    with app.app_context():
        key = _overtaken_fixture(app, "Kept On Purpose", datetime(2026, 10, 19, 18, 0))
        aws_storage.record_restore_request(
            app.redis, key, datetime(2026, 10, 20, 13, 0, tzinfo=timezone.utc)
        )
        assert videos.download_task(key, os.path.basename(key), "receipt-2") is True

    assert len(downloads) == 1


def test_download_task_keeps_a_restore_of_an_absent_file(app, monkeypatch):
    """Test that a restore continues when the library file is absent."""

    from datetime import datetime, timezone

    import app.videos as videos

    from app import aws_storage

    downloads = []
    monkeypatch.setattr(
        aws_storage,
        "aws_download",
        lambda *args, **kwargs: downloads.append(args) or aws_storage.DOWNLOAD_COMPLETE,
    )
    with app.app_context():
        key = _overtaken_fixture(
            app, "Kept Absent", datetime(2026, 10, 20, 18, 0), present=False
        )
        aws_storage.record_restore_request(
            app.redis, key, datetime(2026, 10, 20, 13, 0, tzinfo=timezone.utc)
        )
        assert videos.download_task(key, os.path.basename(key), "receipt-3") is True

    assert len(downloads) == 1


def test_two_restores_with_one_untouched_name_never_overwrite(app, monkeypatch):
    """The second restore waits under its own staging name (#433).

    Before, both staged as the hidden untouched name, and the second
    rename replaced the first file before the import took it."""

    from app import aws_storage

    name = "Shared Name (1999) - [DVD].mkv"
    import_dir = app.config["IMPORT_DIR"]
    first = S3(download=b"first rip")
    second = S3(download=b"second rip")
    monkeypatch.setattr(aws_storage, "aws_sqs_client", lambda: SQS())

    with app.app_context():
        monkeypatch.setattr(aws_storage, "aws_s3_client", lambda **kw: first)
        assert (
            aws_storage.aws_download(f"untouched/{name}", name)
            == aws_storage.DOWNLOAD_COMPLETE
        )
        monkeypatch.setattr(aws_storage, "aws_s3_client", lambda **kw: second)
        sibling = "untouched/Shared Name (1999) - [DVD] (12).mkv"
        assert aws_storage.aws_download(sibling, name) == aws_storage.DOWNLOAD_COMPLETE

        target = os.path.join(import_dir, name)
        staged = os.path.join(import_dir, f".restore-{sibling.replace('/', '-')}")
        try:
            with open(target, "rb") as f:
                assert f.read() == b"first rip"
            with open(staged, "rb") as f:
                assert f.read() == b"second rip"
            (job,) = [
                job
                for job in scheduled_jobs(app.file_queue)
                if job.func_name == "app.aws_storage.place_restored_import_task"
            ]
            assert job.args == (staged, name)

            # The import takes the first file. The wait then places the second.
            os.remove(target)
            assert aws_storage.place_restored_import_task(staged, name) is True
            with open(target, "rb") as f:
                assert f.read() == b"second rip"
            assert not os.path.exists(staged)
        finally:
            for path in (target, staged):
                if os.path.exists(path):
                    os.remove(path)


def test_a_restore_already_in_progress_still_records_the_request(app, monkeypatch):
    """An archive move asked first, with no record (#532).

    A later restore from a page gets RestoreAlreadyInProgress. It must
    record its time, or the download never occurs. An existing time
    stays."""

    from datetime import datetime, timezone

    import botocore.exceptions

    from app import aws_storage

    class InProgressS3:
        def list_objects(self, **kwargs):
            return {
                "Contents": [{"Key": kwargs["Prefix"], "StorageClass": "DEEP_ARCHIVE"}]
            }

        def head_object(self, **kwargs):
            return {}

        def restore_object(self, **kwargs):
            raise botocore.exceptions.ClientError(
                {"Error": {"Code": "RestoreAlreadyInProgress"}}, "RestoreObject"
            )

    monkeypatch.setattr(aws_storage, "aws_s3_client", lambda **kwargs: InProgressS3())
    key = "untouched/In Progress (1951) - [DVD].mkv"
    with app.app_context():
        app.redis.delete(aws_storage.RESTORE_REQUESTED_KEY.format(key))
        aws_storage.aws_restore(key, record_request=False)
        assert aws_storage.restore_requested_at(app.redis, key) is None

        aws_storage.aws_restore(key)
        first = aws_storage.restore_requested_at(app.redis, key)
        assert first is not None

        earlier = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
        aws_storage.record_restore_request(app.redis, key, earlier)
        aws_storage.aws_restore(key)
        assert aws_storage.restore_requested_at(app.redis, key) == earlier


def _direct_restore_row(app, title):
    """Return (key, library path) of a row whose archive is a modified/ object."""

    from app import db
    from tests.factories import make_movie, make_movie_file

    with app.app_context():
        file = make_movie_file(make_movie(title, 1952), "DVD")
        key = f"modified/{file.basename}"
        file.aws_key = key
        db.session.commit()
        return key, os.path.join(app.config["LIBRARY_DIR"], file.file_path)


def _volume_retries(app):
    return [
        job
        for job in scheduled_jobs(app.file_queue)
        if job.id.startswith(safe_job_id("retry:download_task"))
        and job.kwargs.get("volume_waits")
    ]


def test_a_direct_restore_waits_for_a_dead_library_volume(app, monkeypatch):
    """A dropped share is a plain folder on the boot disk (#533).

    The restore must not download into it or place a file there."""

    import app.videos as videos
    from app import aws_storage

    key, library_path = _direct_restore_row(app, "Dead Share Before")
    downloads = []
    monkeypatch.setattr(
        aws_storage, "aws_download", lambda *a, **k: downloads.append(a) or True
    )
    monkeypatch.setattr(videos, "_dead_volumes", lambda paths: ["/Volumes/Movies"])
    with app.app_context():
        assert videos.download_task(key, os.path.basename(key), "receipt-1") is False
    assert downloads == []
    (retry,) = _volume_retries(app)
    assert retry.kwargs == {"transient_retries": 0, "volume_waits": 1}
    retry.delete()


def test_a_volume_that_drops_during_the_download_defers_the_placement(app, monkeypatch):
    import app.videos as videos
    from app import aws_storage

    key, library_path = _direct_restore_row(app, "Dead Share During")
    answers = [[], ["/Volumes/Movies"]]
    monkeypatch.setattr(videos, "_dead_volumes", lambda paths: answers.pop(0))
    placed = []

    def download(key, basename, handle=None, place_at=None, before_place=None):
        before_place()
        placed.append(place_at)
        return aws_storage.DOWNLOAD_COMPLETE

    monkeypatch.setattr(aws_storage, "aws_download", download)
    with app.app_context():
        assert videos.download_task(key, os.path.basename(key), "receipt-2") is False
    assert placed == []
    (retry,) = _volume_retries(app)
    assert retry.kwargs["volume_waits"] == 1
    retry.delete()


def test_a_track_edit_during_the_download_refuses_the_placement(app, monkeypatch):
    """The archive became older than the library file during the download (#533)."""

    from app import db
    from app.models import File
    import app.videos as videos
    from app import aws_storage

    key, library_path = _direct_restore_row(app, "Edited During")
    os.makedirs(os.path.dirname(library_path), exist_ok=True)
    with open(library_path, "wb") as f:
        f.write(b"newer library file")
    placed = []

    def download(key, basename, handle=None, place_at=None, before_place=None):
        # A track edit on another worker marks the archive stale.
        db.session.execute(
            db.update(File).where(File.aws_key == key).values(aws_stale=True)
        )
        db.session.commit()
        before_place()
        placed.append(place_at)
        return aws_storage.DOWNLOAD_COMPLETE

    monkeypatch.setattr(aws_storage, "aws_download", download)
    try:
        with app.app_context():
            assert videos.download_task(key, os.path.basename(key)) is True
        assert placed == []
        with open(library_path, "rb") as f:
            assert f.read() == b"newer library file"
    finally:
        os.remove(library_path)


def test_a_refused_placement_discards_the_download(app, monkeypatch):
    import app.videos as videos
    from app import aws_storage

    s3 = S3(head={"ContentLength": 3})
    monkeypatch.setattr(aws_storage, "aws_s3_client", lambda **kwargs: s3)
    monkeypatch.setattr(aws_storage, "aws_sqs_client", lambda: None)

    def refuse():
        raise aws_storage.PlacementDeferred(["/Volumes/Movies"])

    with app.app_context():
        with pytest.raises(aws_storage.PlacementDeferred):
            videos.aws_download(
                "modified/y.mkv",
                "y.mkv",
                place_at=os.path.join(app.config["LIBRARY_DIR"], "Movies/y.mkv"),
                before_place=refuse,
            )
        assert s3.downloads == ["modified/y.mkv"]
        staged = os.path.join(app.config["IMPORT_DIR"], ".restore-modified-y.mkv")
        assert not os.path.exists(staged)
        assert not os.path.exists(
            os.path.join(app.config["LIBRARY_DIR"], "Movies/y.mkv")
        )
