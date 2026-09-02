"""Rename a TV series on disk and in the database.

This is the fix for Plex disambiguation (Batman becomes Batman (1966)).

This task does NOT touch S3. This is intentional. A series rename
changes the display name, not the identity. The aws_untouched_key
column is the authoritative pointer to a real archived object. A rename
of the archived keys would force a new upload of every Deep Archive
file (the fallback of rename_untouched_object). The weekly S3 sync
compares the stored keys. It never compares a key against a basename.
Thus, the old keys stay green forever.

The task is resumable. Each file commits after its own disk move. A
file that is already at its target counts as moved. A record whose
local file was deleted intentionally (archived-only physical media)
gets new rows with no disk operation. The weekly sync cannot run during
a rename. The sync defers unless every queue is idle, and this task
occupies one queue.
"""

import json
import os
import traceback

import urllib3

from flask import current_app
from pathvalidate import sanitize_filename
from unidecode import unidecode
from werkzeug.local import LocalProxy

from app import db, get_app
from app.models import TVSeries

app = LocalProxy(get_app)


def _update_sonarr_path(series, old_folder, new_folder):
    """Point the Sonarr series at the renamed folder.

    Sonarr matches the series by TheTVDB id, its native key. Thus, the
    Sonarr imports and upgrades continue to arrive in the correct folder.
    This function uses urllib3, the same as the arr webhook helpers. The
    requests library segfaults on this host for arr calls."""

    base_url = current_app.config.get("SONARR_URL")
    api_key = current_app.config.get("SONARR_API_KEY")
    if not (base_url and api_key and series.tvdb_id):
        return False

    http = urllib3.PoolManager()
    headers = {"X-Api-Key": api_key, "Content-Type": "application/json"}
    listing = http.request(
        "GET", f"{base_url}/api/v3/series", headers=headers, timeout=15
    )
    if listing.status != 200:
        current_app.logger.warning(
            f"Series rename: Sonarr series list answered {listing.status}"
        )
        return False

    for entry in json.loads(listing.data.decode("utf-8")):
        if entry.get("tvdbId") != series.tvdb_id:
            continue
        old_path = entry.get("path") or ""
        new_path = os.path.join(os.path.dirname(old_path), new_folder)
        entry["path"] = new_path
        put = http.request(
            "PUT",
            f"{base_url}/api/v3/series/{entry['id']}",
            headers=headers,
            body=json.dumps(entry).encode("utf-8"),
            timeout=30,
        )
        if put.status in (200, 202):
            current_app.logger.info(
                f"Series rename: Sonarr path {old_path!r} -> {new_path!r}"
            )
            return True
        current_app.logger.warning(
            f"Series rename: Sonarr path update answered {put.status}"
        )
        return False

    current_app.logger.info(
        f"Series rename: Sonarr doesn't manage tvdb {series.tvdb_id}, skipping"
    )
    return False


def rename_tv_series_task(series_id, new_title):
    """Rename the folder, files, and database rows of a TV series.

    This is a task. After the rename, it updates the Sonarr path and
    queues a Plex rescan."""

    with app.app_context():
        series = db.session.get(TVSeries, series_id)
        if series is None:
            current_app.logger.warning(f"Series rename: no series {series_id}")
            return False

        old_title = series.title
        if new_title == old_title:
            current_app.logger.warning("Series rename: title unchanged")
            return False
        if sanitize_filename(unidecode(new_title)) != new_title:
            current_app.logger.warning(
                f"Series rename: {new_title!r} isn't filesystem-safe"
            )
            return False
        if TVSeries.query.filter_by(title=new_title).first() is not None:
            current_app.logger.warning(
                f"Series rename: a series titled {new_title!r} already exists"
            )
            return False

        library_dir = current_app.config["LIBRARY_DIR"]
        old_prefix = f"{old_title} - "
        new_prefix = f"{new_title} - "

        from app.videos import _rename_with_retries

        moved = db_only = skipped = 0
        old_dirs = set()
        for file in series.files.all():
            parts = file.dirname.split("/")
            if old_title not in parts or not file.basename.startswith(old_prefix):
                current_app.logger.warning(
                    f"Series rename: {file.file_path!r} doesn't carry the "
                    f"series title, skipping"
                )
                skipped += 1
                continue

            new_dirname = "/".join(
                new_title if part == old_title else part for part in parts
            )
            new_basename = new_prefix + file.basename[len(old_prefix) :]
            new_path = f"{new_dirname}/{new_basename}"
            src = os.path.join(library_dir, file.file_path)
            dst = os.path.join(library_dir, new_path)

            if os.path.isfile(src):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                _rename_with_retries(src, dst)
                old_dirs.add(os.path.dirname(src))
                moved += 1
            elif os.path.isfile(dst):
                # A previous partial run moved the file. Complete the row.
                moved += 1
            else:
                # There is no local copy. This is an archived-only record.
                # The rows get the new name. The S3 key stays as it is.
                db_only += 1

            file.dirname = new_dirname
            file.basename = new_basename
            file.file_path = new_path
            if file.plex_title.startswith(old_prefix):
                file.plex_title = new_prefix + file.plex_title[len(old_prefix) :]
            db.session.commit()

        # This step is aware of junk files. It is not a plain rmdir. The
        # old series folder usually keeps a poster.png (from Sonarr or
        # placed by hand). Before, that file kept the empty folder alive
        # until the weekly sweep.

        from app.maintenance import clear_leftover_directory

        for old_dir in sorted(old_dirs, key=len, reverse=True):
            clear_leftover_directory(old_dir)

        series.title = new_title
        db.session.commit()

        try:
            _update_sonarr_path(series, old_title, new_title)
        except Exception:
            current_app.logger.warning(traceback.format_exc())

        if current_app.config.get("PLEX_URL") and current_app.config.get("PLEX_TOKEN"):
            current_app.maintenance_queue.enqueue(
                "app.plex_library.refresh_plex_libraries",
                job_timeout=600,
                description="Refreshing Plex libraries",
            )

        current_app.logger.info(
            f"Series rename: {old_title!r} -> {new_title!r}: {moved} files "
            f"moved, {db_only} archived-only rows, {skipped} skipped"
        )
        return True
