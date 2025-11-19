from datetime import datetime, timedelta, timezone

from rq.registry import StartedJobRegistry

from flask import current_app, jsonify, request
from flask_login import current_user, login_required

from app import db
from app.api import bp
from app.models import Movie, User

import requests


@bp.route("/queue-details")
@login_required
def queue_details():
    """Return the number of tasks in queue, and details on tasks currently running.

    This endpoint is checked every 5 seconds so the website can update the current number
    of tasks in queue, and the details of the tasks that are currently running.
    """

    if current_user.is_authenticated:
        return jsonify(current_user.get_queue_details())

    else:
        # The user could not be authenticated, return a 401 http error code

        return jsonify({}), 401


@bp.route("/add-to-cart", methods=["POST"])
def add_to_cart():
    """Endpoint for adding movies to the shopping cart."""

    current_app.logger.info(
        f"Authorization: {request.authorization}, Request: {request.get_json()}"
    )
    payload = request.get_json()
    response = jsonify({})
    if request.authorization.get("username") and request.authorization.get("password"):
        # Check the user email and password to confirm if they're a valid user

        user = User.query.filter_by(email=request.authorization.get("username")).first()
        if user is None or not user.check_password(
            request.authorization.get("password")
        ):
            response.status_code = 401
            return response

        response.status_code = 202
    response = jsonify(request.get_json())
    current_app.logger.info(f"Request: {request.get_json() or {}}")

    cart_item = Movie.query.filter_by(tmdb_id=int(payload["tmdb_id"])).first()
    if not cart_item:
        response.status_code = 500

    current_app.logger.info(cart_item)

    cart_item.shopping_cart_add_date = datetime.now(timezone.utc)
    if cart_item.shopping_cart_priority is None:
        cart_item.shopping_cart_priority = 1
    else:
        cart_item.shopping_cart_priority = cart_item.shopping_cart_priority + 1
    db.session.commit()

    return response