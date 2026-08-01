"""Tasks for maintaining the application itself, rather than the video library."""

import glob
import gzip
import os
import shutil

from datetime import datetime, timedelta

from flask import current_app
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
