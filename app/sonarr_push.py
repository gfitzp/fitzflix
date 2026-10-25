"""Let Sonarr rename its own episode file for the quality downgrade (#289).

This is the Sonarr twin of the adopt task in app.radarr_push (#288).
Fitzflix gives each download a web quality title, because it keeps the
physical-media titles for its own disc rips. A rename in Fitzflix made
Radarr lose its file and download the film again. Thus, Sonarr changes
the quality of its own episode-file record and renames the file. Its
naming formats hold the quality title. Thus, the Sonarr name is the name
that Fitzflix wants, and Sonarr keeps its record.
"""

import time

from app.arr_client import (
    SONARR,
    SonarrError,
    adopt_download,
    arr_request,
    configured,
    rename_to_quality,
)
from app.arr_client import rename_download_locally as _rename_locally

# The longest wait for the RenameFiles command of Sonarr, in seconds.

SONARR_RENAME_WAIT = 120

# SonarrError lives in app.arr_client. This name keeps the old import
# path. The rename wait sleeps through time, and the tests patch
# sonarr_push.time.sleep.

__all__ = ["SonarrError", "time"]


def sonarr_configured():
    """Return True if Fitzflix can call the Sonarr API."""

    return configured(SONARR)


def _sonarr(method, path, payload=None):
    """Make one authenticated JSON call to the Sonarr API."""

    return arr_request(SONARR, method, path, payload)


def _sonarr_rename_to_quality(series_id, episode_file_id, quality_name):
    """Give a Sonarr episode file a new quality, and let Sonarr rename it.

    Return the new absolute path from the record of Sonarr."""

    return rename_to_quality(
        SONARR, _sonarr, series_id, episode_file_id, quality_name, SONARR_RENAME_WAIT
    )


def rename_download_locally(downloaded_path, new_path, series_id, series_title=None):
    """Rename a Sonarr download in Fitzflix, then ask Sonarr to rescan."""

    _rename_locally(SONARR, downloaded_path, new_path, series_id, title=series_title)


def adopt_episode_task(
    series_id, episode_file_id, downloaded_path, new_path, quality, at_front=False
):
    """Give a Sonarr download the quality of Fitzflix, then queue its import.

    This is a task. Sonarr changes the quality of its episode-file record
    and renames the file. If Sonarr cannot do it, Fitzflix renames the
    file as before. In both cases the import goes on the queue after the
    rename. A recent episode keeps its place at the front."""

    from app import get_app

    with get_app().app_context():
        return adopt_download(
            SONARR,
            _sonarr,
            series_id,
            episode_file_id,
            downloaded_path,
            new_path,
            quality,
            SONARR_RENAME_WAIT,
            lambda: rename_download_locally(downloaded_path, new_path, series_id),
            at_front=at_front,
        )
