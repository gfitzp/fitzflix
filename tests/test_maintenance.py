"""Log rotation and database backup: archive naming, same-day collisions,
retention pruning, and the no-partial-backup failure guarantee.

backup_database runs against a stub mysqldump script, so the gzip/prune
plumbing is tested without a MariaDB server.
"""

import glob
import gzip
import os
import time

import pytest

from app.maintenance import backup_database, rotate_logs


def set_age(path, days):
    stamp = time.time() - days * 86400
    os.utime(path, (stamp, stamp))


def test_rotate_logs_archives_and_prunes(app):
    log_file = app.config["LOG_FILE"]
    with open(log_file, "w") as f:
        f.write("a log line\n")

    stale = f"{log_file}.2020-01-01.gz"
    with gzip.open(stale, "wb") as f:
        f.write(b"ancient")
    set_age(stale, days=30)

    with app.app_context():
        rotate_logs()

    archives = glob.glob(f"{log_file}.*.gz")
    assert len(archives) == 1, archives
    assert not os.path.exists(stale)
    with gzip.open(archives[0], "rb") as f:
        assert f.read() == b"a log line\n"

    # Same-day rotation: a second archive with a timestamped name, no clobber

    with open(log_file, "w") as f:
        f.write("another line\n")
    with app.app_context():
        rotate_logs()

    assert len(glob.glob(f"{log_file}.*.gz")) == 2

    for path in glob.glob(f"{log_file}.*.gz"):
        os.remove(path)


@pytest.fixture
def mysqldump_stub(app, tmp_path, monkeypatch):
    stub = tmp_path / "mysqldump"
    stub.write_text(
        '#!/bin/sh\necho "-- fitzflix dump"\necho "CREATE TABLE t (id INT);"\n'
    )
    stub.chmod(0o755)
    monkeypatch.setitem(app.config, "MYSQLDUMP_BIN", str(stub))
    monkeypatch.setitem(
        app.config,
        "SQLALCHEMY_DATABASE_URI",
        "mysql+pymysql://tester:secret@localhost:3306/fitzflix_test",
    )
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    monkeypatch.setitem(app.config, "DB_BACKUP_DIR", str(backup_dir))
    return stub, backup_dir


def test_backup_database_dumps_and_prunes(app, mysqldump_stub):
    stub, backup_dir = mysqldump_stub

    old = backup_dir / "fitzflix_test-2020-01-01.sql.gz"
    with gzip.open(old, "wb") as f:
        f.write(b"ancient")
    set_age(old, days=30)

    with app.app_context():
        backup_database()

    backups = sorted(backup_dir.glob("fitzflix_test-*.sql.gz"))
    assert len(backups) == 1, backups
    assert not old.exists()
    with gzip.open(backups[0], "rb") as f:
        content = f.read().decode()
    assert "-- fitzflix dump" in content
    assert "CREATE TABLE t" in content

    # Same-day collision: the second backup gets a timestamped name

    with app.app_context():
        backup_database()
    assert len(list(backup_dir.glob("fitzflix_test-*.sql.gz"))) == 2


def test_backup_database_failure_leaves_no_partial(app, mysqldump_stub):
    stub, backup_dir = mysqldump_stub
    stub.write_text('#!/bin/sh\necho "partial output"\necho "boom" >&2\nexit 1\n')

    with app.app_context():
        with pytest.raises(RuntimeError, match="mysqldump exited 1"):
            backup_database()

    assert list(backup_dir.glob("*.sql.gz")) == []


def test_backup_database_uploads_to_s3_and_prunes_remote(
    app, mysqldump_stub, monkeypatch
):
    """With AWS configured, the dump is copied to the backup prefix and
    remote copies past the retention window are deleted."""

    from datetime import datetime, timedelta, timezone

    import app.videos as videos

    stub, backup_dir = mysqldump_stub
    monkeypatch.setitem(app.config, "AWS_BUCKET", "test-bucket")

    uploads = []
    deletes = []

    class FakeS3Client:
        def upload_file(self, filename, bucket, key, **kwargs):
            uploads.append((filename, bucket, key))

        def delete_object(self, Bucket, Key):
            deletes.append((Bucket, Key))

    stale = {
        "Key": "backup/fitzflix_test-2020-01-01.sql.gz",
        "LastModified": datetime.now(timezone.utc) - timedelta(days=30),
    }
    fresh = {
        "Key": "backup/fitzflix_test-fresh.sql.gz",
        "LastModified": datetime.now(timezone.utc) - timedelta(days=1),
    }
    marker = {
        "Key": "backup/",
        "LastModified": datetime.now(timezone.utc) - timedelta(days=999),
    }

    monkeypatch.setattr(
        videos, "aws_s3_client", lambda with_retries=False: FakeS3Client()
    )
    monkeypatch.setattr(
        videos,
        "get_matching_s3_objects",
        lambda bucket, prefix="", suffix="": iter([stale, fresh, marker]),
    )

    with app.app_context():
        backup_database()

    assert len(uploads) == 1
    filename, bucket, key = uploads[0]
    assert filename.endswith(".sql.gz")
    assert bucket == "test-bucket"
    assert key.startswith("backup/fitzflix_test-")
    assert key.endswith(".sql.gz")

    # Only the stale backup is deleted: not the fresh one, not the
    # directory marker, and never the object just uploaded

    assert deletes == [("test-bucket", "backup/fitzflix_test-2020-01-01.sql.gz")]


def test_backup_database_skips_s3_when_unconfigured(app, mysqldump_stub, monkeypatch):
    """Without an AWS bucket, the backup succeeds without touching boto3."""

    import app.videos as videos

    stub, backup_dir = mysqldump_stub
    assert app.config["AWS_BUCKET"] is None

    def explode(*args, **kwargs):
        raise AssertionError("S3 client should not be built")

    monkeypatch.setattr(videos, "aws_s3_client", explode)

    with app.app_context():
        backup_database()

    assert len(list(backup_dir.glob("fitzflix_test-*.sql.gz"))) == 1
