"""Shared functions for the Sonarr and Radarr import webhooks."""

import functools
import json
import os
import traceback

import urllib3

from flask import current_app, jsonify, request
from markupsafe import escape

from app.api.auth import authenticate_api_request

# The webhook writes at most this many characters of a payload to the log.
# An event of Radarr or Sonarr is some kilobytes.

LOGGED_PAYLOAD_CHARS = 4000


def import_event_webhook(service, observe=None):
    """Wrap a webhook handler with authentication and an import-event filter.

    The wrapper calls the handler with the request payload only for an
    authenticated "Download" (import or upgrade) event. The wrapper
    acknowledges a connection test and all other event types. Then it
    ignores them. observe is a function of the payload. It sees each
    authenticated event except a test before the filter. A failure of
    observe is logged and does not change the answer.

    The authentication runs before the wrapper reads the body (#398). A
    caller without a valid key gets 401, and nothing of the body goes to
    the log. The log line holds only the first part of a large payload.
    """

    def decorator(handler):
        @functools.wraps(handler)
        def wrapper():
            response = jsonify({})

            # The password field must hold the API key of the user. The
            # user must be an ADMIN. The handlers rename, delete, and
            # import files at the paths that the payload names. Thus, the
            # key of a member account must not open them (security review,
            # 2026-09).

            user = authenticate_api_request()
            if user is None:
                response.status_code = 401
                return response
            if not user.admin:
                current_app.logger.warning(
                    f"{service} webhook authenticated as non-admin "
                    f"'{user.email}'; the webhook needs an admin's API key"
                )
                response.status_code = 403
                return response

            payload = request.get_json(silent=True)
            logged = str(payload)
            if len(logged) > LOGGED_PAYLOAD_CHARS:
                logged = logged[:LOGGED_PAYLOAD_CHARS] + " [cut]"
            current_app.logger.info(f"Authorization: *redacted*, Request: {logged}")
            if not isinstance(payload, dict):
                response.status_code = 400
                return response

            # If the service only confirms the connection, return a valid
            # status code.

            response.status_code = 202
            if payload.get("eventType") == "Test":
                return response

            # A download that the app cannot import waits for a person.
            # Record it for the Manual imports page (#276).

            if payload.get("eventType") == "ManualInteractionRequired":
                from app.manual_imports import record_manual_interaction

                record_manual_interaction(service, payload)
                return response

            # Sonarr compares the series folder of an event here (#370).
            # The answer to an ignored event stays 202.

            if observe is not None:
                try:
                    observe(payload)
                except Exception:
                    current_app.logger.warning(
                        f"{service} webhook: the event check failed: "
                        + traceback.format_exc()
                    )

            # Only an import event carries the file fields that the handlers
            # use. Acknowledge and ignore all other events.

            if payload.get("eventType") != "Download":
                current_app.logger.info(
                    f"Ignoring {service} '{payload.get('eventType')}' event"
                )
                return response

            # A body without the file fields gets a 400, not a 500 (#448).

            problem = download_payload_problem(service, payload)
            if problem:
                current_app.logger.warning(
                    f"{service} Download webhook refused: {problem}"
                )
                response.status_code = 400
                return response

            return handler(payload)

        return wrapper

    return decorator


def _is_int(value):
    """Return True if value is an integer. A JSON true is not one."""

    return isinstance(value, int) and not isinstance(value, bool)


def download_payload_problem(service, payload):
    """Return the reason that a Download payload is not usable, or None (#448).

    The file block must be a dict with a string quality and a string
    relativePath. A Sonarr payload also needs a list of episodes. The
    movie or series block and the custom format block must be objects
    when they are present (#507)."""

    file_key = "movieFile" if service == "Radarr" else "episodeFile"
    owner_key = "movie" if service == "Radarr" else "series"
    if not isinstance(payload.get(owner_key) or {}, dict):
        return f"the {owner_key} block is not an object"
    # An id goes to int() and to the rescan command (#577). A text or a
    # list id answered 500.
    owner_id = (payload.get(owner_key) or {}).get("id")
    if owner_id is not None and not _is_int(owner_id):
        return f"{owner_key}.id is not an integer"
    custom_format = payload.get("customFormatInfo")
    if custom_format is not None and not isinstance(custom_format, dict):
        return "the customFormatInfo block is not an object"
    media_file = payload.get(file_key)
    if not isinstance(media_file, dict):
        return f"the {file_key} block is missing"
    for field in ("quality", "relativePath"):
        if not isinstance(media_file.get(field), str):
            return f"{file_key}.{field} is missing"
    if media_file.get("id") is not None and not _is_int(media_file.get("id")):
        return f"{file_key}.id is not an integer"
    if service == "Sonarr":
        episodes = payload.get("episodes")
        if not isinstance(episodes, list) or not episodes:
            return "the episodes list is missing or empty"
        if not isinstance(episodes[0], dict):
            return "the first episode is not an object"
    return None


