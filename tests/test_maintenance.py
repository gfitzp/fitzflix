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


def test_backup_uploads_encrypted_env(app, mysqldump_stub, monkeypatch, tmp_path):
    """With a passphrase set, the nightly backup uploads an encrypted .env
    that the documented openssl command can decrypt."""

    import os
    import subprocess

    import app.videos as videos

    stub, backup_dir = mysqldump_stub
    monkeypatch.setitem(app.config, "AWS_BUCKET", "test-bucket")
    monkeypatch.setitem(app.config, "BACKUP_PASSPHRASE", "drill-passphrase")
    env_file = tmp_path / "dotenv"
    env_file.write_text("SECRET_KEY=abc123\n")
    monkeypatch.setitem(app.config, "ENV_FILE", str(env_file))

    captured = {}

    class FakeS3Client:
        def upload_file(self, filename, bucket, key, **kwargs):
            with open(filename, "rb") as f:
                captured[key] = f.read()

        def delete_object(self, Bucket, Key):
            pass

    monkeypatch.setattr(videos, "aws_s3_client", lambda **kwargs: FakeS3Client())
    monkeypatch.setattr(
        videos,
        "get_matching_s3_objects",
        lambda bucket, prefix="", suffix="": iter([]),
    )

    with app.app_context():
        backup_database()

    env_keys = [k for k in captured if "/dotenv-" in k and k.endswith(".enc")]
    assert len(env_keys) == 1

    # Actually encrypted, and recoverable with the runbook's exact command

    encrypted = captured[env_keys[0]]
    assert b"SECRET_KEY" not in encrypted

    encrypted_path = tmp_path / "roundtrip.enc"
    encrypted_path.write_bytes(encrypted)
    decrypted = subprocess.run(
        [
            "openssl",
            "enc",
            "-d",
            "-aes-256-cbc",
            "-pbkdf2",
            "-iter",
            "200000",
            "-in",
            str(encrypted_path),
            "-pass",
            "env:BACKUP_PASSPHRASE",
        ],
        env={**os.environ, "BACKUP_PASSPHRASE": "drill-passphrase"},
        capture_output=True,
        check=True,
    )
    assert decrypted.stdout == b"SECRET_KEY=abc123\n"

    # The encrypted temp file didn't linger locally

    assert not list(backup_dir.glob(".dotenv-*"))


def test_backup_syncs_custom_posters(app, mysqldump_stub, monkeypatch):
    """Custom posters mirror to S3: new and changed files upload, unchanged
    ones don't, and remote copies of deleted files are removed."""

    import os
    import shutil
    from datetime import datetime, timezone

    import app.videos as videos

    stub, backup_dir = mysqldump_stub
    monkeypatch.setitem(app.config, "AWS_BUCKET", "test-bucket")

    posters_dir = os.path.join(app.config["CUSTOM_ARTWORK_DIR"], "movie", "7", "w185")
    os.makedirs(posters_dir, exist_ok=True)
    with open(os.path.join(posters_dir, "new.jpg"), "wb") as f:
        f.write(b"new poster bytes")
    with open(os.path.join(posters_dir, "changed.jpg"), "wb") as f:
        f.write(b"now bigger contents")
    with open(os.path.join(posters_dir, "same.jpg"), "wb") as f:
        f.write(b"12345")

    now = datetime.now(timezone.utc)

    def fake_objects(bucket, prefix="", suffix=""):
        if prefix.startswith("custom-posters"):
            return iter(
                [
                    {
                        "Key": "custom-posters/movie/7/w185/changed.jpg",
                        "Size": 1,
                        "LastModified": now,
                    },
                    {
                        "Key": "custom-posters/movie/7/w185/same.jpg",
                        "Size": 5,
                        "LastModified": now,
                    },
                    {
                        "Key": "custom-posters/orphan.jpg",
                        "Size": 3,
                        "LastModified": now,
                    },
                ]
            )
        return iter([])

    uploads = []
    deletes = []

    class FakeS3Client:
        def upload_file(self, filename, bucket, key, **kwargs):
            uploads.append(key)

        def delete_object(self, Bucket, Key):
            deletes.append(Key)

    monkeypatch.setattr(videos, "aws_s3_client", lambda **kwargs: FakeS3Client())
    monkeypatch.setattr(videos, "get_matching_s3_objects", fake_objects)

    try:
        with app.app_context():
            backup_database()

        poster_uploads = {k for k in uploads if k.startswith("custom-posters/")}
        assert poster_uploads == {
            "custom-posters/movie/7/w185/new.jpg",
            "custom-posters/movie/7/w185/changed.jpg",
        }
        assert deletes == ["custom-posters/orphan.jpg"]
    finally:
        shutil.rmtree(app.config["CUSTOM_ARTWORK_DIR"], ignore_errors=True)


def test_newest_backup_key_picks_latest_dump():
    from datetime import datetime, timezone

    from app.maintenance import _newest_backup_key

    def at(day):
        return datetime(2026, 8, day, tzinfo=timezone.utc)

    objects = [
        {"Key": "backup/fitzflix_test-2026-08-01.sql.gz", "LastModified": at(1)},
        {"Key": "backup/fitzflix_test-2026-08-05.sql.gz", "LastModified": at(5)},
        {"Key": "backup/dotenv-2026-08-06.enc", "LastModified": at(6)},
        {"Key": "backup/", "LastModified": at(6)},
        {"Key": "backup/otherdb-2026-08-07.sql.gz", "LastModified": at(7)},
    ]
    assert (
        _newest_backup_key(objects, "fitzflix_test")
        == "backup/fitzflix_test-2026-08-05.sql.gz"
    )
    assert _newest_backup_key([], "fitzflix_test") is None


def test_restore_drill_skips_without_aws(app):
    from app.maintenance import restore_drill

    with app.app_context():
        assert restore_drill() is True
