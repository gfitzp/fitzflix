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


def test_the_upload_of_a_stale_file_stores_the_size_of_an_edit_that_died(
    app, tmp_path, monkeypatch
):
    """An edit that died after its write left the old size and the stale flag (#750).

    The upload clears the flag. A record with no flag must have the
    size of its file. If not, the weekly sync reads the file of the
    edit as a file from outside and imports it. The track rows are old
    too, and a scan reads them again."""

    from datetime import datetime, timezone

    from app import db
    from app.models import File
    from tests.factories import make_movie, make_movie_file

    import app.aws_storage as aws_storage
    import app.tracks as tracks

    monkeypatch.setitem(app.config, "LIBRARY_DIR", str(tmp_path))
    scans = []
    monkeypatch.setattr(
        tracks,
        "_rescan_after_a_failed_edit",
        lambda file_id, *a, **k: scans.append(file_id),
    )
    monkeypatch.setattr(
        aws_storage,
        "aws_upload",
        lambda *args, **kwargs: ("modified/died.mkv", datetime.now(timezone.utc), 6),
    )
    with app.app_context():
        died = make_movie_file(
            make_movie("Edit Died Upload", 2021),
            "Bluray-1080p",
            aws_stale=True,
            filesize_bytes=999,
        )
        whole = make_movie_file(
            make_movie("Edit Whole Upload", 2021),
            "Bluray-1080p",
            aws_stale=True,
            filesize_bytes=6,
        )
        db.session.commit()
        ids = died.id, whole.id
        for file in (died, whole):
            path = tmp_path / file.file_path
            path.parent.mkdir(parents=True)
            path.write_bytes(b"edited")

        for file_id in ids:
            assert aws_storage.upload_task(file_id) is True

        db.session.expire_all()
        for file_id in ids:
            row = db.session.get(File, file_id)
            assert row.aws_stale is False
            assert row.filesize_bytes == 6
        assert scans == [ids[0]]


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
