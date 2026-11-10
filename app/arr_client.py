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
Only Radarr has the request, the withdraw and the badge cache. Each app
has its own nightly path check (app.radarr_push, app.series_rename).
Radarr finds a film by tmdbId. Sonarr finds a series by tvdbId.

The HTTP library (decided 2026-10-25): the API calls use requests. An
old note said that requests crashed this machine with a segmentation
fault on arr calls. But _radarr has used requests for every Radarr
push since 2026-08-19, and _sonarr since #289. supervisord.log shows no
SIGSEGV of a worker in that time. Thus, the note was stale. The
command and history helpers of app.api.arr still use urllib3. They
work, and a change gives nothing.
"""

import json
import os
import shutil
import time
import traceback

from dataclasses import dataclass
from datetime import timedelta

import redis
import requests

from flask import current_app

from app.cache import cache_json

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
    restore_task: str
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
    restore_task="app.radarr_push.restore_download_record_task",
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
    restore_task="app.sonarr_push.restore_download_record_task",
    noun="series",
)

ARRS = {arr.name: arr for arr in (RADARR, SONARR)}


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


def run_command(arr, call, body, wait, what):
    """Send a command to arr and wait until it ends. Raise if it fails.

    wait is the longest wait, in seconds. what names the command in the
    error, for example "rename"."""

    command = call("POST", "/api/v3/command", body)
    deadline = time.monotonic() + wait
    while True:
        status = call("GET", f"/api/v3/command/{command['id']}").get("status")
        if status == "completed":
            return
        if status in ("failed", "aborted", "cancelled", "orphaned"):
            raise arr.error(f"The {arr.name} {what} ended with the status {status!r}")
        if time.monotonic() > deadline:
            raise arr.error(f"The {arr.name} {what} did not end in time")
        time.sleep(1)


def rename_to_quality(arr, call, owner_id, file_id, quality_name, wait):
    """Give a file record of arr a new quality, and let arr rename the file.

    Return the new absolute path from the record. The naming format of
    arr holds the quality title. Thus, the name of arr has the quality
    of Fitzflix (#288, #289). The import can still store the file under
    a different name. Then restore_download_record writes the release
    data onto the new record of arr (#671). wait is the longest wait for
    the RenameFiles command, in seconds."""

    record_path = f"/api/v3/{arr.record}/{int(file_id)}"
    record = call("GET", record_path)
    record["quality"] = quality_resource(arr, call, quality_name)
    call("PUT", record_path, record)
    run_command(
        arr,
        call,
        {
            "name": "RenameFiles",
            arr.owner_id: int(owner_id),
            "files": [int(file_id)],
        },
        wait,
        "rename",
    )
    return call("GET", record_path)["path"]


# The release data of an arr download, for the import of its path (#671).
# The import can store the file under a different name. Then arr drops
# its record at the next disk scan, and the new record has no scene name
# and no release group. arr then downloads the same release again. The
# key expires by itself when no import reads it.

DOWNLOAD_RECORD_KEY = "fitzflix:arr:download:{path}"
DOWNLOAD_RECORD_TTL = 4 * 86400

# A held import waits for a decision of the admin. Its release data
# lives this long at the held path.

HELD_DOWNLOAD_RECORD_TTL = 30 * 86400


def remember_download(arr, path, owner_id, media_file):
    """Keep the release data of a webhook download for the import of path.

    media_file is the episodeFile or movieFile block of the payload. A
    download with no scene name and no release group has nothing to
    keep. Then the data of an older download of the same path goes.
    That download did not finish its import, and its data is not the
    data of this file."""

    key = DOWNLOAD_RECORD_KEY.format(path=path)
    scene_name = media_file.get("sceneName")
    release_group = media_file.get("releaseGroup")
    if not owner_id or not (scene_name or release_group) or not configured(arr):
        current_app.redis.delete(key)
        return
    cache_json(
        key,
        {
            "service": arr.name,
            "owner_id": int(owner_id),
            "scene_name": scene_name,
            "release_group": release_group,
        },
        DOWNLOAD_RECORD_TTL,
    )


def carry_remembered_download(old_path, new_path, ttl=None):
    """Move the release data of a download to its new path after a rename.

    ttl gives the data a new life in seconds. A held import uses it,
    because it can wait for a decision longer than an import runs."""

    if old_path == new_path:
        return
    new_key = DOWNLOAD_RECORD_KEY.format(path=new_path)
    try:
        current_app.redis.rename(DOWNLOAD_RECORD_KEY.format(path=old_path), new_key)
    except redis.exceptions.ResponseError:
        return  # No release data was kept for the old path.
    if ttl:
        current_app.redis.expire(new_key, ttl)


def take_remembered_download(path):
    """Return the release data kept for the import of path, and delete it.

    Return None when nothing was kept."""

    raw = current_app.redis.getdel(DOWNLOAD_RECORD_KEY.format(path=path))
    return json.loads(raw) if raw else None


# A run of the record update makes 1 rescan (#680). The rescan can fail
# or not end in time, or the record is not at the path yet. Then the run
# books the task again after RESTORE_RETRY_DELAY. The last try is
# RESTORE_MAX_TRIES, about 1 hour after the import. A season batch makes
# the rescans of Sonarr fail while it imports. It is over in that time.

RESTORE_RETRY_DELAY = timedelta(minutes=10)
RESTORE_MAX_TRIES = 6


def restore_download_record(
    arr,
    call,
    owner_id,
    library_path,
    scene_name,
    release_group,
    wait,
    tries=1,
    prepare=None,
):
    """Write the release data of a download onto the arr record of its library file.

    The import stored the file under a different name than arr gave it
    (#671). arr rescans, and it then has a new record at library_path
    with no scene name and no release group. This function finds that
    record and PUTs it back whole with the 2 values. arr keeps a scene
    name only when it is a scene title. It keeps a release group always.
    Return True if a record was written.

    A run makes 1 rescan (#680). The rescan can fail or not end in time.
    arr can also give back an older rescan that runs already, or be
    down. Then no record is written yet, and the task books itself again
    with the same data and tries 1 higher. The last try writes 1
    warning, and the release data is lost. A record outside the root
    folders, or a library file that is gone, stops the tries. prepare
    runs first in each try. tries is the number of this try."""

    if not os.path.exists(library_path):
        current_app.logger.info(
            f"'{library_path}' is not in the library. Its {arr.name} record "
            f"gets no release data"
        )
        return False
    try:
        if prepare is not None:
            prepare()
        written = _write_download_record(
            arr, call, owner_id, library_path, scene_name, release_group, wait
        )
    except (OSError, arr.error) as error:
        written = f"The {arr.name} call failed: {error}"
    if isinstance(written, bool):
        return written
    if tries >= RESTORE_MAX_TRIES:
        current_app.logger.warning(
            f"'{library_path}' {written}. After {tries} tries, the release data "
            f"is lost, and the {arr.name} record keeps no release group"
        )
        return False
    _book_restore_try(
        arr, owner_id, library_path, scene_name, release_group, tries + 1, written
    )
    return False


def _write_download_record(
    arr, call, owner_id, library_path, scene_name, release_group, wait
):
    """Make 1 rescan, and write the release data onto the record at library_path.

    Return True if a record was written. Return False if the record is
    outside the root folders. Return the reason as text when no record
    is at the path yet. A rescan that fails raises the error of arr."""

    run_command(arr, call, rescan_command(arr, owner_id), wait, "rescan")
    wanted = os.path.realpath(library_path)
    records = call("GET", f"/api/v3/{arr.record}?{arr.owner_id}={int(owner_id)}")
    for record in records:
        path = record.get("path")
        if path and os.path.realpath(path) == wanted:
            if fenced_arr_path(arr.name, path) is None:
                return False
            record["sceneName"] = scene_name
            record["releaseGroup"] = release_group
            call("PUT", f"/api/v3/{arr.record}/{int(record['id'])}", record)
            current_app.logger.info(
                f"'{library_path}' {arr.name} file record {record['id']} has "
                f"the release data of the download again"
            )
            return True
    return f"{arr.name} has no file record at this path after the rescan"


def _book_restore_try(
    arr, owner_id, library_path, scene_name, release_group, tries, reason
):
    """Book the record update of library_path again after RESTORE_RETRY_DELAY.

    The job id holds the path and the try. Thus, it differs from the id
    of the running job. A booking of the same path that waits goes
    first. Thus, 1 path has at most 1 booking. A finished job under the
    new id is deleted, and the booking does not take its expiry (#371)."""

    from rq.registry import ScheduledJobRegistry

    from app.tasks import (
        TASKS,
        delete_job,
        free_job_id,
        requeue,
        requeue_job_id,
        resolve_queue,
    )

    name = arr.restore_task
    queue = resolve_queue(TASKS[name].queue)
    for job_id in ScheduledJobRegistry(queue=queue).get_job_ids():
        job = queue.fetch_job(job_id)
        if job and job.func_name == name and job.args[1:2] == (library_path,):
            delete_job(job)
    counters = {"tries": tries}
    if not free_job_id(name, requeue_job_id(name, library_path, counters)):
        current_app.logger.info(
            f"'{library_path}' Try {tries} of the {arr.name} record update "
            f"waits in the queue already"
        )
        return
    requeue(
        name,
        key=library_path,
        delay=RESTORE_RETRY_DELAY,
        reason=(
            f"has no release data yet ({reason}). Try {tries} of "
            f"{RESTORE_MAX_TRIES}"
        ),
        counters=counters,
        args=(owner_id, library_path, scene_name, release_group),
        description=f"'{os.path.basename(library_path)}' Updating the "
        f"{arr.name} record",
        level="info",
    )


def rename_download_locally(arr, downloaded_path, new_path, owner_id, title=None):
    """Rename a download in Fitzflix, then ask arr to rescan.

    This was the only way before #288 and #289. It stays as the fallback
    when arr cannot rename its own file. Then arr can lose the file. arr
    can send a webhook when Fitzflix has no address for it. The rename
    is done in that case too. Thus, the caller still gets to the
    localization.

    A call that renames nothing sends no command to arr (#672). A rescan
    then changed nothing on disk. It made Sonarr drop the records of the
    episodes that the import had stored under the Fitzflix name."""

    from app.api.arr import send_arr_command

    if downloaded_path == new_path:
        return
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


def fenced_arr_path(service, path):
    """Return the path that an arr reports if it is under a root folder, or None.

    The webhook payload goes through the root-folder fence of
    downloaded_path. The path of an arr file record must go through the
    same fence (#399). A misconfigured arr must not point an import at a
    file outside the root folders. A refusal is logged."""

    from app.api.arr import folder_under_roots

    if path and folder_under_roots(service, path) is not None:
        return path
    current_app.logger.warning(
        f"{service} reports {path!r}, which is not under a root folder. "
        f"Fitzflix does not use it"
    )
    return None


def fallback_rename(downloaded_path, new_path, arr_path, rename_locally, service=None):
    """Rename a download in Fitzflix after the arr rename failed, and return
    the path to import.

    The arr can complete its rename after Fitzflix stops the wait (#325).
    For example, the RenameFiles command waits behind a long refresh. Then
    the download is not at its old path. Fitzflix asks the arr for the path
    of its record, then looks for the new name. A failure is logged, and
    the error goes to the job. Thus, the job stays in the failed jobs for a
    retry. With a service name, the path of the record must be under a
    root folder of that service."""

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
        if path and service:
            path = fenced_arr_path(service, path)
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
    Thus, the name of arr has the quality of Fitzflix. If arr cannot do
    it, Fitzflix renames the file as before (rename_locally). In both
    cases the import goes on the queue after the rename. The release data
    that the webhook kept moves to the new path (#671)."""

    from app.importing import enqueue_localization

    try:
        path = rename_to_quality(arr, call, owner_id, file_id, quality, wait)
        if fenced_arr_path(arr.name, path) is None:
            raise arr.error(f"{arr.name} reports {path!r}, outside the root folders")
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
            service=arr.name,
        )
    carry_remembered_download(downloaded_path, path)
    queued = (
        enqueue_localization(path, at_front=True)
        if at_front
        else enqueue_localization(path)
    )
    if queued:
        current_app.logger.info(f"'{path}' Sent to Fitzflix")
    return True
