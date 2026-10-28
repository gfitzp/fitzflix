"""Receive the Sonarr webhook: imports, and the folder check of a series."""

from datetime import date, datetime

from flask import current_app

from app.api import bp
from app.api.arr import folder_under_roots, handle_import_event, import_event_webhook
from app.arr_client import SONARR

# An episode that aired this many days ago or less goes to the front of
# the import queue.

RECENT_EPISODE_DAYS = 14


def check_series_folder(payload):
    """Queue the Sonarr path check of a series if its folder differs (#370).

    A Rename, a Download and a SeriesAdd event carry series.path. A
    Rename also carries the old and new path of each renamed episode
    file. The path must be below SONARR_ROOT_FOLDERS, as the path of a
    download must be. A path outside them is logged and ignored."""

    from app.series_rename import SONARR_SERIES_EVENTS, queue_series_reconcile

    event = payload.get("eventType")
    if event not in SONARR_SERIES_EVENTS:
        return
    series = payload.get("series") or {}
    if folder_under_roots(SONARR.name, series.get("path")) is None:
        current_app.logger.warning(
            f"Sonarr '{event}' event named the series folder "
            f"{series.get('path')!r}, outside SONARR_ROOT_FOLDERS. Ignored"
        )
        return
    renamed = payload.get("renamedEpisodeFiles") if event == "Rename" else None
    queue_series_reconcile(event, series, renamed)


@bp.route("/sonarr/add", methods=["POST"])
@import_event_webhook("Sonarr", observe=check_series_folder)
def sonarr_add(payload):
    """Process the notification from Sonarr that a new video file is added."""

    def recently_aired(new_name):
        """Return True if the episode aired in the last RECENT_EPISODE_DAYS."""

        airdate = (payload.get("episodes") or [{}])[0].get("airDate")
        if not airdate:
            return False
        # A malformed airDate is not a recent episode. It does not stop
        # the import (#507).
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
