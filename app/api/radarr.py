import os

from flask import current_app, jsonify, request

from app.api import bp
from app.radarr_push import (
    enqueue_radarr_push,
    radarr_configured,
    rename_download_locally,
)
from app.api.arr import (
    downgrade_quality_title,
    downloaded_path,
    import_event_webhook,
    import_source_incomplete,
    reject_incomplete_download,
)


@bp.route("/radarr/add", methods=["POST"])
@import_event_webhook("Radarr")
def radarr_add(payload):
    """Receive the Radarr notification that a new video file was added."""

    response = jsonify(request.get_json())
    folder = payload["movie"].get("folderPath")
    relative = payload["movieFile"].get("relativePath")
    downloaded_file_path = downloaded_path("Radarr", folder, relative)
    if downloaded_file_path is None:
        current_app.logger.warning(
            f"Radarr webhook named a file outside RADARR_ROOT_FOLDERS "
            f"({folder!r} + {relative!r}); refusing it"
        )
        response.status_code = 400
        return response

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
    movie_id = payload["movie"].get("id")
    movie_file_id = payload["movieFile"].get("id")

    # A rename in Fitzflix made Radarr lose its file. Radarr then
    # downloaded the film again (#288). Thus, Radarr changes the quality
    # of its own record and renames the file, in a task. The task then
    # queues the import. This keeps the webhook short.

    if (
        downloaded_file_path != radarr_file_path
        and movie_id
        and movie_file_id
        and radarr_configured()
    ):
        enqueue_radarr_push(
            "app.radarr_push.adopt_download_task",
            int(movie_id),
            int(movie_file_id),
            downloaded_file_path,
            radarr_file_path,
            new_quality,
            description=f"'{radarr_file_name}' Setting the Radarr quality",
        )
        current_app.logger.info(
            f"'{downloaded_file_path}' Radarr renames it as '{radarr_file_name}'"
        )
        return response

    # Send the file to Fitzflix for processing. An earlier version copied the
    # file to the import directory. But if a second file arrived during the
    # copy, the first copy was abandoned. A later version made a hard link in
    # the import directory. But the NAS did not support hard links. Thus,
    # Fitzflix now imports the downloaded file in place. A second webhook for
    # the same file adds no second job (#287).

    from app.importing import enqueue_localization

    rename_download_locally(downloaded_file_path, radarr_file_path, movie_id)
    if enqueue_localization(radarr_file_path):
        current_app.logger.info(f"'{radarr_file_path}' Sent to Fitzflix")
    return response
