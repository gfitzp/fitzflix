import os
import shutil

from datetime import date, datetime

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


@bp.route("/sonarr/add", methods=["POST"])
@import_event_webhook("Sonarr")
def sonarr_add(payload):
    """Process the notification from Sonarr that a new video file is added."""

    response = jsonify(request.get_json())
    downloaded_file_path = os.path.join(
        payload["series"].get("path"),
        payload["episodeFile"].get("relativePath"),
    )

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

    # Rename the downloaded file with a downgraded quality title

    original_quality = payload["episodeFile"].get("quality")
    new_quality = downgrade_quality_title(
        original_quality,
        (payload.get("customFormatInfo") or {}).get("customFormatScore", 0),
    )
    sonarr_file_name = os.path.basename(downloaded_file_path).replace(
        f"[{original_quality}]", f"[{new_quality}]"
    )
    sonarr_file_path = os.path.join(
        os.path.dirname(downloaded_file_path), sonarr_file_name
    )
    if downloaded_file_path != sonarr_file_path:
        shutil.move(downloaded_file_path, sonarr_file_path)
        current_app.logger.info(
            f"'{downloaded_file_path}' renamed as '{sonarr_file_path}'"
        )

    # If the episode aired in the last 14 days, add it to the front of the queue.

    today = date.today()
    airdate = payload["episodes"][0].get("airDate")
    at_front = False
    if airdate:
        airdate = datetime.strptime(airdate, "%Y-%m-%d").date()
        aired_days_ago = (today - airdate).days
        current_app.logger.info(
            f"'{os.path.basename(sonarr_file_path)}' aired {aired_days_ago} day(s) ago"
        )
        if aired_days_ago <= 14:
            at_front = True
            current_app.logger.info(
                f"'{os.path.basename(sonarr_file_path)}' Import will be prioritized"
            )

    # Ask Sonarr to refresh its series data, because the file possibly has a new
    # name.

    series = payload.get("series")
    id = series.get("id")
    if id:
        current_app.logger.info(f"Rescanning series '{series.get('title')}'")
        send_arr_command(
            "Sonarr",
            current_app.config["SONARR_URL"] + "/api/v3/command",
            current_app.config["SONARR_API_KEY"],
            {"name": "RescanSeries", "seriesId": int(id)},
        )

    # Pass the file to Fitzflix for processing. The first attempt copied the
    # file to the import directory. If a second file arrived during the copy,
    # the first copy stopped and was lost. The second attempt made a hard link
    # in the import directory. The NAS did not support hard links. Thus,
    # Fitzflix imports the downloaded file directly, in place.

    job = current_app.import_queue.enqueue(
        "app.videos.localization_task",
        args=(sonarr_file_path,),
        job_timeout=current_app.config["LOCALIZATION_TASK_TIMEOUT"],
        description=f"'{os.path.basename(sonarr_file_path)}'",
        job_id=safe_job_id(os.path.basename(sonarr_file_path)),
        at_front=at_front,
    )
    if job:
        current_app.logger.info(f"'{sonarr_file_path}' Sent to Fitzflix")

    else:
        response.status_code = 500

    return response
