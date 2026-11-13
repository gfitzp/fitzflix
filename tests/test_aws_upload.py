"""Test the archive upload of a library file and its stale mark.

A failed upload never moves the library file. The stale mark makes the
weekly S3 sync try the archive again.
"""

import errno
import os

import pytest


def test_the_stale_mark_survives_the_rollback_that_follows_it(app):
    """Make sure the stale mark survives the rollback that follows it.

    Each caller is a failure path that rolls back. If the rollback
    removed the marker, the loss would stay hidden as before."""

    from app import db
    from app.aws_storage import mark_archive_stale
    from app.models import File
    from tests.factories import make_movie, make_movie_file

    with app.app_context():
        movie = make_movie("Rolled Back", 2021)
        file = make_movie_file(movie, "Bluray-1080p")
        db.session.commit()
        file_id = file.id

        assert mark_archive_stale(file_id, reason="test") is True
        db.session.rollback()

        db.session.expire_all()
        assert File.query.get(file_id).aws_stale is True


def test_a_successful_upload_clears_the_stale_mark(app, monkeypatch):
    """Make sure a successful upload clears the stale mark.

    If not, the repair queue never empties."""

    from datetime import datetime, timezone

    from app import db
    from app.models import File
    from tests.factories import make_movie, make_movie_file

    import app.aws_storage as aws_storage

    with app.app_context():
        movie = make_movie("Repaired", 2021)
        file = make_movie_file(movie, "Bluray-1080p")
        file.aws_key = "untouched/repaired.mkv"
        file.aws_stale = True
        db.session.commit()
        file_id = file.id

        monkeypatch.setattr(
            aws_storage,
            "aws_upload",
            lambda *args, **kwargs: (
                "modified/repaired.mkv",
                datetime.now(timezone.utc),
                123,
            ),
        )

        assert aws_storage.upload_task(file_id) is True

        db.session.expire_all()
        assert File.query.get(file_id).aws_stale is False


def test_a_transient_mount_error_does_not_reject_the_file(app, tmp_path, monkeypatch):
    """Make sure a transient mount error does not reject the file.

    A reject would move a library file out of the library because of a
    problem that clears on its own. This is exactly how the lost-handle
    state arrives: from inside the close of s3transfer."""

    import app.aws_storage as aws_storage

    path = str(tmp_path / "transient.mkv")
    open(path, "wb").write(b"x")

    class ExplodingClient:
        def head_object(self, **kwargs):
            from tests.fakes import missing_object

            raise missing_object()

        def upload_file(self, *args, **kwargs):
            raise OSError(errno.EBADF, "Bad file descriptor")

    monkeypatch.setattr(aws_storage, "aws_s3_client", lambda **kw: ExplodingClient())

    monkeypatch.setitem(app.config, "AWS_BUCKET", "test-bucket")

    with app.app_context():
        with pytest.raises(OSError):
            aws_storage.aws_upload(path, "untouched")

        # The upload never moves the file. The caller decides.
        assert os.path.exists(path)


@pytest.mark.parametrize(
    "error",
    [
        "s3_failed",
        "connection",
    ],
)
def test_a_failed_upload_never_moves_the_file(app, monkeypatch, tmp_path, error):
    """An S3 error or a lost connection must not move a library file to rejects."""

    import boto3
    import botocore.exceptions

    import app.aws_storage as aws_storage

    path = tmp_path / "library.mkv"
    path.write_bytes(b"x")

    uploads = []

    class FailingClient:
        def head_object(self, **kwargs):
            from tests.fakes import missing_object

            raise missing_object()

        def upload_file(self, *args, **kwargs):
            uploads.append(args)
            if error == "s3_failed":
                raise boto3.exceptions.S3UploadFailedError("RequestTimeout")
            raise botocore.exceptions.EndpointConnectionError(endpoint_url="s3")

    monkeypatch.setattr(aws_storage, "aws_s3_client", lambda **kw: FailingClient())
    monkeypatch.setitem(app.config, "AWS_BUCKET", "test-bucket")

    # Only the S3 errors may come out (#333). Any other error means that
    # the function stopped before the upload, and the test proves nothing.

    with app.app_context():
        with pytest.raises(
            (
                boto3.exceptions.S3UploadFailedError,
                botocore.exceptions.EndpointConnectionError,
            )
        ):
            aws_storage.aws_upload(str(path), "modified")

    assert len(uploads) == 1
    assert path.exists()