def _root_folders(service):
    """Return the real paths of the root folders of Radarr or Sonarr."""

    roots = current_app.config[
        "RADARR_ROOT_FOLDERS" if service == "Radarr" else "SONARR_ROOT_FOLDERS"
    ]
    return [os.path.realpath(root) for root in roots]


def folder_under_roots(service, folder):
    """Return the real path of a folder below a root folder, or None.

    This is the fence of downloaded_path for a folder that a payload
    names, for example series.path (#370). The folder must be absolute.
    A root folder itself is not the folder of a series or a movie."""

    if not folder or not os.path.isabs(folder):
        return None
    path = os.path.realpath(folder)
    for root in _root_folders(service):
        if path != root and os.path.commonpath([path, root]) == root:
            return path
    return None


def downloaded_path(service, folder, relative):
    """Return the absolute path of the file that a webhook payload names.

    Return None if the file is not under a root folder of the service.
    The root folders are RADARR_ROOT_FOLDERS and SONARR_ROOT_FOLDERS.
    They default to the movie and TV library directories, because the
    apps of this deployment import into those directories. The
    handlers rename, delete, and enqueue by this path. Thus, this
    function does not trust the 2 halves of the payload to compose
    one path. It refuses an absolute relativePath. It refuses a
    relativePath that steps into a parent directory. It refuses a
    folder outside every root (security review, 2026-09)."""

    if not folder or not relative or os.path.isabs(relative):
        return None
    if os.pardir in relative.split(os.sep):
        return None
    path = os.path.realpath(os.path.join(folder, relative))
    for root in _root_folders(service):
        if os.path.commonpath([path, root]) == root:
            return path
    return None


# The web equivalent of each physical-media quality of Sonarr and
# Radarr. Each value is a RefQuality title. A title that is not a key does
# not change. An explicit map replaces the earlier chain of substring
# replacements. That chain made titles such as "WEBDL-480p-R" from
# "DVD-R" (#506).

WEB_EQUIVALENT_QUALITY = {
    "DVD": "WEBDL-480p",
    "DVD-R": "WEBDL-480p",
    "DVDSCR": "WEBDL-480p",
    "Bluray-480p": "WEBDL-480p",
    "Bluray-576p": "WEBDL-480p",
    "Bluray-720p": "WEBDL-720p",
    "Bluray-1080p": "WEBDL-1080p",
    "Bluray-1080p Remux": "WEBDL-1080p",
    "Remux-1080p": "WEBDL-1080p",
    "Bluray-2160p": "WEBDL-2160p",
    "Bluray-2160p Remux": "WEBDL-2160p",
    "Remux-2160p": "WEBDL-2160p",
}


def downgrade_quality_title(original_quality, custom_format_score, min_score):
    """Downgrade a Sonarr/Radarr quality title to its web-sourced equivalent.

    If Fitzflix does not know that a file came from physical media, it must
    not use a physical-media quality title. Thus, it uses the next highest
    quality. "Remux" also does not identify a Bluray rip:

        - DVD, DVD-R, DVDSCR  -> WEBDL-480p
        - Bluray-480p, -576p  -> WEBDL-480p
        - Bluray-720p         -> WEBDL-720p
        - Bluray-1080p        -> WEBDL-1080p
        - Bluray-1080p Remux  -> WEBDL-1080p
        - Remux-1080p         -> WEBDL-1080p

    WEB_EQUIVALENT_QUALITY holds the full map. A download with a
    custom-format score below min_score gets the label WEBRip instead of
    WEBDL. The callers read min_score from the setting of their service
    (#290).
    """

    new_quality = WEB_EQUIVALENT_QUALITY.get(original_quality, original_quality or "")
    if custom_format_score < min_score and new_quality.startswith("WEBDL-"):
        new_quality = "WEBRip-" + new_quality.removeprefix("WEBDL-")
    return new_quality


