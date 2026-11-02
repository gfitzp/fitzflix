"""Receive the Radarr webhook: imports, and the poster restore after a delete."""

from flask import current_app

from app.api import bp
from app.api.arr import folder_under_roots, handle_import_event, import_event_webhook
from app.arr_client import RADARR


def check_movie_file_delete(payload):
    """Queue the poster restore of a folder after Radarr deletes a movie file (#654).

    Radarr links the poster copy of a folder to the movie file of the
    folder, and deletes the copy with the file. A rip upgrade in
    Fitzflix makes the old file go missing at the next Radarr scan.
    The event carries movie.folderPath. The folder must be below
    RADARR_ROOT_FOLDERS, as the folder of a download must be."""

    from app.custom_artwork import queue_folder_poster_restore

    if payload.get("eventType") != "MovieFileDelete":
        return
    movie = payload.get("movie")
    folder = movie.get("folderPath") if isinstance(movie, dict) else None
    if not isinstance(folder, str) or folder_under_roots(RADARR.name, folder) is None:
        current_app.logger.warning(
            f"Radarr 'MovieFileDelete' event named the folder {folder!r}, "
            f"outside RADARR_ROOT_FOLDERS. Ignored"
        )
        return
    queue_folder_poster_restore(folder, payload.get("deleteReason"))


@bp.route("/radarr/add", methods=["POST"])
@import_event_webhook("Radarr", observe=check_movie_file_delete)
def radarr_add(payload):
    """Receive the Radarr notification that a new video file was added."""

    return handle_import_event(RADARR, payload)
