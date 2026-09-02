"""Shared functions for the Sonarr and Radarr import webhooks."""

import functools
import json
import os

import urllib3

from flask import current_app, jsonify, request

from app.api.auth import authenticate_api_request


def import_event_webhook(service):
    """Wrap a webhook handler with authentication and an import-event filter.

    The wrapper calls the handler with the request payload only for an
    authenticated "Download" (import or upgrade) event. The wrapper
    acknowledges a connection test and all other event types. Then it
    ignores them.
    """

    def decorator(handler):
        @functools.wraps(handler)
        def wrapper():
            current_app.logger.info(
                f"Authorization: *redacted*, Request: {request.get_json()}"
            )
            payload = request.get_json()
            response = jsonify({})

            if not request.authorization:
                response.status_code = 401
                return response

            if not (
                request.authorization.get("username")
                and request.authorization.get("password")
            ):
                response.status_code = 401
                return response

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

            # If the service only confirms the connection, return a valid
            # status code.

            response.status_code = 202
            if payload.get("eventType") == "Test":
                return response

            # Only an import event carries the file fields that the handlers
            # use. Acknowledge and ignore all other events.

            if payload.get("eventType") != "Download":
                current_app.logger.info(
                    f"Ignoring {service} '{payload.get('eventType')}' event"
                )
                return response

            return handler(payload)

        return wrapper

    return decorator


def downloaded_path(service, folder, relative):
    """The absolute path of the file a webhook payload names, or None
    when it doesn't sit under one of the service's root folders —
    RADARR_ROOT_FOLDERS / SONARR_ROOT_FOLDERS, which default to the
    movie and TV library directories because that is where this
    deployment's apps import into. The handlers rename, delete, and
    enqueue by this path, so the payload's two halves aren't trusted
    to compose one: an absolute or parent-hopping relativePath, or a
    folder outside every root, is refused (security review, Sept
    2026)."""

    roots = current_app.config[
        "RADARR_ROOT_FOLDERS" if service == "Radarr" else "SONARR_ROOT_FOLDERS"
    ]
    if not folder or not relative or os.path.isabs(relative):
        return None
    if os.pardir in relative.split(os.sep):
        return None
    path = os.path.realpath(os.path.join(folder, relative))
    for root in roots:
        root = os.path.realpath(root)
        if os.path.commonpath([path, root]) == root:
            return path
    return None


def downgrade_quality_title(original_quality, custom_format_score):
    """Downgrade a Sonarr/Radarr quality title to its web-sourced equivalent.

    If Fitzflix does not know that a file came from physical media, it must
    not use a physical-media quality title. Thus, it uses the next highest
    quality. "Remux" also does not identify a Bluray rip:

        DVD                 -> WEBDL-480p
        Bluray-480p         -> WEBDL-480p
        Bluray-720p         -> WEBDL-720p
        Bluray-1080p        -> WEBDL-1080p
        Bluray-1080p Remux  -> WEBDL-1080p
        Remux-1080p         -> WEBDL-1080p

    A download with a score below the custom-format threshold gets the
    label WEBRip instead of WEBDL.
    """

    new_quality = (
        original_quality.replace("DVD", "WEBDL-480p")
        .replace("Bluray", "WEBDL")
        .replace("Remux-", "WEBDL-")
        .replace(" Remux", "")
    )
    if custom_format_score < 1600:
        new_quality = new_quality.replace("WEBDL", "WEBRip")
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

    from app.email import send_email as send_email_async
    from app.models import User

    basename = os.path.basename(file_path)
    base_url = current_app.config.get(f"{service.upper()}_URL")
    api_key = current_app.config.get(f"{service.upper()}_API_KEY")
    current_app.logger.error(
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
        else "Marking the release failed did NOT succeed — the file was "
        "left in place for manual handling."
    )
    admin_user = User.query.filter(User.admin == True).first()
    send_email_async(
        "Fitzflix - Incomplete download rejected",
        sender=("Fitzflix", current_app.config["SERVER_EMAIL"]),
        recipients=[admin_user.email],
        text_body=(
            f"'{basename}' arrived structurally incomplete and was not "
            f"imported.\n\n{outcome}"
        ),
        html_body=(
            f"<p>'{basename}' arrived structurally incomplete and was "
            f"not imported.</p><p>{outcome}</p>"
        ),
    )
    return failed


def send_arr_command(service, url, api_key, body):
    """POST a command to a Sonarr/Radarr API. Log a failure. Do not raise.

    This function uses urllib3, not requests. The requests version crashed
    with a segmentation fault on this machine many times. The cause was
    never found.
    """

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