def import_source_incomplete(file_path):
    """Return True if the source file of a webhook is incomplete.

    Incomplete means truncated per its own container. Only a definite
    truncation verdict counts. A file that the probe cannot read continues
    normally. The retries of the import pipeline then deal with it."""

    from app.videos import probe_file_completeness

    return probe_file_completeness(file_path) is False


def mark_grab_failed(service, base_url, api_key, download_id):
    """Find the grab of a download in the Sonarr/Radarr history and mark it
    as failed.

    The app then blocklists the release. If its redownload setting permits,
    the app searches for a replacement. Return True if the app accepted the
    failed-mark."""

    if not (base_url and api_key and download_id):
        return False
    try:
        http = urllib3.PoolManager()
        r = http.request(
            "GET",
            f"{base_url}/api/v3/history",
            fields={
                "page": "1",
                "pageSize": "50",
                "sortKey": "date",
                "sortDirection": "descending",
                "downloadId": download_id,
            },
            headers={"X-Api-Key": api_key},
            timeout=urllib3.Timeout(connect=5, read=30),
            retries=False,
        )
        if not 200 <= r.status < 300:
            current_app.logger.warning(
                f"{service} history lookup returned HTTP {r.status}"
            )
            return False
        records = json.loads(r.data.decode("utf-8")).get("records") or []
        grab = next((rec for rec in records if rec.get("eventType") == "grabbed"), None)
        if grab is None:
            current_app.logger.warning(
                f"{service} history has no grab for download '{download_id}'"
            )
            return False
        r = http.request(
            "POST",
            f"{base_url}/api/v3/history/failed/{grab['id']}",
            headers={"X-Api-Key": api_key},
            timeout=urllib3.Timeout(connect=5, read=30),
            retries=False,
        )
        if not 200 <= r.status < 300:
            current_app.logger.warning(
                f"{service} failed-mark returned HTTP {r.status}"
            )
            return False
        return True

    except Exception as e:
        current_app.logger.warning(
            f"Unable to mark {service} download '{download_id}' failed: {e}"
        )
        return False


def reject_incomplete_download(service, payload, file_path, refresh_command):
    """Reject an incomplete download before it reaches the import pipeline.

    This function marks the grab as failed in the app that sent the
    webhook. That app blocklists the release and searches for a
    replacement. Only if that mark succeeded does this function delete the
    incomplete file. It then sends a rescan command. Thus, the app notices
    the deletion. If the mark did not succeed, the file stays in place for
    manual handling. This function emails the admin in both cases."""

    from app.email import notify_admin_text

    basename = os.path.basename(file_path)
    base_url = current_app.config.get(f"{service.upper()}_URL")
    api_key = current_app.config.get(f"{service.upper()}_API_KEY")
    # The notification below is the alert. An ERROR record would mail the
    # admin a second time (#453).
    current_app.logger.warning(
        f"'{basename}' is structurally incomplete (truncated per its "
        f"container); refusing to import it"
    )

    failed = mark_grab_failed(service, base_url, api_key, payload.get("downloadId"))
    if failed:
        try:
            os.remove(file_path)
        except OSError:
            pass
        if refresh_command:
            send_arr_command(
                service, f"{base_url}/api/v3/command", api_key, refresh_command
            )

    outcome = (
        "The release was marked failed: it is blocklisted, the file was "
        "deleted, and a replacement search was triggered."
        if failed
        else "Marking the release failed did NOT succeed. The file stays "
        "in place for manual handling."
    )
    notify_admin_text(
        "Incomplete download rejected",
        f"'{basename}' arrived structurally incomplete and was not "
        f"imported.\n\n{outcome}",
        f"<p>'{escape(basename)}' arrived structurally incomplete and was "
        f"not imported.</p><p>{escape(outcome)}</p>",
    )
    return failed


