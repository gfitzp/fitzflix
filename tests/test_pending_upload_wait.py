"""Test that a re-rip upload survives a long wait for library space (#455).

A same-name re-rip uploads below pending-untouched/ (#430). No record
claims that key until finalize. The wait for library space comes after
the upload and has no limit. The weekly S3 sync must keep the upload,
and a move that finds no upload must archive the library file."""

import os
from datetime import datetime, timedelta, timezone

from tests.factories import make_movie, make_movie_file
from tests.fakes import ArchiveS3

BASENAME = "Long Rerip (2024) - [DVD].mkv"
PENDING = f"pending-untouched/run1/{BASENAME}"


def test_a_copy_that_waits_for_space_marks_its_pending_upload(
    app, incoming_dir, monkeypatch
):
    """The space-wait marker of the library copy holds the run-scoped key."""

    from app import importing, videos

    source = os.path.join(incoming_dir, BASENAME)
    with open(source, "wb") as f:
        f.write(b"untouched source")
    hidden = os.path.join(app.config["STAGING_DIR"], f".{BASENAME}")
    os.makedirs(app.config["STAGING_DIR"], exist_ok=True)
    with open(hidden, "wb") as f:
        f.write(b"localized output")
    details = {
        "basename": BASENAME,
        "dirname": "Movies/Long Rerip (2024)",
        "container": "Matroska",
        "aws_key": PENDING,
    }
    monkeypatch.setattr(importing, "_crosses_volumes", lambda *args: True)
    monkeypatch.setattr(importing, "_free_bytes", lambda path: 0)
    try:
        with app.app_context():
            lock = app.lock_manager.lock("long-rerip-title", 60000)
            videos.move_localized_file(source, details, lock, hidden)
            marker = app.redis.get(importing.SPACE_WAIT_KEY.format(BASENAME))
            assert marker == PENDING.encode()
    finally:
        app.redis.delete(importing.SPACE_WAIT_KEY.format(BASENAME))
        os.remove(source)
        os.remove(hidden)


def test_the_prune_keeps_the_pending_upload_of_a_waiting_import(app, monkeypatch):
    """A marker that holds a pending key protects that key from the prune."""

    from app import aws_storage
    from app.importing import SPACE_WAIT_KEY

    old = datetime.now(timezone.utc) - timedelta(days=30)
    stray = "pending-untouched/run2/Old Stray (2020) - [DVD].mkv"
    objects = {
        "pending-untouched/": [
            {"Key": PENDING, "Size": 5, "LastModified": old},
            {"Key": stray, "Size": 5, "LastModified": old},
        ],
    }
    monkeypatch.setattr(
        aws_storage,
        "get_matching_s3_objects",
        lambda bucket, prefix="": objects.get(prefix, []),
    )
    deletes = []
    monkeypatch.setattr(aws_storage, "aws_delete", lambda key: deletes.append(key))
    monkeypatch.setattr(aws_storage, "aws_restore", lambda key, **kw: None)
    monkeypatch.setattr("app.email.send_email", lambda *a, **k: None)

    class FakeClient:
        def put_object(self, **kwargs):
            pass

    monkeypatch.setattr(aws_storage, "aws_s3_client", lambda **kw: FakeClient())
    app.redis.set(SPACE_WAIT_KEY.format(BASENAME), PENDING)
    try:
        aws_storage.sync_aws_s3_storage_task()
    finally:
        app.redis.delete(SPACE_WAIT_KEY.format(BASENAME))

    assert deletes == [stray]


