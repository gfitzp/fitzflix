import json
import re
import secrets

from datetime import datetime, timezone

from flask import current_app, jsonify, request
from flask_login import current_user

from app import db
from app.api import bp
from app.api.auth import authenticate_api_request
from app.models import Movie


@bp.route("/queue-details")
def queue_details():
    """Return the number of tasks in queue, and details on tasks currently running.

    This endpoint is checked every 5 seconds so the website can update the current number
    of tasks in queue, and the details of the tasks that are currently running.

    No @login_required: this is polled via XHR, so an expired session should get
    a clean 401 JSON response instead of a redirect to the HTML login page.
    """

    if current_user.is_authenticated:
        details = current_user.get_queue_details()

        # The per-file pipeline trails (#18): where each recent file
        # sits in its journey through the import pipeline

        from app.pipeline import pipeline_trails

        details["files"] = pipeline_trails(current_app.redis)
        return jsonify(details)

    # The user could not be authenticated, return a 401 http error code

    return jsonify({}), 401


@bp.route("/plex/webhook/<token>", methods=["POST"])
def plex_webhook(token):
    """Plex webhook receiver: record movie scrobbles as watches.

    Plex webhooks can't carry auth headers, so a secret path segment gates
    the endpoint (404 when unset or wrong, indistinguishable from a missing
    route). Scrobbles enqueue the same apply task the history poller uses;
    the shared dedup marker keeps the two sources from double-counting.
    """

    expected = current_app.config["PLEX_WEBHOOK_TOKEN"]
    if not expected or not secrets.compare_digest(token, expected):
        return jsonify({}), 404

    # Plex posts multipart form data with the JSON in a "payload" field
    # (plus an optional thumbnail file); accept a raw JSON body too

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

    tmdb_id = None
    for guid in metadata.get("Guid") or []:
        match = re.match(r"tmdb://(\d+)", guid.get("id") or "")
        if match:
            tmdb_id = int(match.group(1))
            break
    if tmdb_id is None:
        # Legacy metadata agent: the guid is a single string
        match = re.search(r"themoviedb://(\d+)", metadata.get("guid") or "")
        if match:
            tmdb_id = int(match.group(1))
    if tmdb_id is None:
        current_app.logger.info(
            f"Plex scrobble of '{metadata.get('title')}' has no TMDb guid; ignoring"
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


@bp.route("/add-to-cart", methods=["POST"])
def add_to_cart():
    """Endpoint for adding movies to the shopping cart."""

    current_app.logger.info(f"Authorization: *redacted*, Request: {request.get_json()}")
    payload = request.get_json()
    response = jsonify({})

    if not request.authorization:
        response.status_code = 401
        return response

    if request.authorization.get("username") and request.authorization.get("password"):
        # The password field must hold the user's API key

        if authenticate_api_request() is None:
            response.status_code = 401
            return response

        # Build the response before setting the status code; jsonify() returns
        # a fresh response object, so setting the code first would discard it

        response = jsonify(request.get_json())
        response.status_code = 202

        cart_item = Movie.query.filter_by(tmdb_id=int(payload["tmdb_id"])).first()
        if not cart_item:
            response.status_code = 500
            return response

        current_app.logger.info(f"Adding to shopping cart: {cart_item}")

        cart_item.shopping_cart_add_date = datetime.now(timezone.utc)
        if cart_item.shopping_cart_priority is None:
            cart_item.shopping_cart_priority = 1
        else:
            cart_item.shopping_cart_priority = cart_item.shopping_cart_priority + 1
        db.session.commit()

        return response

    else:
        response.status_code = 401

    return response
