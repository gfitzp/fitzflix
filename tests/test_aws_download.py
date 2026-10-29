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
        def head_object(self, **kwargs):
            return {"StorageClass": "DEEP_ARCHIVE"}

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
        def head_object(self, **kwargs):
            return {"StorageClass": "DEEP_ARCHIVE"}

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

    def download(key, basename, handle=None, place_at=None, before_place=None, **_):
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

    def download(key, basename, handle=None, place_at=None, before_place=None, **_):
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


def test_a_deferred_placement_keeps_the_finished_download(app, monkeypatch):
    """A volume wait at the placement keeps the download (follow-up #533).

    The defer discarded the finished file and its sidecar. Each of up to
    288 waits downloaded the whole object again. The next run places the
    kept file with no new download."""

    import app.videos as videos
    from app import aws_storage

    s3 = S3(head={"ContentLength": 3, "ETag": '"v1"'})
    monkeypatch.setattr(aws_storage, "aws_s3_client", lambda **kwargs: s3)
    monkeypatch.setattr(aws_storage, "aws_sqs_client", lambda: None)
    library = os.path.join(app.config["LIBRARY_DIR"], "Movies/y.mkv")
    staged = os.path.join(app.config["IMPORT_DIR"], ".restore-modified-y.mkv")

    def refuse(etag=None, size=None, library_stat=None):
        raise aws_storage.PlacementDeferred(["/Volumes/Movies"])

    try:
        with app.app_context():
            with pytest.raises(aws_storage.PlacementDeferred):
                videos.aws_download(
                    "modified/y.mkv", "y.mkv", place_at=library, before_place=refuse
                )
            assert s3.downloads == ["modified/y.mkv"]
            assert os.path.exists(staged)
            assert not os.path.exists(library)

            os.makedirs(os.path.dirname(library), exist_ok=True)
            assert (
                videos.aws_download(
                    "modified/y.mkv",
                    "y.mkv",
                    place_at=library,
                    before_place=lambda etag=None, size=None, **_: None,
                )
                == aws_storage.DOWNLOAD_COMPLETE
            )
        assert s3.downloads == ["modified/y.mkv"]
        assert open(library, "rb").read() == b"s3!"
        assert not os.path.exists(staged)
        assert not os.path.exists(f"{staged}.etag")
    finally:
        for path in (staged, f"{staged}.etag", library):
            if os.path.exists(path):
                os.remove(path)


def _place_with(app, monkeypatch, s3, key):
    """Run a direct restore of key through the real download and placement."""

    import app.videos as videos
    from app import aws_storage

    monkeypatch.setattr(aws_storage, "aws_s3_client", lambda **kwargs: s3)
    monkeypatch.setattr(aws_storage, "aws_sqs_client", lambda: None)
    monkeypatch.setattr(videos, "_dead_volumes", lambda paths: [])
    with app.app_context():
        return videos.download_task(key, os.path.basename(key))


def test_a_rename_during_the_download_places_at_the_new_path(app, monkeypatch):
    """The placement goes to the path of the fresh row (#563).

    The task placed at the path that it read before the download. A
    TMDB rename meanwhile left the copy under the old name, uncataloged,
    and the row still named a missing file."""

    from app import db
    from app.models import File

    key, old_path = _direct_restore_row(app, "Renamed During")
    with app.app_context():
        file = File.query.filter_by(aws_key=key).one()
        new_relative = file.file_path.replace("Renamed During", "Renamed Later")

    class RenamingS3(S3):
        def download_file(self, bucket, key, filename, Callback=None):
            db.session.execute(
                db.update(File)
                .where(File.aws_key == key)
                .values(file_path=new_relative)
            )
            db.session.commit()
            super().download_file(bucket, key, filename, Callback)

    new_path = os.path.join(app.config["LIBRARY_DIR"], new_relative)
    try:
        assert _place_with(app, monkeypatch, RenamingS3(), key) is True
        assert open(new_path, "rb").read() == b"s3!"
        assert not os.path.exists(old_path)
    finally:
        for path in (new_path, old_path):
            if os.path.exists(path):
                os.remove(path)


