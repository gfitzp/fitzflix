from rq.registry import StartedJobRegistry

from flask import current_app, jsonify
from flask_login import current_user, login_required

from app import db
from app.api import bp


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
