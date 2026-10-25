"""Hold the shared client of the Radarr and Sonarr APIs.

Radarr and Sonarr have the same v3 API for the calls of Fitzflix. An
Arr names the differences: the config keys, the file record (moviefile
or episodefile), the id field of the owner (movieId or seriesId), the
rescan command, and the error class. The functions of this module take
an Arr. app.radarr_push and app.sonarr_push keep their own names
(_radarr, _sonarr, adopt_download_task, adopt_episode_task) as thin
wrappers. The stored rq job names and the test patch points use them.

The 2 apps really differ in 4 places, and those stay explicit at the
callers. A recent Sonarr episode goes to the front of the import queue.
Only Radarr has the request, the withdraw, the badge cache and the
nightly path check. Radarr finds a film by tmdbId. Sonarr finds a
series by tvdbId.

The HTTP library (decided 2026-10-25): the API calls use requests. An
old note said that requests crashed this machine with a segmentation
fault on arr calls. But _radarr has used requests for every Radarr
push since 2026-08-19, and _sonarr since #289. supervisord.log shows no
SIGSEGV of a worker in that time. Thus, the note was stale. The
command and history helpers of app.api.arr still use urllib3. They
work, and a change gives nothing.
"""

import os
import shutil
import time
import traceback

from dataclasses import dataclass

import requests

from flask import current_app

# The connect and read timeouts of an arr call, in seconds. A host that
# is down fails at the connect. Thus, it costs 5 seconds, not 30. The
# read stays long for the full movie list (#268).

ARR_TIMEOUT = (5, 30)


class RadarrError(Exception):
    """A Radarr request failed in a way that the user must know about.

    The message stays as it is."""


class SonarrError(Exception):
    """A Sonarr request did not give the expected result."""


@dataclass(frozen=True)
class Arr:
    """The differences between Radarr and Sonarr that the shared code reads.

    owner_key and file_key are the payload keys of a webhook, and
    folder_field is the folder of the owner in that payload. rescan_ids
    is the field of the rescan command. A field that ends in "Ids" takes
    a list."""

    name: str
    url_key: str
    api_key_key: str
    record: str
    owner_id: str
    rescan: str
    rescan_ids: str
    error: type
    owner_key: str
    file_key: str
    folder_field: str
    min_score_setting: str
    adopt_task: str
    noun: str


RADARR = Arr(
    name="Radarr",
    url_key="RADARR_URL",
    api_key_key="RADARR_API_KEY",
    record="moviefile",
    owner_id="movieId",
    rescan="RefreshMovie",
    rescan_ids="movieIds",
    error=RadarrError,
    owner_key="movie",
    file_key="movieFile",
    folder_field="folderPath",
    min_score_setting="RADARR_WEBDL_MIN_SCORE",
    adopt_task="app.radarr_push.adopt_download_task",
    noun="movie",
)

SONARR = Arr(
    name="Sonarr",
    url_key="SONARR_URL",
    api_key_key="SONARR_API_KEY",
    record="episodefile",
    owner_id="seriesId",
    rescan="RescanSeries",
    rescan_ids="seriesId",
    error=SonarrError,
    owner_key="series",
    file_key="episodeFile",
    folder_field="path",
    min_score_setting="SONARR_WEBDL_MIN_SCORE",
    adopt_task="app.sonarr_push.adopt_episode_task",
    noun="series",
)


def configured(arr):
    """Return True if Fitzflix can call the API of arr."""

    return bool(
        current_app.config.get(arr.url_key) and current_app.config.get(arr.api_key_key)
    )


def arr_request(arr, method, path, payload=None):
    """Make 1 authenticated JSON call to the API of arr. Raise on an error status."""

    r = requests.request(
        method,
        current_app.config[arr.url_key] + path,
        json=payload,
        headers={"X-Api-Key": current_app.config[arr.api_key_key]},
        timeout=ARR_TIMEOUT,
    )
    r.raise_for_status()
    return r.json() if r.content else None


def rescan_command(arr, owner_id):
    """Return the body of the rescan command of 1 movie or 1 series."""

    owner_id = int(owner_id)
    return {
        "name": arr.rescan,
        arr.rescan_ids: [owner_id] if arr.rescan_ids.endswith("Ids") else owner_id,
    }


def quality_resource(arr, call, name):
    """Return the quality resource of a quality title, revision 1.

    call is the API function of the caller, for example _radarr."""

    for definition in call("GET", "/api/v3/qualitydefinition"):
        if definition["quality"]["name"] == name:
            return {
                "quality": definition["quality"],
                "revision": {"version": 1, "real": 0, "isRepack": False},
            }
    raise arr.error(f"{arr.name} has no quality named {name!r}")