def test_a_new_archive_during_the_download_refuses_the_placement(app, monkeypatch):
    """A track edit archived the file again during the download (#563).

    Its own upload cleared the stale flag. Thus, only the ETag shows that
    the downloaded bytes are older than the library file."""

    key, library_path = _direct_restore_row(app, "Archived During")
    os.makedirs(os.path.dirname(library_path), exist_ok=True)
    with open(library_path, "wb") as f:
        f.write(b"edited library file")
    s3 = S3(
        head=lambda call: {
            "ContentLength": 3,
            "ETag": '"v1"' if call == 1 else '"v2"',
        }
    )
    try:
        assert _place_with(app, monkeypatch, s3, key) is True
        assert s3.downloads == [key]
        with open(library_path, "rb") as f:
            assert f.read() == b"edited library file"
        staged = os.path.join(
            app.config["IMPORT_DIR"], f".restore-{key.replace('/', '-')}"
        )
        assert not os.path.exists(staged)
    finally:
        os.remove(library_path)


def test_a_handle_that_arrives_during_the_download_deletes_the_message(
    app, monkeypatch
):
    """A download queued with no handle can get one later (#572).

    sqs_retrieve_task stores it under SQS_HANDLE_KEY. Every delete site
    tested the original argument. Then the message came back and a
    second full download ran."""

    from types import SimpleNamespace

    import app.videos as videos
    from app import aws_storage

    s3 = S3()
    sqs = SQS()
    monkeypatch.setattr(aws_storage, "aws_s3_client", lambda **kwargs: s3)
    monkeypatch.setattr(aws_storage, "aws_sqs_client", lambda: sqs)
    monkeypatch.setattr(
        aws_storage.rq, "get_current_job", lambda: SimpleNamespace(id="late-job")
    )
    monkeypatch.setattr(aws_storage, "place_restored_import", lambda *a: True)
    handle_key = aws_storage.SQS_HANDLE_KEY.format(job_id="late-job")
    app.redis.set(handle_key, "late-handle")
    staged = os.path.join(app.config["IMPORT_DIR"], ".restore-untouched-late.mkv")
    try:
        with app.app_context():
            assert (
                videos.aws_download("untouched/late.mkv", "late.mkv")
                == aws_storage.DOWNLOAD_COMPLETE
            )
        assert sqs.deleted == ["late-handle"]
    finally:
        app.redis.delete(handle_key)
        if os.path.exists(staged):
            os.remove(staged)


def test_a_restore_of_a_missing_key_with_a_longer_sibling_does_nothing(
    app, monkeypatch
):
    """The listing of the prefix with MaxKeys=1 can name another key (#572).

    It listed a longer key that starts with the missing key. Then the
    HEAD of the missing key raised, or the shortcut read the storage
    class of the sibling. A HEAD of the key itself decides now."""

    from app import aws_storage
    from tests.fakes import ArchiveS3

    key = "untouched/Sibling (1950) - [DVD].mkv"
    s3 = ArchiveS3(
        objects={f"{key}.etag": {"ContentLength": 3, "StorageClass": "STANDARD"}}
    )
    monkeypatch.setattr(aws_storage, "aws_s3_client", lambda **kwargs: s3)
    with app.app_context():
        aws_storage.aws_restore(key)
        assert s3.restores == []
        assert not [
            job
            for job in app.file_queue.jobs
            if job.func_name == "app.videos.download_task"
        ]


