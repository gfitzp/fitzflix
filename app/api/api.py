import json
import re
import secrets

from datetime import datetime, timezone

from flask import current_app, jsonify, request
from flask_login import current_user

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

        # The per-file pipeline trails show where each recent file is in
        # the import pipeline. The poll of the queue page uses the default
        # of 25. The dedicated pipeline page asks for the full retained set
        # with ?files=. Fitzflix clamps that number to what Redis keeps.

        from app.pipeline import ACTIVE_LIMIT, pipeline_trails

        limit = request.args.get("files", 25, type=int) or 25
        details["files"] = pipeline_trails(
            current_app.redis, limit=max(1, min(limit, ACTIVE_LIMIT))
        )
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

    expected = current_app.config["PLEX_WEBHOOK_TOKEN"]
    if not expected or not secrets.compare_digest(token, expected):
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

    metadata = payload.get("Metadata") or {}
    if payload.get("event") != "media.scrobble" or metadata.get("type") != "movie":
        return "", 204

    # Live TV airings also have the type "movie". Plex scrobbles them
    # again and again while a channel plays. A virtual-channel surf (#182)
    # must never write a diary watch. The match that Plex made for the
    # airing is not important.

    if metadata.get("live"):
        return "", 204

    tmdb_id = None
    for guid in metadata.get("Guid") or []:
        match = re.match(r"tmdb://(\d+)", guid.get("id") or "")
        if match:
            tmdb_id = int(match.group(1))
            break
    if tmdb_id is None:
        # The legacy metadata agent sends the guid as one string.
        match = re.search(r"themoviedb://(\d+)", metadata.get("guid") or "")
        if match:
            tmdb_id = int(match.group(1))
    if tmdb_id is None:
        current_app.logger.info(
            f"Plex scrobble of '{metadata.get('title')}' has no TMDB guid; ignoring"
        )
        return "", 204

    username = (payload.get("Account") or {}).get("title") or ""
    current_app.sql_queue.enqueue(
        "app.videos.apply_plex_watch",
        args=(
            tmdb_id,
            username,
            datetime.now(timezone.utc).isoformat(),
            "webhook",
        ),
        job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
        description=f"Recording Plex watch of tmdb:{tmdb_id} by {username}",
    )
    return "", 204