def rename_to_quality(arr, call, owner_id, file_id, quality_name, wait):
    """Give a file record of arr a new quality, and let arr rename the file.

    Return the new absolute path from the record. The naming format of
    arr holds the quality title. Thus, arr keeps its record of the file.
    It does not see a missing file (#288, #289). wait is the longest wait
    for the RenameFiles command, in seconds."""

    record_path = f"/api/v3/{arr.record}/{int(file_id)}"
    record = call("GET", record_path)
    record["quality"] = quality_resource(arr, call, quality_name)
    call("PUT", record_path, record)
    command = call(
        "POST",
        "/api/v3/command",
        {
            "name": "RenameFiles",
            arr.owner_id: int(owner_id),
            "files": [int(file_id)],
        },
    )
    deadline = time.monotonic() + wait
    while True:
        status = call("GET", f"/api/v3/command/{command['id']}").get("status")
        if status == "completed":
            break
        if status in ("failed", "aborted", "cancelled", "orphaned"):
            raise arr.error(f"The {arr.name} rename ended with the status {status!r}")
        if time.monotonic() > deadline:
            raise arr.error(f"The {arr.name} rename did not end in time")
        time.sleep(1)
    return call("GET", record_path)["path"]


def rename_download_locally(arr, downloaded_path, new_path, owner_id, title=None):
    """Rename a download in Fitzflix, then ask arr to rescan.

    This was the only way before #288 and #289. It stays as the fallback
    when arr cannot rename its own file. Then arr can lose the file. arr
    can send a webhook when Fitzflix has no address for it. The rename
    is done in that case too. Thus, the caller still gets to the
    localization."""

    from app.api.arr import send_arr_command

    if downloaded_path != new_path:
        shutil.move(downloaded_path, new_path)
        current_app.logger.info(f"'{downloaded_path}' renamed as '{new_path}'")
    if owner_id and configured(arr):
        current_app.logger.info(f"Rescanning {arr.noun} '{title or owner_id}'")
        send_arr_command(
            arr.name,
            current_app.config[arr.url_key] + "/api/v3/command",
            current_app.config[arr.api_key_key],
            rescan_command(arr, owner_id),
        )


def fallback_rename(downloaded_path, new_path, arr_path, rename_locally):
    """Rename a download in Fitzflix after the arr rename failed, and return
    the path to import.

    The arr can complete its rename after Fitzflix stops the wait (#325).
    For example, the RenameFiles command waits behind a long refresh. Then
    the download is not at its old path. Fitzflix asks the arr for the path
    of its record, then looks for the new name. A failure is logged, and
    the error goes to the job. Thus, the job stays in the failed jobs for a
    retry."""

    try:
        if os.path.exists(downloaded_path):
            rename_locally()
            return new_path
        try:
            path = arr_path()
        except Exception as e:
            current_app.logger.warning(
                f"'{downloaded_path}' is not at its path, and the arr record "
                f"is not available: {e}"
            )
            path = None
        for candidate in (path, new_path):
            if candidate and os.path.exists(candidate):
                current_app.logger.info(
                    f"'{downloaded_path}' The arr renamed it as '{candidate}'"
                )
                return candidate
        raise FileNotFoundError(
            f"'{downloaded_path}' is not at its path, at the arr path "
            f"{path!r}, or at '{new_path}'"
        )
    except Exception:
        current_app.logger.error(
            f"'{downloaded_path}' Fitzflix could not rename or find the "
            f"download. The import is not queued: " + traceback.format_exc()
        )
        raise


def adopt_download(
    arr,
    call,
    owner_id,
    file_id,
    downloaded_path,
    new_path,
    quality,
    wait,
    rename_locally,
    at_front=False,
):
    """Give a download the quality of Fitzflix through arr, then queue its import.

    arr changes the quality of its file record and renames the file.
    Thus, arr keeps the file and does not download it again. If arr
    cannot do it, Fitzflix renames the file as before (rename_locally).
    In both cases the import goes on the queue after the rename."""

    from app.importing import enqueue_localization

    try:
        path = rename_to_quality(arr, call, owner_id, file_id, quality, wait)
        if not os.path.exists(path):
            raise arr.error(f"{arr.name} reports {path!r}, which does not exist")
        current_app.logger.info(
            f"'{downloaded_path}' {arr.name} renamed it as '{path}' ({quality})"
        )
    except Exception:
        current_app.logger.warning(
            f"{arr.name} could not rename '{downloaded_path}'. Fitzflix renames "
            f"it: " + traceback.format_exc()
        )
        path = fallback_rename(
            downloaded_path,
            new_path,
            lambda: call("GET", f"/api/v3/{arr.record}/{int(file_id)}")["path"],
            rename_locally,
        )
    queued = (
        enqueue_localization(path, at_front=True)
        if at_front
        else enqueue_localization(path)
    )
    if queued:
        current_app.logger.info(f"'{path}' Sent to Fitzflix")
    return True
