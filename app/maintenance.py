"""Tasks for maintaining the application itself, rather than the video library."""

import glob
import gzip
import os
import shutil
import subprocess

from datetime import datetime, timedelta

from flask import current_app
from sqlalchemy.engine import make_url
from werkzeug.local import LocalProxy

from app import get_app

# This process's app instance, resolved lazily so importing this module from
# a process that already has an application doesn't build a second one

app = LocalProxy(get_app)


def rotate_logs():
    """Archive the current log file and prune archives past the retention window.

    The log file is renamed with a date suffix and gzipped; every process
    writes through a WatchedFileHandler, so each reopens the fresh log file on
    its next write. Archives older than LOG_RETENTION_DAYS are deleted.
    """

    with app.app_context():
        log_file = current_app.config["LOG_FILE"]
        retention_days = current_app.config["LOG_RETENTION_DAYS"]

        archived = None
        if os.path.isfile(log_file) and os.path.getsize(log_file) > 0:
            stamp = datetime.now().strftime("%Y-%m-%d")
            archive = f"{log_file}.{stamp}"
            if os.path.exists(archive) or os.path.exists(f"{archive}.gz"):
                # Already rotated today; timestamp instead of clobbering
                stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
                archive = f"{log_file}.{stamp}"

            os.rename(log_file, archive)
            with open(archive, "rb") as source, gzip.open(
                f"{archive}.gz", "wb"
            ) as target:
                shutil.copyfileobj(source, target)
            os.remove(archive)
            archived = f"{archive}.gz"

        cutoff = datetime.now() - timedelta(days=retention_days)
        deleted = []
        for path in sorted(glob.glob(f"{log_file}.*.gz")):
            if datetime.fromtimestamp(os.path.getmtime(path)) < cutoff:
                os.remove(path)
                deleted.append(os.path.basename(path))

        current_app.logger.info(
            f"Rotated logs: "
            f"archived {os.path.basename(archived) if archived else 'nothing'}, "
            f"deleted {len(deleted)} archive(s) older than {retention_days} days"
            f"{' ' + str(deleted) if deleted else ''}"
        )


def backup_database():
    """Dump the database to a compressed backup and prune old backups.

    The media files are archived at AWS, but the database — reviews,
    Criterion details, shopping priorities — exists only here, so it gets a
    nightly dump with its own retention window.
    """

    with app.app_context():
        backup_dir = current_app.config["DB_BACKUP_DIR"]
        retention_days = current_app.config["DB_BACKUP_RETENTION_DAYS"]
        url = make_url(current_app.config["SQLALCHEMY_DATABASE_URI"])

        os.makedirs(backup_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d")
        backup_file = os.path.join(backup_dir, f"{url.database}-{stamp}.sql.gz")
        if os.path.exists(backup_file):
            # Already backed up today; timestamp instead of clobbering
            stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
            backup_file = os.path.join(backup_dir, f"{url.database}-{stamp}.sql.gz")

        command = [
            current_app.config["MYSQLDUMP_BIN"],
            "--single-transaction",
            f"--user={url.username}",
        ]
        if url.host:
            command.append(f"--host={url.host}")
        if url.port:
            command.append(f"--port={url.port}")
        command.append(url.database)

        # Pass the password through the environment so it doesn't appear in
        # the process list

        env = dict(os.environ)
        if url.password:
            env["MYSQL_PWD"] = url.password

        try:
            with gzip.open(backup_file, "wb") as target:
                dump = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                )
                shutil.copyfileobj(dump.stdout, target)
                dump.stdout.close()
                stderr = dump.stderr.read().decode("utf-8", "replace")
                if dump.wait() != 0:
                    raise RuntimeError(
                        f"mysqldump exited {dump.returncode}: {stderr[:300]}"
                    )

        except Exception:
            # Don't leave a partial backup that looks like a good one
            if os.path.exists(backup_file):
                os.remove(backup_file)
            raise

        size_mb = round(os.path.getsize(backup_file) / 1024 / 1024, 1)

        cutoff = datetime.now() - timedelta(days=retention_days)
        deleted = []
        for path in sorted(glob.glob(os.path.join(backup_dir, "*.sql.gz"))):
            if path != backup_file and (
                datetime.fromtimestamp(os.path.getmtime(path)) < cutoff
            ):
                os.remove(path)
                deleted.append(os.path.basename(path))

        current_app.logger.info(
            f"Backed up the database to {os.path.basename(backup_file)} "
            f"({size_mb} MB), deleted {len(deleted)} backup(s) older than "
            f"{retention_days} days{' ' + str(deleted) if deleted else ''}"
        )
        return True