def handle_import_event(arr, payload, priority=None):
    """Handle the import event of a Radarr or Sonarr webhook.

    The steps are the same for the 2 apps. Take the path of the file,
    and refuse a path outside the root folders of arr. Reject a truncated
    download. Downgrade the quality title. If the name changes, a task
    lets arr rename its own file (#288, #289), and the task queues the
    import. Otherwise rename the file here and queue the import.
    priority is a function of the new file name. It returns True when
    the import goes to the front of the queue. Only Sonarr gives it, for
    a recent episode."""

    from app.arr_client import configured, rescan_command
    from app.arr_client import rename_download_locally
    from app.importing import enqueue_localization
    from app.settings import get_setting
    from app.tasks import enqueue_task

    response = jsonify(request.get_json())
    owner = payload.get(arr.owner_key) or {}
    media_file = payload[arr.file_key]
    folder = owner.get(arr.folder_field)
    relative = media_file.get("relativePath")
    downloaded_file_path = downloaded_path(arr.name, folder, relative)
    if downloaded_file_path is None:
        current_app.logger.warning(
            f"{arr.name} webhook named a file outside "
            f"{arr.name.upper()}_ROOT_FOLDERS ({folder!r} + {relative!r}); "
            f"refusing it"
        )
        response.status_code = 400
        return response

    # A download that is truncated per its container never reaches the
    # pipeline. Fitzflix marks the grab as failed. Thus, arr blocklists
    # the release and searches again.

    owner_id = owner.get("id")
    if import_source_incomplete(downloaded_file_path):
        reject_incomplete_download(
            arr.name,
            payload,
            downloaded_file_path,
            rescan_command(arr, owner_id) if owner_id else None,
        )
        return response

    # Find the name with the downgraded quality title.

    original_quality = media_file.get("quality")
    # A null or text score counts as 0 (#507).

    score = (payload.get("customFormatInfo") or {}).get("customFormatScore")
    new_quality = downgrade_quality_title(
        original_quality,
        score if isinstance(score, (int, float)) else 0,
        get_setting(arr.min_score_setting),
    )

    # The import parser rejects a quality that is not a RefQuality title.
    # A rename to such a title would make arr lose its file, and the
    # download would go to rejects. Thus, refuse it before any rename
    # (#506).

    from app.models import RefQuality

    if not RefQuality.query.filter_by(quality_title=new_quality).first():
        current_app.logger.warning(
            f"'{downloaded_file_path}' {arr.name} webhook gave the quality "
            f"{original_quality!r}, which Fitzflix does not know; refusing it"
        )
        response.status_code = 400
        return response
    new_name = os.path.basename(downloaded_file_path).replace(
        f"[{original_quality}]", f"[{new_quality}]"
    )
    new_path = os.path.join(os.path.dirname(downloaded_file_path), new_name)
    at_front = bool(priority and priority(new_name))
    file_id = media_file.get("id")

    # A rename in Fitzflix made arr lose its file, and arr downloaded it
    # again (#288). Thus, arr changes the quality of its own record and
    # renames the file, in a task. The task then queues the import. This
    # keeps the webhook short.

    if downloaded_file_path != new_path and owner_id and file_id and configured(arr):
        enqueue_task(
            arr.adopt_task,
            int(owner_id),
            int(file_id),
            downloaded_file_path,
            new_path,
            new_quality,
            *([at_front] if priority else []),
            description=f"'{new_name}' Setting the {arr.name} quality",
            result_ttl=86400,
        )
        current_app.logger.info(
            f"'{downloaded_file_path}' {arr.name} renames it as '{new_name}'"
        )
        return response

    # Import the downloaded file in place. An earlier version copied the
    # file to the import directory. But if a second file arrived during
    # the copy, the first copy was abandoned. A later version made a hard
    # link in the import directory. But the NAS did not support hard
    # links. A second webhook for the same file adds no second job (#287).

    rename_download_locally(
        arr,
        downloaded_file_path,
        new_path,
        owner_id,
        title=owner.get("title") or os.path.dirname(downloaded_file_path),
    )
    queued = (
        enqueue_localization(new_path, at_front=True)
        if at_front
        else enqueue_localization(new_path)
    )
    if queued:
        current_app.logger.info(f"'{new_path}' Sent to Fitzflix")
    return response


def send_arr_command(service, url, api_key, body):
    """POST a command to a Sonarr/Radarr API. Log a failure. Do not raise.

    This function uses urllib3. The docstring of app.arr_client records
    the choice of the HTTP library."""

    try:
        http = urllib3.PoolManager()
        r = http.request(
            "POST",
            url,
            headers={
                "X-Api-Key": api_key,
                "Content-Type": "application/json",
            },
            body=json.dumps(body).encode("utf-8"),
            # The timeout is bounded. Thus, a stalled Sonarr/Radarr cannot hang
            # a worker.
            timeout=urllib3.Timeout(connect=5, read=30),
            retries=False,
        )
        if not 200 <= r.status < 300:
            current_app.logger.warning(
                f"{service} {body.get('name')} command returned HTTP {r.status}: "
                f"{r.data.decode('utf-8', 'replace')[:200]}"
            )

    except Exception as e:
        current_app.logger.warning(
            f"Unable to send {body.get('name')} command to {service}: {e}"
        )
