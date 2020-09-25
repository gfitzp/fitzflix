import json
import os
import shutil

import requests

from flask import current_app, jsonify, request

from app import create_app, db
from app.api import bp
from app.models import TVSeries, User


@bp.route("/sonarr/add", methods=["POST"])
def add():
    """Endpoint for Sonarr to notify Fitzflix when a new video file is added."""

    current_app.logger.debug(
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

        # If Sonarr is just confirming the connection, return a valid status code

        response.status_code = 202
        if payload.get("eventType") == "Test":
            return response

        response = jsonify(request.get_json())
        current_app.logger.debug(f"Request: {request.get_json() or {}}")
        downloaded_file_path = os.path.join(
            payload["series"].get("path"), payload["episodeFile"].get("relativePath"),
        )

        # Downgrade quality title and rename the downloaded file.
        # If a file isn't specifically known to be from physical media, I don't want to
        # use a physical media quality title, so I instead use the next highest quality.
        # Also, I don't use "Remux" to indicate a Bluray rip.
        # So, if a file is listed as...             show as...
        #                   - DVD                   WEBDL-480p
        #                   - Bluray-480p           WEBDL-480p
        #                   - Bluray-720p           WEBDL-720p
        #                   - Bluray-1080p          WEBDL-1080p
        #                   - Bluray-1080p Remux    WEBDL-1080p

        original_quality = payload["episodeFile"].get("quality")
        new_quality = (
            original_quality.replace("DVD", "WEBDL-480p")
            .replace("Bluray", "WEBDL")
            .replace(" Remux", "")
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

        # Ask Sonarr to refresh its series data now that we've possibly renamed the file

        series = payload.get("series")
        id = series.get("id")
        if id:
            current_app.logger.info(f"Rescanning series '{series.get('title')}'")
            params = {"apikey": current_app.config["SONARR_API_KEY"]}
            data = {"name": "RescanSeries", "seriesId": series.get("id")}
            r = requests.post(
                current_app.config["SONARR_URL"] + "/api/command",
                params=params,
                data=json.dumps(data),
            )
            current_app.logger.debug(r.json())

        # Pass the file to Fitzflix for processing; tried copying the file to the import
        # directory for processing but if another file came in while it was copying
        # then the first copy was abandoned, and tried doing a hard link to the import
        # directory but that wasn't supported on my NAS, so just sending the downloaded
        # file directly to Sonarr to be imported in place

        current_app.logger.info(f"'{sonarr_file_path}' Sending to Fitzflix")
        job = current_app.task_queue.enqueue(
            "app.videos.localization_task",
            args=(sonarr_file_path,),
            job_timeout=current_app.config["LOCALIZATION_TASK_TIMEOUT"],
            description=f"'{os.path.basename(sonarr_file_path)}'",
            job_id=os.path.basename(sonarr_file_path),
        )

    else:
        response.status_code = 401

    return response
