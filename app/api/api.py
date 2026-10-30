import json
import re
import secrets

from datetime import datetime, timezone

from flask import current_app, jsonify, request
from flask_login import current_user

from app.tasks import enqueue_task
from app.api import bp


@bp.route("/queue-details")
def queue_details():
    """Return the number of queued tasks and the details of the running tasks.

    The website polls this endpoint every 5 seconds. Then it updates the
    number of queued tasks and the details of the tasks that run now.

    This route has no @login_required. The browser polls it through XHR.
    Thus, an expired session must get a clean 401 JSON response, not a
    redirect to the HTML login page.
    """

    if current_user.is_authenticated:
        details = current_user.get_queue_details()

        # A Redis outage gives an empty queue, not a 500 (#476). The
        # poll continues, and the badge fills again when Redis is back.

        if not details:
            return jsonify({"count": 0, "running": [], "all": []})
        return jsonify(details)

    # The user is not authenticated. Return a 401 HTTP error code.

    return jsonify({}), 401


@bp.route("/plex/webhook/<token>", methods=["POST"])
def plex_webhook(token):
    """Receive Plex webhooks and record movie scrobbles as watches.

    Plex webhooks cannot carry auth headers. Thus, a secret path segment
    gates the endpoint. A missing or wrong token gets a 404. That response
    is the same as for a missing route. A scrobble enqueues the same apply
    task that the history poller uses. The shared dedup marker prevents a
    double count from the two sources.
    """

    # The compare is on bytes. A non-ASCII str makes compare_digest raise.

    expected = current_app.config["PLEX_WEBHOOK_TOKEN"]
    if not expected or not secrets.compare_digest(token.encode(), expected.encode()):
        return jsonify({}), 404

    # Plex posts multipart form data with the JSON in a "payload" field
    # and an optional thumbnail file. Fitzflix also accepts a raw JSON body.

    try:
        if "payload" in request.form:
            payload = json.loads(request.form["payload"])
        else:
            payload = request.get_json(force=True, silent=True) or {}
    except Exception:
        payload = {}

    # A list, a string, or a number is not a Plex event (#449).

    if not isinstance(payload, dict):
        payload = {}
    metadata = payload.get("Metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    if payload.get("event") != "media.scrobble" or metadata.get("type") != "movie":
        return "", 204

    # Live TV airings also have the type "movie". Plex scrobbles them
    # again and again while a channel plays. A virtual-channel surf (#182)
    # must never write a diary watch. The match that Plex made for the
    # airing is not important.

    if metadata.get("live"):
        return "", 204

    tmdb_id = None
    guids = metadata.get("Guid")
    for guid in guids if isinstance(guids, list) else []:
        guid_id = guid.get("id") if isinstance(guid, dict) else None
        match = re.match(r"tmdb://(\d+)", guid_id if isinstance(guid_id, str) else "")
        if match:
            tmdb_id = int(match.group(1))
            break
    legacy_guid = metadata.get("guid")
    if tmdb_id is None and isinstance(legacy_guid, str):
        # The legacy metadata agent sends the guid as one string.
        match = re.search(r"themoviedb://(\d+)", legacy_guid)
        if match:
            tmdb_id = int(match.group(1))
    if tmdb_id is None:
        current_app.logger.info(
            f"Plex scrobble of '{metadata.get('title')}' has no TMDB guid; ignoring"
        )
        return "", 204

    # The Account block can be a list or a string in a shaped body (#507).

    account = payload.get("Account")
    username = account.get("title") if isinstance(account, dict) else None
    username = username if isinstance(username, str) else ""
    enqueue_task(
        "app.videos.apply_plex_watch",
        tmdb_id,
        username,
        datetime.now(timezone.utc).isoformat(),
        "webhook",
        description=f"Recording Plex watch of tmdb:{tmdb_id} by {username}",
    )
    return "", 204
