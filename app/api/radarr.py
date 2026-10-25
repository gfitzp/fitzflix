from app.api import bp
from app.api.arr import handle_import_event, import_event_webhook
from app.arr_client import RADARR


@bp.route("/radarr/add", methods=["POST"])
@import_event_webhook("Radarr")
def radarr_add(payload):
    """Receive the Radarr notification that a new video file was added."""

    return handle_import_event(RADARR, payload)
