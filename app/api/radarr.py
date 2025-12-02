import json
import os
import shutil

from datetime import date, datetime

import requests
import urllib3

from flask import current_app, jsonify, request

from app import create_app, db
from app.api import bp
from app.models import TVSeries, User


@bp.route("/radarr/add", methods=["POST"])
def radarr_add():
    """Endpoint for Radarr to notify Fitzflix when a new video file is added."""

    current_app.logger.info(
        f"Authorization: *redacted*, Request: {request.get_json()}"
    )
    payload = request.get_json()
    response = jsonify({})

    if not request.authorization:
        response.status_code = 401
        return response

    if request.authorization.get("username") and request.authorization.get("password"):
        # Check the user email and password to confirm if they're a valid user

        user = User.query.filter_by(email=request.authorization.get("username")).first()
        if user is None or not user.check_password(
            request.authorization.get("password")
        ):
            response.status_code = 401
            return response

        # If Radarr is just confirming the connection, return a valid status code

        response.status_code = 202
        if payload.get("eventType") == "Test":
            return response

        response = jsonify(request.get_json())
        current_app.logger.info(f"Request: {request.get_json() or {}}")
        downloaded_file_path = os.path.join(
            payload["movie"].get("folderPath"),
            payload["movieFile"].get("relativePath"),
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

        original_quality = payload["movieFile"].get("quality")
        new_quality = (
            original_quality.replace("DVD", "WEBDL-480p")
            .replace("Bluray", "WEBDL")
            .replace("Remux-", "WEBDL-")
            .replace(" Remux", "")
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

        # Ask Radarr to refresh its series data now that we've possibly renamed the file

        id = payload["movie"].get("id")
        if id:
            current_app.logger.info(
                f"Rescanning movie '{os.path.dirname(downloaded_file_path)}'"
            )

            # r = requests.post(
            #     current_app.config["radarr_URL"] + "/api/command",
            #     params={"apikey": current_app.config["radarr_API_KEY"]},
            #     json={"name": "RescanSeries", "seriesId": int(id)},
            # )
            # current_app.logger.info(r.json())

            # I *would* have used the requests code above to submit the API call to Radarr
            # to refresh the series, but it keeps crashing with a segmentation fault.
            # No idea why, because the same code works perfectly fine on my local machine.
            # Using the urllib3 code below to make the API call instead.

            http = urllib3.PoolManager()
            r = http.request(
                "POST",
                current_app.config["RADARR_URL"] + "/api/v3/command",
                headers={
                    "X-Api-Key": current_app.config["RADARR_API_KEY"],
                    "Content-Type": "application/json",
                },
                body=json.dumps({"name": "RefreshMovie", "movieIds": int(id)}).encode(
                    "utf-8"
                ),
            )

        # Pass the file to Fitzflix for processing; tried copying the file to the import
        # directory for processing but if another file came in while it was copying
        # then the first copy was abandoned, and tried doing a hard link to the import
        # directory but that wasn't supported on my NAS, so just sending the downloaded
        # file directly to Radarr to be imported in place

        job = current_app.import_queue.enqueue(
            "app.videos.localization_task",
            args=(radarr_file_path,),
            job_timeout=current_app.config["LOCALIZATION_TASK_TIMEOUT"],
            description=f"'{os.path.basename(radarr_file_path)}'",
            job_id=os.path.basename(radarr_file_path),
        )
        if job:
            current_app.logger.info(f"'{radarr_file_path}' Sent to Fitzflix")

        else:
            response.status_code = 500

    else:
        response.status_code = 401

    return response