def test_a_move_whose_pending_upload_is_gone_archives_the_library_file(
    app, monkeypatch, tmp_path
):
    """The upload expired. The library file goes to modified/, and the row names it."""

    from app import aws_storage, db
    from app.models import File

    fake = ArchiveS3({})
    monkeypatch.setattr(aws_storage, "aws_s3_client", lambda **kw: fake)
    monkeypatch.setitem(app.config, "AWS_BUCKET", "test-bucket")
    monkeypatch.setitem(app.config, "LIBRARY_DIR", str(tmp_path))
    with app.app_context():
        file = make_movie_file(make_movie("Long Rerip", 2024), "DVD")
        file.untouched_basename = BASENAME
        file.aws_key = PENDING
        file.aws_key_target = f"untouched/{BASENAME}"
        file.aws_date_uploaded = datetime(2026, 1, 1)
        db.session.commit()
        file_id = file.id
        path = os.path.join(str(tmp_path), file.file_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(b"library bytes")

        assert aws_storage.move_archive_object(file_id) is True

        db.session.expire_all()
        row = db.session.get(File, file_id)
        assert row.aws_key.startswith("modified/")
        assert row.aws_key in fake.objects
        assert row.aws_key_target is None


def test_a_missing_pending_upload_with_no_library_file_stops_the_move(
    app, monkeypatch, tmp_path
):
    """Without a library file, the move stops as before, and the sync reports the key."""

    from app import aws_storage, db
    from app.models import File

    fake = ArchiveS3({})
    monkeypatch.setattr(aws_storage, "aws_s3_client", lambda **kw: fake)
    monkeypatch.setitem(app.config, "AWS_BUCKET", "test-bucket")
    monkeypatch.setitem(app.config, "LIBRARY_DIR", str(tmp_path))
    with app.app_context():
        file = make_movie_file(make_movie("Gone Rerip", 2024), "DVD")
        file.untouched_basename = BASENAME
        file.aws_key = PENDING
        file.aws_key_target = f"untouched/{BASENAME}"
        file.aws_date_uploaded = datetime(2026, 1, 1)
        db.session.commit()
        file_id = file.id

        assert aws_storage.move_archive_object(file_id) is False

        db.session.expire_all()
        row = db.session.get(File, file_id)
        assert row.aws_key == PENDING
        assert row.aws_key_target is None
        assert fake.uploads == []


def test_the_prune_keeps_the_upload_of_a_scheduled_conversion_retry(app, monkeypatch):
    """A conversion retry has no marker. Its file_details key is safe (#493)."""

    from app import aws_storage

    old = datetime.now(timezone.utc) - timedelta(days=30)
    parked = "pending-untouched/run3/Parked Rerip (2024) - [UHD].mkv"
    stray = "pending-untouched/run2/Old Stray (2020) - [DVD].mkv"
    objects = {
        "pending-untouched/": [
            {"Key": parked, "Size": 5, "LastModified": old},
            {"Key": stray, "Size": 5, "LastModified": old},
        ],
    }
    monkeypatch.setattr(
        aws_storage,
        "get_matching_s3_objects",
        lambda bucket, prefix="": objects.get(prefix, []),
    )
    deletes = []
    monkeypatch.setattr(aws_storage, "aws_delete", lambda key: deletes.append(key))
    monkeypatch.setattr(aws_storage, "aws_restore", lambda key, **kw: None)
    monkeypatch.setattr("app.email.send_email", lambda *a, **k: None)

    class FakeClient:
        def put_object(self, **kwargs):
            pass

    monkeypatch.setattr(aws_storage, "aws_s3_client", lambda **kw: FakeClient())
    with app.app_context():
        app.transcode_queue.enqueue_in(
            timedelta(minutes=30),
            "app.videos.convert_localized_file",
            "/import/Parked Rerip (2024) - [UHD].mkv",
            {"basename": "Parked Rerip (2024) - [UHD].mkv", "aws_key": parked},
            "lock",
            "/staging/.Parked Rerip (2024) - [UHD].mkv",
            {},
        )
    aws_storage.sync_aws_s3_storage_task()

    assert deletes == [stray]


def test_the_prune_keeps_the_untouched_upload_of_a_rejected_import(app, monkeypatch):
    """A failed in-place import keeps its direct untouched/ upload (#496)."""

    from app import aws_storage

    old = datetime.now(timezone.utc) - timedelta(days=30)
    name = "Rejected In Place (2024) - [UHD].mkv"
    kept = f"untouched/{name}"
    stray = "untouched/Old Untouched Stray (2020) - [DVD].mkv"
    objects = {
        "untouched/": [
            {"Key": kept, "Size": 5, "LastModified": old},
            {"Key": stray, "Size": 5, "LastModified": old},
        ],
    }
    monkeypatch.setattr(
        aws_storage,
        "get_matching_s3_objects",
        lambda bucket, prefix="": objects.get(prefix, []),
    )
    deletes = []
    monkeypatch.setattr(aws_storage, "aws_delete", lambda key: deletes.append(key))
    monkeypatch.setattr(aws_storage, "aws_restore", lambda key, **kw: None)
    monkeypatch.setattr("app.email.send_email", lambda *a, **k: None)

    class FakeClient:
        def put_object(self, **kwargs):
            pass

    monkeypatch.setattr(aws_storage, "aws_s3_client", lambda **kw: FakeClient())
    rejected = os.path.join(app.config["REJECTS_DIR"], "exception")
    os.makedirs(rejected, exist_ok=True)
    with open(os.path.join(rejected, name), "wb") as f:
        f.write(b"rewritten source")
    aws_storage.sync_aws_s3_storage_task()

    assert deletes == [stray]


def test_a_direct_restore_never_replaces_a_newer_library_file(app, monkeypatch):
    """A stale row keeps its library file. The restore stops (#496)."""

    from app import aws_storage, db

    with app.app_context():
        file = make_movie_file(
            make_movie("Newer Local", 2024),
            "DVD",
            aws_key="modified/Newer Local (2024) - [DVD].mkv",
            aws_date_uploaded=datetime(2026, 1, 1),
            aws_stale=True,
        )
        db.session.commit()
        path = os.path.join(app.config["LIBRARY_DIR"], file.file_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(b"newer bytes")

        def no_download(*args, **kwargs):
            raise AssertionError("the stale restore downloaded")

        monkeypatch.setattr(aws_storage, "aws_download", no_download)
        assert aws_storage.download_task(file.aws_key, file.basename) is True
        assert open(path, "rb").read() == b"newer bytes"
