"""Receive the Sonarr webhook for the imports."""

from datetime import date, datetime

from flask import current_app

from app.api import bp
from app.api.arr import handle_import_event, import_event_webhook
from app.arr.arr_client import SONARR

# An episode that aired this many days ago or less goes to the front of
# the import queue.

RECENT_EPISODE_DAYS = 14


@bp.route("/sonarr/add", methods=["POST"])
@import_event_webhook("Sonarr")
def sonarr_add(payload):
    """Process the notification from Sonarr that a new video file is added."""

    def recently_aired(new_name):
        """Return True if the episode aired in the last RECENT_EPISODE_DAYS."""

        airdate = (payload.get("episodes") or [{}])[0].get("airDate")
        if not airdate:
            return False
        # A malformed airDate is not a recent episode. It does not stop
        # the import.
        try:
            aired = datetime.strptime(str(airdate), "%Y-%m-%d").date()
        except ValueError:
            current_app.logger.warning(
                f"'{new_name}' has an airDate that is not a date ({airdate!r})"
            )
            return False
        aired_days_ago = (date.today() - aired).days
        current_app.logger.info(f"'{new_name}' aired {aired_days_ago} day(s) ago")
        if aired_days_ago <= RECENT_EPISODE_DAYS:
            current_app.logger.info(f"'{new_name}' Import will be prioritized")
            return True
        return False

    return handle_import_event(SONARR, payload, priority=recently_aired)
