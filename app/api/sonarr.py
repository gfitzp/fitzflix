import os

from datetime import date, datetime

from flask import current_app, jsonify, request

from app.tasks import enqueue_task
from app.api import bp
from app.settings import get_setting
from app.api.arr import (
    downgrade_quality_title,
    downloaded_path,
    import_event_webhook,
    import_source_incomplete,
    reject_incomplete_download,
)
from app.sonarr_push import rename_download_locally, sonarr_configured


@bp.route("/sonarr/add", methods=["POST"])
@import_event_webhook("Sonarr")
def sonarr_add(payload):
    """Process the notification from Sonarr that a new video file is added."""

    response = jsonify(request.get_json())
    folder = payload["series"].get("path")
    relative = payload["episodeFile"].get("relativePath")
    downloaded_file_path = downloaded_path("Sonarr", folder, relative)
    if downloaded_file_path is None:
        current_app.logger.warning(
            f"Sonarr webhook named a file outside SONARR_ROOT_FOLDERS "
            f"({folder!r} + {relative!r}); refusing it"
        )
        response.status_code = 400
        return response

    # A download that is provably truncated never goes into the pipeline.
    # Fitzflix marks the download as failed. Thus, Sonarr blocklists the
    # download and searches again.

    if import_source_incomplete(downloaded_file_path):
        series_id = payload["series"].get("id")
        reject_incomplete_download(
            "Sonarr",
            payload,
            downloaded_file_path,
            {"name": "RescanSeries", "seriesId": int(series_id)} if series_id else None,
        )
        return response

    # Find the name with the downgraded quality title.

    original_quality = payload["episodeFile"].get("quality")
    new_quality = downgrade_quality_title(
        original_quality,
        (payload.get("customFormatInfo") or {}).get("customFormatScore", 0),
        get_setting("SONARR_WEBDL_MIN_SCORE"),
    )
    sonarr_file_name = os.path.basename(downloaded_file_path).replace(
        f"[{original_quality}]", f"[{new_quality}]"
    )
    sonarr_file_path = os.path.join(
        os.path.dirname(downloaded_file_path), sonarr_file_name
    )

    # If the episode aired in the last 14 days, add it to the front of the queue.

    today = date.today()
    airdate = payload["episodes"][0].get("airDate")
    at_front = False
    if airdate:
        airdate = datetime.strptime(airdate, "%Y-%m-%d").date()
        aired_days_ago = (today - airdate).days
        current_app.logger.info(
            f"'{sonarr_file_name}' aired {aired_days_ago} day(s) ago"
        )
        if aired_days_ago <= 14:
            at_front = True
            current_app.logger.info(f"'{sonarr_file_name}' Import will be prioritized")

    series = payload.get("series") or {}
    series_id = series.get("id")
    episode_file_id = payload["episodeFile"].get("id")

    # A rename in Fitzflix can make Sonarr lose its file, as it did for
    # Radarr (#288). Thus, Sonarr changes the quality of its own record
    # and renames the file, in a task. The task then queues the import
    # (#289).

    if (
        downloaded_file_path != sonarr_file_path
        and series_id
        and episode_file_id
        and sonarr_configured()
    ):
        enqueue_task(
            "app.sonarr_push.adopt_episode_task",
            int(series_id),
            int(episode_file_id),
            downloaded_file_path,
            sonarr_file_path,
            new_quality,
            at_front,
            description=f"'{sonarr_file_name}' Setting the Sonarr quality",
            result_ttl=86400,
        )
        current_app.logger.info(
            f"'{downloaded_file_path}' Sonarr renames it as '{sonarr_file_name}'"
        )
        return response

    # Pass the file to Fitzflix for processing. The first attempt copied the
    # file to the import directory. If a second file arrived during the copy,
    # the first copy stopped and was lost. The second attempt made a hard link
    # in the import directory. The NAS did not support hard links. Thus,
    # Fitzflix imports the downloaded file directly, in place. A second
    # webhook for the same file adds no second job (#287).

    from app.importing import enqueue_localization

    rename_download_locally(
        downloaded_file_path, sonarr_file_path, series_id, series.get("title")
    )
    if enqueue_localization(sonarr_file_path, at_front=at_front):
        current_app.logger.info(f"'{sonarr_file_path}' Sent to Fitzflix")
    return response
