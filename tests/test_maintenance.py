"""Test the log rotation and the database backup.

The tests cover the archive naming, the same-day collisions, the
retention pruning, and the no-partial-backup guarantee on failure.

backup_database runs against a stub mysqldump script. Thus, the tests
cover the gzip and prune plumbing without a MariaDB server.
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

    # A same-day rotation makes a second archive with a timestamped name.
    # It does not overwrite the first archive

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

    # On a same-day collision, the second backup gets a timestamped name

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
    """Upload the dump to the backup prefix when AWS is configured.

    The task deletes the remote copies that are older than the retention
    window."""

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

    # The task deletes only the stale backup. It does not delete the fresh
    # backup or the directory marker. It never deletes the object that it
    # uploaded

    assert deletes == [("test-bucket", "backup/fitzflix_test-2020-01-01.sql.gz")]


def test_backup_database_skips_s3_when_unconfigured(app, mysqldump_stub, monkeypatch):
    """Make sure the backup succeeds without boto3 when there is no AWS bucket."""

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
    """Upload an encrypted .env when a passphrase is set.

    The documented openssl command can decrypt the file."""

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

    # The file is encrypted. The exact command in the runbook recovers it

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

    # The encrypted temp file did not stay on the local disk

    assert not list(backup_dir.glob(".dotenv-*"))


def test_backup_syncs_custom_posters(app, mysqldump_stub, monkeypatch):
    """Mirror the custom posters to S3.

    The task uploads the new files and the changed files. It does not
    upload the unchanged files. It removes the remote copies of the
    deleted files."""

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


# volume_alive: a path under the volumes root must BE a mountpoint (#227)


def test_volume_alive_rejects_a_volumes_path_that_is_not_a_mountpoint(
    monkeypatch, tmp_path
):
    """Reject a path under the volumes root that is not a mountpoint.

    This is the failure of 2026-08-24. A share drops. macOS leaves the
    mountpoint behind as an ordinary directory. statvfs answers for the
    boot disk. The old probe reported that as alive. Then the Plex
    refresh would scan an empty tree and empty the trash behind it."""

    import app.maintenance as maintenance

    volumes = tmp_path / "Volumes"
    leftover = volumes / "Movies"
    leftover.mkdir(parents=True)
    monkeypatch.setattr(maintenance, "VOLUMES_ROOT", str(volumes))

    # This is exactly what the probe saw during the outage

    assert os.statvfs(str(leftover))
    assert os.path.isdir(str(leftover))
    assert os.path.ismount(str(leftover)) is False

    assert maintenance.volume_alive(str(leftover)) is False


def test_volume_alive_accepts_a_mounted_volumes_path(monkeypatch, tmp_path):
    import app.maintenance as maintenance

    volumes = tmp_path / "Volumes"
    mounted = volumes / "Movies"
    mounted.mkdir(parents=True)
    monkeypatch.setattr(maintenance, "VOLUMES_ROOT", str(volumes))
    monkeypatch.setattr(maintenance.os.path, "ismount", lambda path: True)

    assert maintenance.volume_alive(str(mounted)) is True


def test_volume_alive_leaves_paths_outside_the_volumes_root_alone(
    monkeypatch, tmp_path
):
    """Accept a local directory that is not a mountpoint.

    Local directories were never expected to be mountpoints. The staging
    directory and the log directory are on the boot disk."""

    import app.maintenance as maintenance

    monkeypatch.setattr(maintenance, "VOLUMES_ROOT", str(tmp_path / "Volumes"))
    local = tmp_path / "staging"
    local.mkdir()

    assert os.path.ismount(str(local)) is False
    assert maintenance.volume_alive(str(local)) is True


def test_volume_alive_is_false_for_a_path_that_isnt_there(tmp_path):
    import app.maintenance as maintenance

    assert maintenance.volume_alive(str(tmp_path / "gone")) is False


def test_heal_mounts_doesnt_force_unmount_a_leftover_directory(
    app, monkeypatch, tmp_path
):
    """Do not force-unmount a leftover directory.

    The probe now sees the leftover-directory case. Thus, the healer
    reaches paths where nothing is mounted. It must go directly to the
    remount. A `diskutil unmount force` on a path that is not a
    mountpoint is a hazard. It is not a no-op (#227)."""

    import subprocess as subprocess_module

    import app.maintenance as maintenance

    volumes = tmp_path / "Volumes"
    leftover = volumes / "Movies"
    leftover.mkdir(parents=True)
    monkeypatch.setattr(maintenance, "VOLUMES_ROOT", str(volumes))
    monkeypatch.setitem(app.config, "MOUNT_URLS", {"Movies": "smb://nas.test/Movies"})

    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess_module.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(maintenance.subprocess, "run", fake_run)

    actions = maintenance.heal_mounts([str(leftover)], app.redis, app.config)

    assert not any(command[0] == "diskutil" for command in calls), calls

    # `mount` is the duplicate check (#233). It finds nothing here. Thus,
    # the healer goes directly to the remount. When the remount fails,
    # the healer reads `mount` again to report where the share went

    assert [command[0] for command in calls] == ["mount", "osascript", "mount"]

    # The remount ran. But the path is still not a mountpoint. Thus, the
    # probe still reports it as dead. The action is a failure, not a
    # success

    assert actions == [f"failed to remount {leftover}: still dead"]


def _mount_output(*lines):
    """Return `mount` output with the shares that this app uses."""

    return "\n".join(lines) + "\n"


def test_share_mounted_elsewhere_finds_a_suffixed_duplicate(monkeypatch):
    """Find a suffixed duplicate of a share (#233).

    The share name in the mount table is URL-encoded. Thus, a share name
    with a space matches only after unquoting."""

    import subprocess as subprocess_module

    import app.maintenance as maintenance

    output = _mount_output(
        "/dev/disk3s1s1 on / (apfs, sealed, local, read-only)",
        "//server@192.168.1.175/TV%20Shows on /Volumes/TV Shows-1 "
        "(smbfs, nodev, nosuid, mounted by server)",
        "//server@192.168.1.175/Movies on /Volumes/Movies (smbfs, nodev, nosuid)",
    )
    monkeypatch.setattr(
        maintenance.subprocess,
        "run",
        lambda command, **kwargs: subprocess_module.CompletedProcess(
            command, 0, stdout=output, stderr=""
        ),
    )

    assert maintenance.share_mounted_elsewhere("TV Shows", "/Volumes/TV Shows") == [
        "/Volumes/TV Shows-1"
    ]

    # A share that is already on its canonical path is not its own duplicate

    assert maintenance.share_mounted_elsewhere("Movies", "/Volumes/Movies") == []


def test_share_mounted_elsewhere_leaves_local_disks_alone(monkeypatch):
    """Leave a local disk alone.

    A local volume can also appear in the `mount` output. The healer
    must not unmount it, even if its name matches."""

    import subprocess as subprocess_module

    import app.maintenance as maintenance

    output = _mount_output("/dev/disk7s2 on /Volumes/Movies-1 (apfs, local, nodev)")
    monkeypatch.setattr(
        maintenance.subprocess,
        "run",
        lambda command, **kwargs: subprocess_module.CompletedProcess(
            command, 0, stdout=output, stderr=""
        ),
    )

    assert maintenance.share_mounted_elsewhere("Movies", "/Volumes/Movies") == []


def test_heal_mounts_frees_a_duplicate_before_remounting(app, monkeypatch, tmp_path):
    """Free a duplicate before the remount.

    This is the #233 failure. The share came back at /Volumes/<share>-1
    while a stub held the canonical path. A remount cannot free the
    canonical path while the share is mounted at a different path. Thus,
    the mount call always succeeded, and the healer left the path dead."""

    import subprocess as subprocess_module

    import app.maintenance as maintenance

    volumes = tmp_path / "Volumes"
    stub = volumes / "TV Shows"
    stub.mkdir(parents=True)
    monkeypatch.setattr(maintenance, "VOLUMES_ROOT", str(volumes))
    monkeypatch.setitem(
        app.config, "MOUNT_URLS", {"TV Shows": "smb://server@nas.test/TV Shows"}
    )

    duplicate = f"{stub}-1"
    output = _mount_output(
        f"//server@nas.test/TV%20Shows on {duplicate} (smbfs, nodev, nosuid)"
    )

    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[0] == "mount":
            return subprocess_module.CompletedProcess(
                command, 0, stdout=output, stderr=""
            )
        return subprocess_module.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(maintenance.subprocess, "run", fake_run)

    actions = maintenance.heal_mounts([str(stub)], app.redis, app.config)

    # The healer unmounts the duplicate cleanly BEFORE it tries the remount

    assert ["diskutil", "unmount", duplicate] in calls
    assert calls.index(["diskutil", "unmount", duplicate]) < next(
        i for i, command in enumerate(calls) if command[0] == "osascript"
    )

    # The healer force-unmounted nothing. The clean unmount succeeded,
    # and the stub itself is not a mountpoint (#227)

    assert ["diskutil", "unmount", "force", duplicate] not in calls
    assert f"unmounted {duplicate} to free {stub}" in actions


def test_heal_mounts_forces_a_duplicate_that_wont_unmount_cleanly(
    app, monkeypatch, tmp_path
):
    """Force-unmount a duplicate that refuses a clean unmount.

    To keep the duplicate is worse than to interrupt it. Each config path
    stays aimed at an empty directory on the boot disk. A TRANSCODES_DIR
    that points there fills the boot disk."""

    import subprocess as subprocess_module

    import app.maintenance as maintenance

    volumes = tmp_path / "Volumes"
    stub = volumes / "Transcoded"
    stub.mkdir(parents=True)
    monkeypatch.setattr(maintenance, "VOLUMES_ROOT", str(volumes))
    monkeypatch.setitem(
        app.config, "MOUNT_URLS", {"Transcoded": "smb://server@nas.test/Transcoded"}
    )

    duplicate = f"{stub}-1"
    output = _mount_output(
        f"//server@nas.test/Transcoded on {duplicate} (smbfs, nodev, nosuid)"
    )

    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[0] == "mount":
            return subprocess_module.CompletedProcess(
                command, 0, stdout=output, stderr=""
            )
        if command == ["diskutil", "unmount", duplicate]:
            return subprocess_module.CompletedProcess(
                command, 1, stdout="", stderr="Resource busy"
            )
        return subprocess_module.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(maintenance.subprocess, "run", fake_run)

    actions = maintenance.heal_mounts([str(stub)], app.redis, app.config)

    # The healer tries a clean unmount first. It uses force only after
    # the clean unmount refuses

    assert ["diskutil", "unmount", duplicate] in calls
    assert ["diskutil", "unmount", "force", duplicate] in calls
    assert calls.index(["diskutil", "unmount", duplicate]) < calls.index(
        ["diskutil", "unmount", "force", duplicate]
    )
    assert f"unmounted {duplicate} to free {stub}" in actions


def test_heal_mounts_says_where_a_stranded_share_went(app, monkeypatch, tmp_path):
    """Report where a stranded share went.

    A "still dead" alert sends a person to look. The alert names the
    path that the share is on. Thus, the person can act on it."""

    import subprocess as subprocess_module

    import app.maintenance as maintenance

    volumes = tmp_path / "Volumes"
    stub = volumes / "Movies"
    stub.mkdir(parents=True)
    monkeypatch.setattr(maintenance, "VOLUMES_ROOT", str(volumes))
    monkeypatch.setitem(
        app.config, "MOUNT_URLS", {"Movies": "smb://server@nas.test/Movies"}
    )

    duplicate = f"{stub}-1"
    output = _mount_output(
        f"//server@nas.test/Movies on {duplicate} (smbfs, nodev, nosuid)"
    )

    def fake_run(command, **kwargs):
        if command[0] == "mount":
            return subprocess_module.CompletedProcess(
                command, 0, stdout=output, stderr=""
            )
        if command[0] == "diskutil":
            # Nothing frees the duplicate. Thus, the remount cannot succeed
            return subprocess_module.CompletedProcess(
                command, 1, stdout="", stderr="Resource busy"
            )
        return subprocess_module.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(maintenance.subprocess, "run", fake_run)

    actions = maintenance.heal_mounts([str(stub)], app.redis, app.config)

    assert (
        f"failed to unmount {duplicate}, which is holding the share {stub} needs"
        in actions
    )
    assert (
        f"failed to remount {stub}: still dead — share is mounted at {duplicate}"
        in (actions)
    )


def test_mount_urls_keys_each_share_by_its_url_basename():
    """Key each share in MOUNT_URLS by the basename of its URL.

    The NFS exports are on different volume roots (/volume2/Movies,
    /volume3/TV Shows). Thus, 1 server prefix cannot address them. Each
    share carries a full URL, keyed by its mount-point name. Fitzflix
    decodes an encoded basename. Then it matches what os.path.basename
    returns for the /Volumes path."""

    from config import _mount_urls

    assert _mount_urls(
        "smb://user@nas.local/Movies, nfs://nas.local/volume3/TV%20Shows/"
    ) == {
        "Movies": "smb://user@nas.local/Movies",
        "TV Shows": "nfs://nas.local/volume3/TV%20Shows",
    }
    assert _mount_urls(None) == {}
    assert _mount_urls("") == {}


def test_heal_mounts_calls_out_a_share_missing_from_the_map(app, monkeypatch, tmp_path):
    """Report a dead share that is missing from the configured map.

    A silent skip would hide the reason that the share never heals. The
    old prefix scheme would have tried the remount."""

    import subprocess as subprocess_module

    import app.maintenance as maintenance

    volumes = tmp_path / "Volumes"
    leftover = volumes / "Music"
    leftover.mkdir(parents=True)
    monkeypatch.setattr(maintenance, "VOLUMES_ROOT", str(volumes))
    monkeypatch.setitem(
        app.config, "MOUNT_URLS", {"Movies": "nfs://nas.test/volume2/Movies"}
    )

    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess_module.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(maintenance.subprocess, "run", fake_run)

    actions = maintenance.heal_mounts([str(leftover)], app.redis, app.config)

    assert actions == [
        f"no MOUNT_URLS entry for Music, so not attempting to remount {leftover}"
    ]
    assert calls == []


def test_share_mounted_elsewhere_finds_an_nfs_duplicate(monkeypatch):
    """Find an NFS duplicate of a share (#239).

    An NFS device reads `host:/export/Share`. The share name is literal,
    not URL-encoded. The smb-only match could not see an NFS-mounted
    duplicate at all. Thus, heal_mounts would continue to remount into
    the void. That is the exact failure that #233 fixed for SMB."""

    import subprocess as subprocess_module

    import app.maintenance as maintenance

    output = _mount_output(
        "/dev/disk3s1s1 on / (apfs, sealed, local, read-only)",
        "192.168.1.175:/volume1/TV Shows on /Volumes/TV Shows-1 "
        "(nfs, nodev, nosuid, mounted by server)",
        "192.168.1.175:/volume1/Movies on /Volumes/Movies (nfs, nodev, nosuid)",
    )
    monkeypatch.setattr(
        maintenance.subprocess,
        "run",
        lambda command, **kwargs: subprocess_module.CompletedProcess(
            command, 0, stdout=output, stderr=""
        ),
    )

    assert maintenance.share_mounted_elsewhere("TV Shows", "/Volumes/TV Shows") == [
        "/Volumes/TV Shows-1"
    ]

    # A share that is already on its canonical path is not its own duplicate

    assert maintenance.share_mounted_elsewhere("Movies", "/Volumes/Movies") == []