def test_a_key_move_during_the_download_still_places_the_file(app, monkeypatch):
    """The second HEAD of a moved key refused the placement (#563 follow-up).

    An archive key move (#369) copies the same bytes under the key of
    the fresh row. The downloaded copy is good. Only a key that no row
    names any more refuses."""

    import botocore.exceptions

    from app import db
    from app.models import File

    key, library_path = _direct_restore_row(app, "Moved During")
    moved_key = "modified/Moved Later.mkv"
    gone = botocore.exceptions.ClientError(
        {"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject"
    )

    def head(call):
        if call == 1:
            # The move lands while the download runs.
            db.session.execute(
                db.update(File).where(File.aws_key == key).values(aws_key=moved_key)
            )
            db.session.commit()
            return {"ContentLength": 3, "ETag": '"v1"'}
        if call == 2:
            raise gone
        return {"ContentLength": 3, "ETag": '"v1"'}

    s3 = S3(head=head)
    try:
        assert _place_with(app, monkeypatch, s3, key) is True
        assert s3.downloads == [key]
        assert s3.head_calls == 3
        with open(library_path, "rb") as f:
            assert f.read() == b"s3!"
    finally:
        if os.path.exists(library_path):
            os.remove(library_path)


def test_a_key_that_no_row_names_refuses_the_placement(app, monkeypatch):
    """A gone key with no successor on the row still refuses (#563 follow-up)."""

    import botocore.exceptions

    key, library_path = _direct_restore_row(app, "Gone During")
    gone = botocore.exceptions.ClientError(
        {"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject"
    )

    def head(call):
        if call == 1:
            return {"ContentLength": 3, "ETag": '"v1"'}
        raise gone

    s3 = S3(head=head)
    try:
        assert _place_with(app, monkeypatch, s3, key) is True
        assert not os.path.exists(library_path)
        staged = os.path.join(
            app.config["IMPORT_DIR"], f".restore-{key.replace('/', '-')}"
        )
        assert not os.path.exists(staged)
    finally:
        if os.path.exists(library_path):
            os.remove(library_path)


def test_an_edit_during_the_download_refuses_the_placement_by_its_stat(
    app, monkeypatch
):
    """The archive and the row can look unchanged after an edit (#586).

    The upload of the edit can still run, or it can go to the target key
    of a pending move. Then the ETag of the restored key is the same,
    and the old bytes replaced the edited library file."""

    from app import db
    from app.models import File

    key, library_path = _direct_restore_row(app, "Stat Changed")
    os.makedirs(os.path.dirname(library_path), exist_ok=True)
    with open(library_path, "wb") as f:
        f.write(b"library file")

    class EditingS3(S3):
        def download_file(self, bucket, key, filename, Callback=None):
            # A track edit rewrites the file on another worker. Its upload
            # did not finish. Thus, the row and the object stay as they were.
            with open(library_path, "wb") as f:
                f.write(b"edited library file")
            super().download_file(bucket, key, filename, Callback)

    s3 = EditingS3()
    try:
        assert _place_with(app, monkeypatch, s3, key) is True
        assert s3.downloads == [key]
        with open(library_path, "rb") as f:
            assert f.read() == b"edited library file"
        with app.app_context():
            assert not db.session.get(
                File, File.query.filter_by(aws_key=key).one().id
            ).aws_stale
        staged = os.path.join(
            app.config["IMPORT_DIR"], f".restore-{key.replace('/', '-')}"
        )
        assert not os.path.exists(staged)
    finally:
        os.remove(library_path)


def test_a_moved_key_with_other_bytes_refuses_the_placement(app, monkeypatch):
    """The key of the fresh row must hold a copy of the download (#586).

    An edit of a file with a pending move uploads the edited bytes to
    the target key and deletes the old key. The fallback accepted any
    object at the new key."""

    import botocore.exceptions

    from app import db
    from app.models import File

    key, library_path = _direct_restore_row(app, "Moved Edited")
    moved_key = "modified/Moved Edited Later.mkv"
    gone = botocore.exceptions.ClientError(
        {"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject"
    )

    def head(call):
        if call == 1:
            db.session.execute(
                db.update(File).where(File.aws_key == key).values(aws_key=moved_key)
            )
            db.session.commit()
            return {"ContentLength": 3, "ETag": '"v1"'}
        if call == 2:
            raise gone
        return {"ContentLength": 4, "ETag": '"v2"'}

    s3 = S3(head=head)
    try:
        assert _place_with(app, monkeypatch, s3, key) is True
        assert s3.head_calls == 3
        assert not os.path.exists(library_path)
    finally:
        if os.path.exists(library_path):
            os.remove(library_path)


def test_a_volume_wait_carries_the_stat_of_the_library_file(app, monkeypatch):
    """The next round compares with the stat from before the first download (#586)."""

    import app.videos as videos
    from app import aws_storage

    key, library_path = _direct_restore_row(app, "Stat Carried")
    os.makedirs(os.path.dirname(library_path), exist_ok=True)
    with open(library_path, "wb") as f:
        f.write(b"library file")
    answers = [[], ["/Volumes/Movies"]]
    monkeypatch.setattr(videos, "_dead_volumes", lambda paths: answers.pop(0))

    def download(key, basename, handle=None, place_at=None, before_place=None, **_):
        before_place()
        return aws_storage.DOWNLOAD_COMPLETE

    monkeypatch.setattr(aws_storage, "aws_download", download)
    try:
        with app.app_context():
            assert videos.download_task(key, os.path.basename(key)) is False
            expected = aws_storage._library_stat(library_path)
        (retry,) = _volume_retries(app)
        assert expected and retry.kwargs["library_stat"] == expected
        assert_binds(retry)
        retry.delete()
    finally:
        os.remove(library_path)


def test_a_row_deleted_during_the_download_refuses_and_drops_the_message(
    app, monkeypatch
):
    """The rollback expired the row, and its id raised ObjectDeletedError (#588).

    The error was no PlacementRefused. The SQS message stayed."""

    import app.videos as videos
    from app import aws_storage, db
    from app.models import File

    key, library_path = _direct_restore_row(app, "Deleted During")

    class DeletingS3(S3):
        def download_file(self, bucket, key, filename, Callback=None):
            # Another worker deletes the row. Its own connection keeps the
            # row of this session in the identity map.
            with db.engine.begin() as other:
                other.execute(db.delete(File).where(File.aws_key == key))
            super().download_file(bucket, key, filename, Callback)

    sqs = SQS()
    monkeypatch.setattr(aws_storage, "aws_s3_client", lambda **kwargs: DeletingS3())
    monkeypatch.setattr(aws_storage, "aws_sqs_client", lambda: sqs)
    monkeypatch.setattr(videos, "_dead_volumes", lambda paths: [])
    with app.app_context():
        assert videos.download_task(key, os.path.basename(key), "receipt-9") is True
    assert sqs.deleted == ["receipt-9"]
    assert not os.path.exists(library_path)


def test_a_transient_placement_fault_keeps_the_download_for_the_retry(app, monkeypatch):
    """The sidecar was written only on a PlacementDeferred (#588).

    A fault of the library volume during the copy then downloaded the
    whole object again on the retry."""

    import errno

    import app.videos as videos
    from app import aws_storage

    s3 = S3()
    monkeypatch.setattr(aws_storage, "aws_s3_client", lambda **kwargs: s3)
    monkeypatch.setattr(aws_storage, "aws_sqs_client", lambda: None)
    library = os.path.join(app.config["LIBRARY_DIR"], "Movies/z.mkv")
    staged = os.path.join(app.config["IMPORT_DIR"], ".restore-modified-z.mkv")
    real_place = aws_storage.place_in_library

    def volume_drop(*args):
        raise OSError(errno.EIO, "Input/output error")

    try:
        with app.app_context():
            with monkeypatch.context() as patch:
                patch.setattr(aws_storage, "place_in_library", volume_drop)
                with pytest.raises(OSError):
                    videos.aws_download(
                        "modified/z.mkv",
                        "z.mkv",
                        place_at=library,
                        before_place=lambda etag=None, size=None, **_: None,
                    )
            assert os.path.exists(staged) and os.path.exists(f"{staged}.etag")
            assert aws_storage.place_in_library is real_place
            os.makedirs(os.path.dirname(library), exist_ok=True)
            assert (
                videos.aws_download(
                    "modified/z.mkv",
                    "z.mkv",
                    place_at=library,
                    before_place=lambda etag=None, size=None, **_: None,
                )
                == aws_storage.DOWNLOAD_COMPLETE
            )
        assert s3.downloads == ["modified/z.mkv"]
        assert open(library, "rb").read() == b"s3!"
    finally:
        for path in (staged, f"{staged}.etag", library):
            if os.path.exists(path):
                os.remove(path)


@pytest.mark.parametrize("stop", ["stale", "give-up"])
def test_an_early_stop_removes_the_kept_download(app, monkeypatch, stop):
    """A copy that a deferred round kept stayed for ever (#588).

    The orphan sweep keeps a complete copy with its sidecar."""

    import app.videos as videos
    from app import aws_storage, db
    from app.models import File

    key, library_path = _direct_restore_row(app, f"Kept Copy {stop}")
    staged = os.path.join(app.config["IMPORT_DIR"], f".restore-{key.replace('/', '-')}")
    os.makedirs(os.path.dirname(staged), exist_ok=True)
    with open(staged, "wb") as f:
        f.write(b"s3!")
    with open(f"{staged}.etag", "w") as f:
        f.write('"v1"\n3')
    kwargs = {}
    if stop == "stale":
        os.makedirs(os.path.dirname(library_path), exist_ok=True)
        with open(library_path, "wb") as f:
            f.write(b"newer")
        with app.app_context():
            db.session.execute(
                db.update(File).where(File.aws_key == key).values(aws_stale=True)
            )
            db.session.commit()
    else:
        monkeypatch.setattr(videos, "_dead_volumes", lambda paths: ["/Volumes/Movies"])
        kwargs["volume_waits"] = aws_storage.MAX_VOLUME_WAITS
    monkeypatch.setattr(aws_storage, "aws_sqs_client", lambda: None)
    try:
        with app.app_context():
            videos.download_task(key, os.path.basename(key), **kwargs)
        assert not os.path.exists(staged)
        assert not os.path.exists(f"{staged}.etag")
    finally:
        for path in (staged, f"{staged}.etag", library_path):
            if os.path.exists(path):
                os.remove(path)


def test_the_sqs_delete_lets_a_base_exception_through(app):
    """A bare except swallowed a shutdown during the delete (#588)."""

    from app import aws_storage

    class Stopping:
        def delete_message(self, **kwargs):
            raise SystemExit(1)

    with app.app_context():
        with pytest.raises(SystemExit):
            aws_storage.delete_sqs_message(Stopping(), "receipt")


def test_a_share_fault_at_the_placement_stat_defers_and_keeps_the_download(
    app, monkeypatch
):
    """An ESTALE at the stat of the placement discarded the download (#586 follow-up).

    The stat ran after the volume probe passed. Its fault left before_place
    as a plain OSError, the finished copy was removed, and the transient
    retry downloaded the whole object again."""

    import errno

    from app import aws_storage

    key, library_path = _direct_restore_row(app, "Stat Blip")
    real = aws_storage._library_stat
    calls = []

    def stat(path):
        calls.append(path)
        if len(calls) == 2:
            raise OSError(errno.ESTALE, "Stale file handle")
        return real(path)

    monkeypatch.setattr(aws_storage, "_library_stat", stat)
    s3 = S3(head={"ContentLength": 3, "ETag": '"v1"'})
    staged = os.path.join(app.config["IMPORT_DIR"], f".restore-{key.replace('/', '-')}")
    try:
        assert _place_with(app, monkeypatch, s3, key) is False
        assert s3.downloads == [key]
        assert os.path.exists(staged)
        assert os.path.exists(f"{staged}.etag")
        assert not os.path.exists(library_path)
        (retry,) = _volume_retries(app)
        assert retry.kwargs["volume_waits"] == 1
        retry.delete()
    finally:
        for path in (staged, f"{staged}.etag", library_path):
            if os.path.exists(path):
                os.remove(path)


def test_a_delivered_delete_clears_the_stored_handle_of_the_job(app, monkeypatch):
    """A stale stored handle shadowed the handle of a later chain (#587 follow-up).

    The move retry ids are fixed per file, and the stored handle lived
    14 days. The next chain of the same file deleted with the old handle,
    and its own message came back."""

    from types import SimpleNamespace

    from app import aws_storage

    sqs = SQS()
    monkeypatch.setattr(
        aws_storage.rq, "get_current_job", lambda: SimpleNamespace(id="chain-job")
    )
    handle_key = aws_storage.SQS_HANDLE_KEY.format(job_id="chain-job")
    app.redis.set(handle_key, "stored-handle")
    try:
        with app.app_context():
            assert aws_storage.delete_sqs_message(sqs, "kwarg-handle") is True
        assert sqs.deleted == ["stored-handle"]
        assert app.redis.get(handle_key) is None
        with app.app_context():
            assert aws_storage.delete_sqs_message(sqs, "kwarg-handle") is True
        assert sqs.deleted == ["stored-handle", "kwarg-handle"]
    finally:
        app.redis.delete(handle_key)


def _kept_copy(app, key, sidecar):
    """Write a finished copy of key and its sidecar into staging (#589)."""

    staged = os.path.join(app.config["IMPORT_DIR"], f".restore-{key.replace('/', '-')}")
    os.makedirs(os.path.dirname(staged), exist_ok=True)
    with open(staged, "wb") as f:
        f.write(b"s3!")
    with open(f"{staged}.etag", "w") as f:
        f.write(sidecar)
    return staged


def test_a_kept_copy_from_a_broken_chain_keeps_its_stat(app, monkeypatch):
    """A new chain took the stat of the edited file, and the kept copy reverted it (#589).

    A job timeout left the copy. The edit then changed the library file
    while its upload still ran. The ETag of the key was unchanged."""

    import json

    from app import aws_storage

    key, library_path = _direct_restore_row(app, "Broken Chain")
    os.makedirs(os.path.dirname(library_path), exist_ok=True)
    with open(library_path, "wb") as f:
        f.write(b"library file")
    with app.app_context():
        before = aws_storage._library_stat(library_path)
    staged = _kept_copy(app, key, f'"v1"\n3\n{json.dumps(before)}')
    with open(library_path, "wb") as f:
        f.write(b"edited library file")
    s3 = S3(head={"ContentLength": 3, "ETag": '"v1"'})
    try:
        assert _place_with(app, monkeypatch, s3, key) is True
        assert s3.downloads == []
        with open(library_path, "rb") as f:
            assert f.read() == b"edited library file"
        assert not os.path.exists(staged)
        assert not os.path.exists(f"{staged}.etag")
    finally:
        for path in (staged, f"{staged}.etag", library_path):
            if os.path.exists(path):
                os.remove(path)


def test_a_new_download_writes_the_stat_into_the_sidecar(app, monkeypatch):
    """The sidecar holds the stat from the caller beside the ETag and the size (#589)."""

    import app.videos as videos
    from app import aws_storage

    s3 = S3(head={"ContentLength": 3, "ETag": '"v1"'})
    monkeypatch.setattr(aws_storage, "aws_s3_client", lambda **kwargs: s3)
    monkeypatch.setattr(aws_storage, "aws_sqs_client", lambda: None)
    library = os.path.join(app.config["LIBRARY_DIR"], "Movies/w.mkv")
    staged = os.path.join(app.config["IMPORT_DIR"], ".restore-modified-w.mkv")
    seen = []

    def defer(etag=None, size=None, library_stat=None):
        seen.append(library_stat)
        raise aws_storage.PlacementDeferred(["/Volumes/Movies"])

    try:
        with app.app_context():
            with pytest.raises(aws_storage.PlacementDeferred):
                videos.aws_download(
                    "modified/w.mkv",
                    "w.mkv",
                    place_at=library,
                    before_place=defer,
                    library_stat=[12, 345, 678],
                )
            assert aws_storage.read_partial_sidecar(staged) == (
                '"v1"',
                3,
                [12, 345, 678],
            )

            # The next round reuses the copy. Its sidecar stat goes to
            # before_place.
            with pytest.raises(aws_storage.PlacementDeferred):
                videos.aws_download(
                    "modified/w.mkv",
                    "w.mkv",
                    place_at=library,
                    before_place=defer,
                    library_stat=[99, 99, 99],
                )
        assert s3.downloads == ["modified/w.mkv"]
        assert seen == [None, [12, 345, 678]]
        with app.app_context():
            assert aws_storage.read_partial_sidecar(staged)[2] == [12, 345, 678]
    finally:
        for path in (staged, f"{staged}.etag"):
            if os.path.exists(path):
                os.remove(path)


@pytest.mark.parametrize(
    "text, expected",
    [
        ('"v1"', ('"v1"', None, None)),
        ('"v1"\n3', ('"v1"', 3, None)),
        ('"v1"\n3\n', ('"v1"', 3, None)),
        ('"v1"\n3\n[1, 2, 3]', ('"v1"', 3, [1, 2, 3])),
        ('"v1"\n\n[]', ('"v1"', None, [])),
        ('"v1"\n3\nnot json', ('"v1"', 3, None)),
        ('"v1"\n3\n{"a": 1}', ('"v1"', 3, None)),
    ],
)
def test_an_older_sidecar_reads_with_no_stat(app, text, expected):
    """A sidecar of 1 or 2 lines predates the stat line (#589)."""

    from app import aws_storage

    destination = os.path.join(app.config["IMPORT_DIR"], ".restore-modified-old.mkv")
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    with open(f"{destination}.etag", "w") as f:
        f.write(text)
    try:
        with app.app_context():
            assert aws_storage.read_partial_sidecar(destination) == expected
    finally:
        os.remove(f"{destination}.etag")


def test_the_sidecar_stat_survives_a_write_and_a_read(app):
    """An empty stat means no library file. It must not read as no stat (#589)."""

    from app import aws_storage

    destination = os.path.join(app.config["IMPORT_DIR"], ".restore-modified-rt.mkv")
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    try:
        with app.app_context():
            for size, stat in ((3, []), (None, [1, 2, 3]), (3, None), (None, None)):
                aws_storage._write_partial_etag(destination, '"v1"', size, stat)
                assert aws_storage.read_partial_sidecar(destination) == (
                    '"v1"',
                    size,
                    stat,
                )
    finally:
        if os.path.exists(f"{destination}.etag"):
            os.remove(f"{destination}.etag")


def test_a_deferred_placement_refuses_after_an_edit_in_the_wait(app, monkeypatch):
    """The retry reuses the kept copy and compares with its sidecar stat (#589)."""

    import app.videos as videos
    from app import aws_storage

    key, library_path = _direct_restore_row(app, "Edited In Wait")
    os.makedirs(os.path.dirname(library_path), exist_ok=True)
    with open(library_path, "wb") as f:
        f.write(b"library file")
    s3 = S3(head={"ContentLength": 3, "ETag": '"v1"'})
    monkeypatch.setattr(aws_storage, "aws_s3_client", lambda **kwargs: s3)
    monkeypatch.setattr(aws_storage, "aws_sqs_client", lambda: None)
    answers = [[], ["/Volumes/Movies"]]
    monkeypatch.setattr(
        videos, "_dead_volumes", lambda paths: answers.pop(0) if answers else []
    )
    staged = os.path.join(app.config["IMPORT_DIR"], f".restore-{key.replace('/', '-')}")
    try:
        with app.app_context():
            before = aws_storage._library_stat(library_path)
            assert videos.download_task(key, os.path.basename(key)) is False
            assert aws_storage.read_partial_sidecar(staged)[2] == before
        (retry,) = _volume_retries(app)
        kwargs = dict(retry.kwargs)
        retry.delete()
        assert kwargs["library_stat"] == before

        with open(library_path, "wb") as f:
            f.write(b"edited library file")
        with app.app_context():
            assert videos.download_task(key, os.path.basename(key), **kwargs) is True
        assert s3.downloads == [key]
        with open(library_path, "rb") as f:
            assert f.read() == b"edited library file"
        assert not os.path.exists(staged)
    finally:
        for path in (staged, f"{staged}.etag", library_path):
            if os.path.exists(path):
                os.remove(path)


def test_a_transient_fault_before_the_placement_carries_the_kept_stat(app, monkeypatch):
    """The retry took the stat of the edited file after an EIO (#597).

    A partial of an earlier chain holds the stat from before the edit. A
    transient fault removed the partial before before_place ran. The
    retry then downloaded fresh, compared the new stat with itself, and
    placed the archive over the edit."""

    import errno
    import json

    import app.videos as videos
    from app import aws_storage

    key, library_path = _direct_restore_row(app, "Kept Stat Fault")
    os.makedirs(os.path.dirname(library_path), exist_ok=True)
    with open(library_path, "wb") as f:
        f.write(b"library file")
    with app.app_context():
        before = aws_storage._library_stat(library_path)
    staged = _kept_copy(app, key, f'"v1"\n3\n{json.dumps(before)}')
    with open(library_path, "wb") as f:
        f.write(b"edited library file")

    def lost(*args, **kwargs):
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(aws_storage, "aws_download", lost)
    monkeypatch.setattr(videos, "_dead_volumes", lambda paths: [])
    try:
        with app.app_context():
            assert videos.download_task(key, os.path.basename(key)) is False
        (retry,) = [
            job
            for job in scheduled_jobs(app.file_queue)
            if job.id.startswith(safe_job_id("retry:download_task"))
        ]
        assert retry.kwargs["library_stat"] == before
        retry.delete()
    finally:
        for path in (staged, f"{staged}.etag", library_path):
            if os.path.exists(path):
                os.remove(path)


def _slow_down():
    return _client_error("SlowDown", 503, "HeadObject")


def _lost_endpoint():
    import botocore.exceptions

    return botocore.exceptions.EndpointConnectionError(endpoint_url="https://s3")


@pytest.mark.parametrize("fault", [_slow_down, _lost_endpoint])
def test_a_transient_fault_of_a_placement_check_keeps_the_download(
    app, monkeypatch, fault
):
    """A throttled HEAD discarded a finished restore download (#598)."""

    key, library_path = _direct_restore_row(app, f"Placement Fault {fault.__name__}")
    staged = os.path.join(app.config["IMPORT_DIR"], f".restore-{key.replace('/', '-')}")

    def head(call):
        if call == 2:
            raise fault()
        return {"ContentLength": 3, "ETag": '"v1"'}

    s3 = S3(head=head)
    try:
        assert _place_with(app, monkeypatch, s3, key) is False
        assert s3.downloads == [key]
        assert os.path.exists(staged) and os.path.exists(f"{staged}.etag")
        assert not os.path.exists(library_path)
        (retry,) = _volume_retries(app)
        assert retry.kwargs["library_stat"] == []
        retry.delete()
    finally:
        for path in (staged, f"{staged}.etag"):
            if os.path.exists(path):
                os.remove(path)


def test_an_access_fault_of_a_placement_check_still_discards(app, monkeypatch):
    """A 403 does not clear by itself. The deferral is only for a transient fault."""

    key, library_path = _direct_restore_row(app, "Placement Denied")
    staged = os.path.join(app.config["IMPORT_DIR"], f".restore-{key.replace('/', '-')}")

    def head(call):
        if call == 2:
            raise _client_error("AccessDenied", 403, "HeadObject")
        return {"ContentLength": 3, "ETag": '"v1"'}

    assert _place_with(app, monkeypatch, S3(head=head), key) is None
    assert not os.path.exists(staged)
    assert _volume_retries(app) == []


def test_a_lost_database_at_a_placement_check_keeps_the_download(app, monkeypatch):
    """A MariaDB restart at the read of the row discarded the download (#598)."""

    import sqlalchemy.exc

    from app import db

    key, library_path = _direct_restore_row(app, "Placement Db Lost")
    staged = os.path.join(app.config["IMPORT_DIR"], f".restore-{key.replace('/', '-')}")
    real_get = db.session.get
    calls = []

    def lost_get(*args, **kwargs):
        calls.append(args)
        raise sqlalchemy.exc.OperationalError("SELECT", {}, Exception("gone away"))

    class LosingS3(S3):
        def download_file(self, *args, **kwargs):
            super().download_file(*args, **kwargs)
            monkeypatch.setattr(db.session, "get", lost_get)

    try:
        assert _place_with(app, monkeypatch, LosingS3(), key) is False
        monkeypatch.setattr(db.session, "get", real_get)
        assert calls
        assert os.path.exists(staged) and os.path.exists(f"{staged}.etag")
        (retry,) = _volume_retries(app)
        retry.delete()
    finally:
        for path in (staged, f"{staged}.etag"):
            if os.path.exists(path):
                os.remove(path)
