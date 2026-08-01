"""Shared plumbing for the Sonarr and Radarr import webhooks."""

import functools
import json

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
