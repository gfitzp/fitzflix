"""Shared plumbing for the Sonarr and Radarr import webhooks."""

import functools
import json
import os

import urllib3

from flask import current_app, jsonify, request

from app.api.auth import authenticate_api_request


def import_event_webhook(service):
    """Wrap a webhook handler with authentication and import-event filtering.

    The wrapped handler is called with the request payload only for
    authenticated "Download" (import/upgrade) events; connection tests and
    any other event types the webhook is configured to send are acknowledged
    and ignored.
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

            # The password field must hold the user's API key

            if authenticate_api_request() is None:
                response.status_code = 401
                return response

            # If the service is just confirming the connection, return a
            # valid status code

            response.status_code = 202
            if payload.get("eventType") == "Test":
                return response

            # Only import events carry the file fields the handlers use;
            # acknowledge and ignore anything else

            if payload.get("eventType") != "Download":
                current_app.logger.info(
                    f"Ignoring {service} '{payload.get('eventType')}' event"
                )
                return response

            return handler(payload)

        return wrapper

    return decorator


def downgrade_quality_title(original_quality, custom_format_score):
    """Downgrade a Sonarr/Radarr quality title to its web-sourced equivalent.

    If a file isn't specifically known to be from physical media, we don't
    want to use a physical media quality title, so we instead use the next
    highest quality; "Remux" also isn't used to indicate a Bluray rip:

        DVD                 -> WEBDL-480p
        Bluray-480p         -> WEBDL-480p
        Bluray-720p         -> WEBDL-720p
        Bluray-1080p        -> WEBDL-1080p
        Bluray-1080p Remux  -> WEBDL-1080p
        Remux-1080p         -> WEBDL-1080p

    Downloads scoring below the custom-format threshold are labeled WEBRip
    instead of WEBDL.
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
    """Whether a webhook's source file is structurally incomplete —
    provably truncated per its own container (#73). Only a definite
    truncation verdict counts: an unprobeable file proceeds normally
    and the import pipeline's own retries deal with it."""

    from app.videos import probe_file_completeness

    return probe_file_completeness(file_path) is False


def mark_grab_failed(service, base_url, api_key, download_id):
    """Find a download's grab in Sonarr/Radarr history and mark it
    failed — the app then blocklists the release and, per its
    redownload setting, searches for a replacement. Returns True when
    the failed-mark was accepted."""

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
    """The webhook's incomplete-file path (#73): a structurally
    incomplete download never reaches the import pipeline. The grab is
    marked failed in the sending app — which blocklists the release
    and searches for a replacement — and only when that mark took does
    the junk file get deleted (with a rescan command so the app
    notices); otherwise the file stays put for manual handling. The
    admin is emailed either way."""

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
    """POST a command to a Sonarr/Radarr API, logging failures without raising.

    Uses urllib3 rather than requests: the requests version kept crashing
    with a segmentation fault on this machine, for reasons never determined.
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
            # Bounded, so a wedged Sonarr/Radarr can't hang a worker
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