def test_the_etag_uses_the_part_size_of_the_upload(app, monkeypatch, tmp_path):
    """s3transfer doubles the part size above its part limit. The ETag must too."""

    import hashlib

    from s3transfer.utils import ChunksizeAdjuster

    import app.aws_storage as aws_storage

    mib = 1024 * 1024
    path = tmp_path / "large.mkv"
    path.write_bytes(bytes(range(256)) * (24 * mib // 256))

    # A limit of 2 parts makes 24 MiB need 16 MiB parts, as 10,000 parts
    # does for a file of more than 78 GiB.

    monkeypatch.setattr(
        aws_storage, "ChunksizeAdjuster", lambda: ChunksizeAdjuster(max_parts=2)
    )
    data = path.read_bytes()
    parts = [data[: 16 * mib], data[16 * mib :]]
    expected = (
        hashlib.md5(b"".join(hashlib.md5(part).digest() for part in parts)).hexdigest()
        + "-2"
    )
    with app.app_context():
        assert aws_storage.calculate_etag(str(path)) == expected


def _worker_upload(app, file_id):
    """Run upload_task in a PipelineWorker. Return the job and the chip statuses."""

    from app.pipeline import PipelineWorker, pipeline_trails

    with app.app_context():
        job = app.file_queue.enqueue("app.videos.upload_task", file_id)
    PipelineWorker([app.file_queue], connection=app.redis).work(burst=True)
    (trail,) = pipeline_trails(app.redis)
    return job, [(entry["stage"], entry["status"]) for entry in trail["entries"]]


def _stale_file(title, **fields):
    """Make a stale movie file with a modified/ archive. Return its id."""

    from app import db
    from tests.factories import make_movie, make_movie_file

    file = make_movie_file(
        make_movie(title, 2026),
        "DVD",
        aws_key=f"modified/{title} (2026) - [DVD].mkv",
        aws_stale=True,
        **fields,
    )
    db.session.commit()
    return file.id


def test_a_good_upload_in_the_worker_stamps_its_chip_done(app, monkeypatch):
    """The green chip means that the archive is in S3 (#766)."""

    from datetime import datetime, timezone

    from rq.job import JobStatus

    monkeypatch.setattr(
        "app.aws_storage.aws_upload",
        lambda *args, **kwargs: (
            "modified/Chip Good (2026) - [DVD].mkv",
            datetime.now(timezone.utc),
            123,
        ),
    )
    with app.app_context():
        file_id = _stale_file("Chip Good")

    job, chips = _worker_upload(app, file_id)

    assert chips == [("Archiving to S3", "done")]
    assert job.get_status(refresh=True) == JobStatus.FINISHED


def test_an_upload_that_raises_fails_its_job_and_its_chip(app, monkeypatch):
    """An upload that raised is a failed job with a red chip (#766)."""

    from rq.registry import FailedJobRegistry

    from app import db
    from app.models import File

    def failing_upload(*args, **kwargs):
        raise OSError("drill")

    monkeypatch.setattr("app.aws_storage.aws_upload", failing_upload)
    with app.app_context():
        file_id = _stale_file("Chip Raises")

    job, chips = _worker_upload(app, file_id)

    assert chips == [("Archiving to S3", "failed")]
    assert job.id in FailedJobRegistry(queue=app.file_queue).get_job_ids()
    with app.app_context():
        assert db.session.get(File, file_id).aws_stale is True


def test_a_refused_upload_does_not_show_done(app, monkeypatch):
    """A pending restore refuses the upload. The chip reads did not run (#766).

    The refusal is not a failure. The job does not fail, and nothing
    logs at ERROR."""

    import logging
    from datetime import datetime

    from rq.job import JobStatus

    uploads = []
    monkeypatch.setattr(
        "app.aws_storage.aws_upload", lambda *args, **kwargs: uploads.append(args)
    )
    with app.app_context():
        file_id = _stale_file("Chip Refused", restore_requested_at=datetime.utcnow())

    records = []
    handler = logging.Handler(level=logging.ERROR)
    handler.emit = records.append
    logging.getLogger("app").addHandler(handler)
    try:
        job, chips = _worker_upload(app, file_id)
    finally:
        logging.getLogger("app").removeHandler(handler)

    assert uploads == []
    assert chips == [("Archiving to S3", "skipped")]
    assert job.get_status(refresh=True) == JobStatus.FINISHED
    assert records == []

    with app.app_context():
        chip = app.jinja_env.get_template("_trail_chips.html").module.trail_chips(
            [{"stage": "Archiving to S3", "status": "skipped", "at": "now"}]
        )
    assert "Archiving to S3 &middot; did not run" in str(chip)
    assert "Archived to S3" not in str(chip)
    assert "text-bg-success" not in str(chip)
