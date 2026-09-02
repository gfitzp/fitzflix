import os
import shutil

from flask import current_app, jsonify, request

from app import safe_job_id
from app.api import bp
from app.api.arr import (
    downgrade_quality_title,
    import_event_webhook,
    import_source_incomplete,
    reject_incomplete_download,
    send_arr_command,
)


@bp.route("/radarr/add", methods=["POST"])
@import_event_webhook("Radarr")
def radarr_add(payload):
    """Receive the Radarr notification that a new video file was added."""

    response = jsonify(request.get_json())
    downloaded_file_path = os.path.join(
        payload["movie"].get("folderPath"),
        payload["movieFile"].get("relativePath"),
    )

    # A download that is truncated per its container never reaches the
    # pipeline. Fitzflix marks the grab as failed. Thus, Radarr blocklists
    # the release and searches again.

    if import_source_incomplete(downloaded_file_path):
        movie_id = payload["movie"].get("id")
        reject_incomplete_download(
            "Radarr",
            payload,
            downloaded_file_path,
            {"name": "RefreshMovie", "movieIds": [int(movie_id)]} if movie_id else None,
        )
        return response

    # Rename the downloaded file with a downgraded quality title.

    original_quality = payload["movieFile"].get("quality")
    new_quality = downgrade_quality_title(
        original_quality,
        (payload.get("customFormatInfo") or {}).get("customFormatScore", 0),
    )
    radarr_file_name = os.path.basename(downloaded_file_path).replace(
        f"[{original_quality}]", f"[{new_quality}]"
    )
    radarr_file_path = os.path.join(
        os.path.dirname(downloaded_file_path), radarr_file_name
    )
    if downloaded_file_path != radarr_file_path:
        shutil.move(downloaded_file_path, radarr_file_path)
        current_app.logger.info(
            f"'{downloaded_file_path}' renamed as '{radarr_file_path}'"
        )

    # Ask Radarr to refresh its movie data, because the file possibly has
    # a new name.

    id = payload["movie"].get("id")
    if id:
        current_app.logger.info(
            f"Rescanning movie '{os.path.dirname(downloaded_file_path)}'"
        )
        send_arr_command(
            "Radarr",
            current_app.config["RADARR_URL"] + "/api/v3/command",
            current_app.config["RADARR_API_KEY"],
            {"name": "RefreshMovie", "movieIds": [int(id)]},
        )

    # Send the file to Fitzflix for processing. An earlier version copied the
    # file to the import directory. But if a second file arrived during the
    # copy, the first copy was abandoned. A later version made a hard link in
    # the import directory. But the NAS did not support hard links. Thus,
    # Fitzflix now imports the downloaded file in place.

    job = current_app.import_queue.enqueue(
        "app.videos.localization_task",
        args=(radarr_file_path,),
        job_timeout=current_app.config["LOCALIZATION_TASK_TIMEOUT"],
        description=f"'{os.path.basename(radarr_file_path)}'",
        job_id=safe_job_id(os.path.basename(radarr_file_path)),
    )
    if job:
        current_app.logger.info(f"'{radarr_file_path}' Sent to Fitzflix")

    else:
        response.status_code = 500

    return response
