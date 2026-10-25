"""Let Sonarr rename its own episode file for the quality downgrade (#289).

This is the Sonarr twin of the adopt task in app.radarr_push (#288).
Fitzflix gives each download a web quality title, because it keeps the
physical-media titles for its own disc rips. A rename in Fitzflix made
Radarr lose its file and download the film again. Thus, Sonarr changes
the quality of its own episode-file record and renames the file. Its
naming formats hold the quality title. Thus, the Sonarr name is the name
that Fitzflix wants, and Sonarr keeps its record.
"""

import os
import shutil
import time
import traceback

import requests

from flask import current_app

# The connect and read timeouts of a Sonarr call, in seconds.

SONARR_TIMEOUT = (5, 30)

# The longest wait for the RenameFiles command of Sonarr, in seconds.

SONARR_RENAME_WAIT = 120


class SonarrError(Exception):
    """A Sonarr request did not give the expected result."""


def sonarr_configured():
    """Return True if Fitzflix can call the Sonarr API."""

    return bool(
        current_app.config.get("SONARR_URL")
        and current_app.config.get("SONARR_API_KEY")
    )


def _sonarr(method, path, payload=None):
    """Make one authenticated JSON call to the Sonarr API."""

    r = requests.request(
        method,
        current_app.config["SONARR_URL"] + path,
        json=payload,
        headers={"X-Api-Key": current_app.config["SONARR_API_KEY"]},
        timeout=SONARR_TIMEOUT,
    )
    r.raise_for_status()
    return r.json() if r.content else None


def _quality_resource(name):
    """Return the Sonarr quality resource of a quality title, revision 1."""

    for definition in _sonarr("GET", "/api/v3/qualitydefinition"):
        if definition["quality"]["name"] == name:
            return {
                "quality": definition["quality"],
                "revision": {"version": 1, "real": 0, "isRepack": False},
            }
    raise SonarrError(f"Sonarr has no quality named {name!r}")


def _sonarr_rename_to_quality(series_id, episode_file_id, quality_name):
    """Give a Sonarr episode file a new quality, and let Sonarr rename it.

    Return the new absolute path from the record of Sonarr."""

    record = _sonarr("GET", f"/api/v3/episodefile/{int(episode_file_id)}")
    record["quality"] = _quality_resource(quality_name)
    _sonarr("PUT", f"/api/v3/episodefile/{int(episode_file_id)}", record)
    command = _sonarr(
        "POST",
        "/api/v3/command",
        {
            "name": "RenameFiles",
            "seriesId": int(series_id),
            "files": [int(episode_file_id)],
        },
    )
    deadline = time.monotonic() + SONARR_RENAME_WAIT
    while True:
        status = _sonarr("GET", f"/api/v3/command/{command['id']}").get("status")
        if status == "completed":
            break
        if status in ("failed", "aborted", "cancelled", "orphaned"):
            raise SonarrError(f"The Sonarr rename ended with the status {status!r}")
        if time.monotonic() > deadline:
            raise SonarrError("The Sonarr rename did not end in time")
        time.sleep(1)
    return _sonarr("GET", f"/api/v3/episodefile/{int(episode_file_id)}")["path"]


def rename_download_locally(downloaded_path, new_path, series_id, series_title=None):
    """Rename a Sonarr download in Fitzflix, then ask Sonarr to rescan.

    This was the only way before #289. It stays as the fallback when
    Sonarr cannot rename its own file."""

    from app.api.arr import send_arr_command

    if downloaded_path != new_path:
        shutil.move(downloaded_path, new_path)
        current_app.logger.info(f"'{downloaded_path}' renamed as '{new_path}'")
    # Sonarr can send a webhook when Fitzflix has no Sonarr address. The
    # rename is done, so the caller must still get to the localization.

    if series_id and sonarr_configured():
        current_app.logger.info(f"Rescanning series '{series_title or series_id}'")
        send_arr_command(
            "Sonarr",
            current_app.config["SONARR_URL"] + "/api/v3/command",
            current_app.config["SONARR_API_KEY"],
            {"name": "RescanSeries", "seriesId": int(series_id)},
        )


def adopt_episode_task(
    series_id, episode_file_id, downloaded_path, new_path, quality, at_front=False
):
    """Give a Sonarr download the quality of Fitzflix, then queue its import.

    This is a task. Sonarr changes the quality of its episode-file record
    and renames the file. If Sonarr cannot do it, Fitzflix renames the
    file as before. In both cases the import goes on the queue after the
    rename. A recent episode keeps its place at the front."""

    from app import get_app
    from app.importing import enqueue_localization
    from app.radarr_push import fallback_rename

    with get_app().app_context():
        try:
            path = _sonarr_rename_to_quality(series_id, episode_file_id, quality)
            if not os.path.exists(path):
                raise SonarrError(f"Sonarr reports {path!r}, which does not exist")
            current_app.logger.info(
                f"'{downloaded_path}' Sonarr renamed it as '{path}' ({quality})"
            )
        except Exception:
            current_app.logger.warning(
                f"Sonarr could not rename '{downloaded_path}'. Fitzflix renames "
                f"it: " + traceback.format_exc()
            )
            path = fallback_rename(
                downloaded_path,
                new_path,
                lambda: _sonarr("GET", f"/api/v3/episodefile/{int(episode_file_id)}")[
                    "path"
                ],
                lambda: rename_download_locally(downloaded_path, new_path, series_id),
            )
        if enqueue_localization(path, at_front=at_front):
            current_app.logger.info(f"'{path}' Sent to Fitzflix")
        return True
