import csv
import hashlib
import io
import json
import math
import os
import pathlib
import random
import re
import shutil
import subprocess
import threading
import time
import traceback
import urllib.parse

from datetime import datetime, timedelta, timezone

import boto3
import botocore
import requests
import rq

from botocore.client import Config
from bs4 import BeautifulSoup
from pathvalidate import sanitize_filename
from pymediainfo import MediaInfo
from rq import get_current_job
from rq.registry import StartedJobRegistry
from unidecode import unidecode

from flask import current_app, flash, render_template

from app import create_app, db
from app.email import task_send_email as send_email
from app.models import (
    File,
    FileAudioTrack,
    FileSubtitleTrack,
    Movie,
    RefFeatureType,
    RefQuality,
    TVSeries,
    User,
    UserMovieReview,
)


EIGHT_MEGABYTES = 8388608


class UploadProgressPercentage(object):
    """Return the upload progress as a callback when uploading a file to AWS S3."""

    def __init__(self, file_path):
        self._file_path = file_path
        self._size = float(os.path.getsize(file_path))
        self._seen_so_far = 0
        self._lock = threading.Lock()
        self._job = rq.get_current_job()

    def __call__(self, bytes_amount):
        with self._lock:
            self._seen_so_far += bytes_amount
            progress = int((self._seen_so_far / self._size) * 100)
            app.logger.info(
                f"'{os.path.basename(self._file_path)}' Uploading to AWS: {progress}%"
            )
            if self._job:
                self._job.meta["description"] = (
                    f"'{os.path.basename(self._file_path)}' — Uploading to AWS"
                )
                self._job.meta["progress"] = progress
                self._job.save_meta()


class DownloadProgressPercentage(object):
    """Return the download progress as a callback when downloading a file from AWS S3."""

    def __init__(self, client, bucket, key, basename):
        self._file_path = basename
        app.logger.info(client.head_object(Bucket=bucket, Key=key).get("ContentLength"))
        self._size = client.head_object(Bucket=bucket, Key=key).get("ContentLength", 0)
        self._seen_so_far = 0
        self._lock = threading.Lock()
        self._job = rq.get_current_job()

    def __call__(self, bytes_amount):
        with self._lock:
            self._seen_so_far += bytes_amount
            progress = int((self._seen_so_far / self._size) * 100)
            app.logger.info(
                f"'{os.path.basename(self._file_path)}' Downloading from AWS: {progress}%"
            )
            if self._job:
                self._job.meta["description"] = (
                    f"'{os.path.basename(self._file_path)}' — Downloading from AWS"
                )
                self._job.meta["progress"] = progress
                self._job.save_meta()


# Tasks


def localization_task(file_path, force_upload=False, ignore_etag=False):
    """Archive an untouched file and remove unnecessary language tracks.

    - Untouched file is uploaded to AWS S3 storage for safekeeping.
    - File is localized by keeping all native-language audio and subtitle tracks, as well
      as the first audio track if the first audio track is not in the native language.
    - Pass the localized file to a separate process to add to the database.
    """

    with app.app_context():
        # Initalize the database
        # https://stackoverflow.com/a/60438156
        db.init_app(app)

        try:
            job = get_current_job()

            # If the incoming file doesn't exist, there's nothing for us to do

            if not os.path.exists(file_path):
                return False

            # Parse movie or TV show info from the file name

            basename = os.path.basename(file_path)

            # If the file name contains "temp-1234.", then ignore it
            if re.search("\-temp\-\d+\.", basename):
                return False

            file_details = evaluate_filename(file_path)
            current_app.logger.info(file_details)
            if not file_details:
                current_app.logger.error(
                    f"'{basename}' doesn't match expected naming formats!"
                )
                move_to_rejects(file_path, "incorrect filename")
                return False

            # We don't want to process other versions of this video at the same time,
            # so create a identifier using specific movie or tv show fields to use when
            # creating the lock. If we try to process any other files with this same
            # identifier, the lock will prevent us from processing it until the previous file
            # is done being processed.

            if file_details.get("media_library") == "Movies":
                file_identifier = {
                    "title": file_details.get("title"),
                    "year": file_details.get("year"),
                    "feature_type": file_details.get("feature_type_name"),
                    "plex_title": file_details.get("plex_title"),
                    "version": file_details.get("version"),
                }

            elif file_details.get("media_library") == "TV Shows":
                file_identifier = {
                    "title": file_details.get("title"),
                    "season": file_details.get("season"),
                    "episode": file_details.get("episode"),
                }

            file_identifier = json.dumps(file_identifier)
            current_app.logger.debug(f"'{basename}' Lock identifier: {file_identifier}")

            # Multiply by 1000 since we need to specify the redlock timeout in milliseconds

            lock = current_app.lock_manager.lock(
                file_identifier, current_app.config["LOCALIZATION_TASK_TIMEOUT"] * 1000
            )
            current_app.logger.info(f"Created lock {lock}")

            # If we didn't get a lock, return this task to the localization queue after
            # 45 to 75 minutes to be processed once the lock becomes available

            if not lock:
                sleep_duration = random.randint(45, 75)
                current_app.logger.warning(
                    f"'{basename}' Lock exists, "
                    f"returning to queue after {sleep_duration} minutes"
                )
                current_app.import_scheduler.enqueue_in(
                    timedelta(minutes=sleep_duration),
                    "app.videos.localization_task",
                    file_path=file_path,
                    timeout=current_app.config["LOCALIZATION_TASK_TIMEOUT"],
                    job_description=f"'{basename}'",
                )
                return False

            # See if any better-quality versions of this file already exist

            better_versions = File(**file_details).find_better_files()
            if better_versions:
                current_app.logger.info(
                    f"Better versions of '{basename}' exist; skipping import"
                )
                for better in better_versions:
                    current_app.logger.debug(vars(better))

                move_to_rejects(file_path, "inferior quality")
                current_app.lock_manager.unlock(lock)
                current_app.logger.info(f"Removed lock {lock}")
                admin_user = User.query.filter(User.admin == True).first()
                send_email(
                    "Fitzflix - Received an inferior-quality file",
                    sender=("Fitzflix", current_app.config["SERVER_EMAIL"]),
                    recipients=[admin_user.email],
                    text_body=render_template(
                        "email/inferior_warning.txt",
                        user=admin_user.email,
                        basename=basename,
                        better_versions=better_versions,
                        rejects_directory=current_app.config["REJECTS_DIR"],
                    ),
                    html_body=render_template(
                        "email/inferior_warning.html",
                        user=admin_user.email,
                        basename=basename,
                        better_versions=better_versions,
                        rejects_directory=current_app.config["REJECTS_DIR"],
                    ),
                )

                return False

            # Save the untouched filename in case we need to recreate the file

            file_details["untouched_basename"] = os.path.basename(file_path)

            # Upload the untouched file to AWS S3 storage for safekeeping

            if current_app.config["ARCHIVE_ORIGINAL_MEDIA"]:
                (
                    file_details["aws_untouched_key"],
                    file_details["aws_untouched_date_uploaded"],
                ) = aws_upload(
                    file_path,
                    current_app.config["AWS_UNTOUCHED_PREFIX"],
                    force_upload=force_upload,
                    ignore_etag=ignore_etag,
                )

            # Start localization process

            current_app.logger.info(f"'{basename}' Starting localization process")

            # Determine output directories and files to be created

            output_directory = os.path.join(
                current_app.config["LIBRARY_DIR"], file_details.get("dirname")
            )
            hidden_output_file = os.path.join(
                output_directory, f".{file_details.get('basename')}"
            )

            # Parse the incoming file and get its details with MediaInfo

            current_app.logger.info(f"'{basename}' Parsing with MediaInfo")
            media_info = MediaInfo.parse(file_path)
            current_app.logger.debug(f"'{basename}' -> {media_info.to_json()}")

            for track in media_info.tracks:
                if track.track_type == "General" and track.format:
                    current_app.logger.info(
                        f"'{basename}' File container {track.format}"
                    )
                    file_details["container"] = track.format

            # Export a localized version of the incoming file

            if file_details.get("container") == "Matroska":
                current_app.logger.info(f"'{basename}' Localizing as a Matroska file")

                # If the file isn't from physical media, replace any lossless audio tracks
                # with ones in FLAC so the AppleTV can play them natively.

                lossless_to_flac(file_path)

                # Sometimes the input mkv file is missing track details, such as the number
                # of subtitle elements in a subtitle track, which we need for us to tell
                # whether or not there is possibly a forced subtitle track; this command
                # adds those details to the file if they are missing.

                current_app.logger.info(f"'{basename}' Adding track statistics tags")

                statistics_tags_process = subprocess.Popen(
                    [
                        current_app.config["MKVPROPEDIT_BIN"],
                        "--add-track-statistics-tags",
                        file_path,
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    universal_newlines=True,
                    bufsize=1,
                )
                for line in statistics_tags_process.stdout:
                    progress_match = re.search("Progress\: \d+\%", line)
                    if progress_match:
                        progress_match = re.match(
                            "^Progress\: (?P<percent>\d+)\%", line
                        )
                        progress = int(progress_match.group("percent"))
                        current_app.logger.info(
                            f"'{basename}' Adding track statistics tags: {progress}%"
                        )
                        if job:
                            job.meta["description"] = (
                                f"'{basename}' — Adding track statistics tags"
                            )
                            job.meta["progress"] = progress
                            job.save_meta()

                # Re-parse the file now that the track statistics tags have been added

                current_app.logger.info(
                    f"'{basename}' Parsing added statistics with MediaInfo"
                )
                media_info = MediaInfo.parse(file_path)
                current_app.logger.debug(f"'{basename}' -> {media_info.to_json()}")
                audio_tracks = get_audio_tracks_from_file(file_path)
                subtitle_tracks = get_subtitle_tracks_from_file(file_path)

                # Change from ISO-639-2 to ISO-639-3 language code
                # if the file was written by MakeMKV

                native_language = current_app.config["NATIVE_LANGUAGE"]

                for track in media_info.tracks:
                    if track.writing_application:
                        if (
                            track.track_type == "General"
                            and "MakeMKV" in track.writing_application
                        ):
                            native_language = iso_639_3_native_language()
                            current_app.logger.warning(
                                f"'{basename}' was created with MakeMKV. Will use ISO-639-3 "
                                f"code '{native_language}' instead of user-supplied "
                                f"ISO-639-2 '{current_app.config['NATIVE_LANGUAGE']}' when "
                                f"processing this file with mkvmerge"
                            )

                # Determine which audio tracks to export

                # If there are no audio tracks, then technically we could use the
                # --no-audio flag with mkvmerge. Defaulting to the first audio track we
                # find is good enough, however, as none will exist.

                if len(audio_tracks) == 0:
                    first_audio_track_language = "1"

                elif audio_tracks[0].get("language"):
                    first_audio_track_language = audio_tracks[0].get("language")

                else:
                    first_audio_track_language = 1

                # If the first audio track is in our native language, remove all other languages

                if (
                    len(audio_tracks) >= 1
                    and first_audio_track_language == native_language
                ):
                    current_app.logger.info(
                        f"'{basename}' First audio track matches native language "
                        f"'{native_language}'"
                    )
                    output_audio_langs = native_language

                # If the first audio track isn't our native language, but our language is present,
                # export tracks in the first language + all other native-language audio
                # (it's probably a dub, or there are native-language commentary tracks, etc.)

                elif native_language in [track["language"] for track in audio_tracks]:
                    current_app.logger.info(
                        f"'{basename}' First audio track is foreign, "
                        f"but '{native_language}' audio is present"
                    )
                    output_audio_langs = (
                        f"{first_audio_track_language},{native_language}"
                    )

                # If no native-language track is present, export only tracks in the first
                # language (it's probably a subtitled movie with no commentary track)

                else:
                    current_app.logger.info(
                        f"'{basename}' No '{native_language}' audio track"
                    )
                    output_audio_langs = first_audio_track_language

                # Determine which tracks to export and create the output file

                os.makedirs(output_directory, exist_ok=True)

                # Non-native audio, native-language subtitles present

                if (
                    len(audio_tracks) >= 1
                    and first_audio_track_language != native_language
                    and native_language
                    in [track["language"] for track in subtitle_tracks]
                ):
                    current_app.logger.info(
                        f"'{basename}' Non-native audio, "
                        f"but '{native_language}' subtitles are present"
                    )

                    default_subtitle_tracks = []

                    # Turn on the first native-language subtitle track
                    for i, track in enumerate(subtitle_tracks):
                        if track["language"] == native_language:
                            default_subtitle_tracks.extend(
                                ["--default-track-flag", f"{track['streamorder']}:1"]
                            )
                            first_native_language_sub_track = i
                            break

                    # Turn off all the subsequent native-language subtitle tracks
                    for track in subtitle_tracks[i + 1 :]:
                        if track["language"] == native_language:
                            default_subtitle_tracks.extend(
                                ["--default-track-flag", f"{track['streamorder']}:0"]
                            )

                    mkvmerge_process = subprocess.Popen(
                        [
                            current_app.config["MKVMERGE_BIN"],
                            "-o",
                            hidden_output_file,
                            "-a",
                            output_audio_langs,
                            "-s",
                            native_language,
                        ]
                        + default_subtitle_tracks
                        + [
                            "--title",
                            "",
                            "--track-name",
                            "-1:",
                            file_path,
                        ],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        universal_newlines=True,
                        bufsize=1,
                    )

                # Native-language audio, native-language subtitles present

                elif native_language in [
                    track["language"] for track in subtitle_tracks
                ]:
                    current_app.logger.info(
                        f"'{basename}' '{native_language}' audio and subtitles"
                    )

                    default_subtitle_tracks = []

                    # Since it has native-language audio, turn off all subtitle tracks
                    for track in subtitle_tracks:
                        if track["language"] == native_language:
                            default_subtitle_tracks.extend(
                                ["--default-track-flag", f"{track['streamorder']}:0"]
                            )

                    mkvmerge_process = subprocess.Popen(
                        [
                            current_app.config["MKVMERGE_BIN"],
                            "-o",
                            hidden_output_file,
                            "-a",
                            output_audio_langs,
                            "-s",
                            native_language,
                        ]
                        + default_subtitle_tracks
                        + [
                            "--title",
                            "",
                            "--track-name",
                            "-1:",
                            file_path,
                        ],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        universal_newlines=True,
                        bufsize=1,
                    )

                # No native-language subtitles

                elif len(subtitle_tracks) >= 1:
                    current_app.logger.info(
                        f"'{basename}' No '{native_language}' subtitles"
                    )
                    mkvmerge_process = subprocess.Popen(
                        [
                            current_app.config["MKVMERGE_BIN"],
                            "-o",
                            hidden_output_file,
                            "-a",
                            output_audio_langs,
                            "--no-subtitles",
                            "--title",
                            "",
                            "--track-name",
                            "-1:",
                            file_path,
                        ],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        universal_newlines=True,
                        bufsize=1,
                    )

                # No subtitles whatsoever

                else:
                    current_app.logger.info(f"'{basename}' No subtitles whatsoever")
                    mkvmerge_process = subprocess.Popen(
                        [
                            current_app.config["MKVMERGE_BIN"],
                            "-o",
                            hidden_output_file,
                            "-a",
                            output_audio_langs,
                            "--no-subtitles",
                            "--title",
                            "",
                            "--track-name",
                            "-1:",
                            file_path,
                        ],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        universal_newlines=True,
                        bufsize=1,
                    )

                for line in mkvmerge_process.stdout:
                    progress_match = re.search("Progress\: \d+\%", line)
                    if progress_match:
                        progress_match = re.match(
                            "^Progress\: (?P<percent>\d+)\%", line
                        )
                        progress = int(progress_match.group("percent"))
                        current_app.logger.info(f"'{basename}' Localizing: {progress}%")
                        if job:
                            job.meta["description"] = f"'{basename}' — Localizing"
                            job.meta["progress"] = progress
                            job.save_meta()

            else:
                if file_details.get("container") == "MPEG-4":
                    current_app.logger.info(f"'{basename}' Removing MPEG-4 metadata")
                    atomicparsley_process = subprocess.Popen(
                        [
                            current_app.config["ATOMICPARSLEY_BIN"],
                            file_path,
                            "--metaEnema",
                            "--overWrite",
                        ],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        universal_newlines=True,
                        bufsize=1,
                    )
                    for line in atomicparsley_process.stdout:
                        line = line.replace("\n", "")
                        current_app.logger.info(
                            f"'{os.path.basename(file_path)}' {line}"
                        )
                        if job:
                            job.meta["description"] = (
                                f"'{os.path.basename(file_path)}' — Removing MPEG-4 metadata"
                            )
                            job.meta["progress"] = -1
                            job.save_meta()

                    current_app.logger.info(f"'{basename}' Removed MPEG-4 metadata")

                else:
                    current_app.logger.info(
                        f"'{basename}' Not Matroska or MPEG-4, importing as-is"
                    )

                current_app.logger.info(
                    f"'{basename}' Copying to '{hidden_output_file}'"
                )
                os.makedirs(output_directory, exist_ok=True)
                if job:
                    job.meta["description"] = f"'{basename}' — Copying to destination"
                    job.meta["progress"] = -1
                    job.save_meta()

                shutil.copy(file_path, hidden_output_file)

        except Exception:
            current_app.logger.error(traceback.format_exc())
            move_to_rejects(file_path, "exception")
            current_app.lock_manager.unlock(lock)
            current_app.logger.info(f"Removed lock {lock}")

        else:
            current_app.sql_queue.enqueue(
                "app.videos.finalize_localization",
                args=(file_path, file_details, lock),
                job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
                description=f"'{basename}'",
            )

        return True


def finalize_localization(file_path, file_details, lock):
    """Add a localized file to the database and move it into position.

    - A record of the localized file is added to the database.
    - The movie or tv show is updated with data from either TheMovieDB or TheTVDB.
    - Supplemental movie / tv show files (e.g. images) are downloaded.
    - The localized file is moved into position.
    - Changes are committed to the database.
    """

    with app.app_context():
        # Initalize the database
        # https://stackoverflow.com/a/60438156
        db.init_app(app)

        try:
            job = get_current_job()

            # Determine output directories and file to be created

            output_directory = os.path.join(
                current_app.config["LIBRARY_DIR"], file_details.get("dirname")
            )
            hidden_output_file = os.path.join(
                output_directory, f".{file_details.get('basename')}"
            )
            output_file = os.path.join(
                current_app.config["LIBRARY_DIR"], file_details.get("file_path")
            )

            # See if this File record already exists in the database.
            # If not, create a new one. Otherwise, update that existing record.

            file = File.query.filter_by(file_path=file_details.get("file_path")).first()
            if not file:
                file = File(**file_details)
                current_app.logger.debug(vars(file))
                current_app.logger.info(f"{file} Creating File record")
                db.session.add(file)

            else:
                current_app.logger.info(f"{file} Existing File record found")

                # Clear metadata for existing File record

                file.date_updated = datetime.now(timezone.utc)
                file.date_transcoded = None
                file.date_archived = None
                FileAudioTrack.query.filter_by(file_id=file.id).delete()
                FileSubtitleTrack.query.filter_by(file_id=file.id).delete()

            if file.media_library == "Movies":
                # See if a Movie record already exists; if not, create one.

                current_app.logger.info(
                    f"{file} Searching in Movies table using "
                    f"title='{file_details.get('title')}', year='{file_details.get('year')}'"
                )
                movie = Movie.query.filter_by(
                    title=file_details.get("title"), year=file_details.get("year")
                ).first()
                if not movie:
                    movie = Movie(
                        title=file_details.get("title"), year=file_details.get("year")
                    )
                    current_app.logger.info(f"{file} Creating {movie}")
                    criterion_collection = get_criterion_collection_from_wikipedia()
                    for release in criterion_collection:
                        if (movie.title == release.get("title")) and (
                            movie.year == release.get("year")
                        ):
                            movie.criterion_spine_number = release.get("spine_number")

                            # Decided against automatically adding the box set title
                            # movie.criterion_set_title = release.get("set")

                            movie.criterion_in_print = release.get("in_print")
                            movie.criterion_bluray = release.get("bluray")
                            if movie.criterion_disc_owned == None:
                                movie.criterion_disc_owned = False

                            current_app.logger.info(
                                f"{movie} Assigning Criterion Collection "
                                f"spine #{movie.criterion_spine_number}"
                            )

                    db.session.add(movie)

                file.movie = movie
                current_app.logger.info(f"{file} Associating with {movie}")

                # Set the special feature type if the file is a special feature

                if file_details.get("feature_type_name"):
                    feature_type = RefFeatureType.query.filter_by(
                        feature_type=file_details.get("feature_type_name")
                    ).first()
                    file.feature_type = feature_type
                    current_app.logger.info(f"{file} Marking as {feature_type}")

            elif file.media_library == "TV Shows":
                # See if a TVSeries record exists; if not, create one

                current_app.logger.info(
                    f"{file} Searching in TVSeries table using title='{file_details.get('title')}"
                )
                tv_series = TVSeries.query.filter_by(
                    title=file_details.get("title")
                ).first()
                if not tv_series:
                    tv_series = TVSeries(title=file_details.get("title"))
                    current_app.logger.info(f"{file} Creating {tv_series}")
                    db.session.add(tv_series)

                file.tv_series = tv_series
                current_app.logger.info(f"{file} Associating with {tv_series}")

            # Set file quality details

            quality = RefQuality.query.filter_by(
                quality_title=file_details.get("quality_title")
            ).first()
            file.quality = quality
            current_app.logger.info(f"{file} Setting file_quality {quality}")

            # Parse the localized file and get its details with MediaInfo

            media_info = MediaInfo.parse(hidden_output_file)
            current_app.logger.debug(
                f"'{os.path.basename(hidden_output_file)}' -> {media_info.to_json()}"
            )
            output_audio_tracks = get_audio_tracks_from_file(hidden_output_file)
            output_subtitle_tracks = get_subtitle_tracks_from_file(hidden_output_file)

            # Set file video track info

            for track in media_info.tracks:
                if track.track_type == "Video" and track.format:
                    file.format = track.format
                    break

            for track in media_info.tracks:
                if track.track_type == "Video" and track.codec_id:
                    file.codec = track.codec_id
                    break

            for track in media_info.tracks:
                if track.track_type == "Video" and track.bit_rate:
                    file.video_bitrate_kbps = track.bit_rate / 1000
                    break

            for track in media_info.tracks:
                if track.track_type == "Video" and track.other_hdr_format:
                    if track.other_hdr_format[0]:
                        file.hdr_format = track.other_hdr_format[0]
                        break

            # Put the final touches on the output file and move it into place

            if file_details.get("container") == "Matroska":
                # Set the first audio track as default
                # TODO: set all other audio tracks as flag-default=0

                if len(output_audio_tracks) >= 1:
                    current_app.logger.info(
                        f"'{os.path.basename(hidden_output_file)}' "
                        f"Setting the first audio track as default"
                    )
                    if output_audio_tracks[0].get("language") == "und":
                        mkvpropedit_process = subprocess.Popen(
                            [
                                current_app.config["MKVPROPEDIT_BIN"],
                                hidden_output_file,
                                "--edit",
                                "track:a1",
                                "--set",
                                "flag-default=1",
                                "--edit",
                                "track:a1",
                                "--set",
                                "language=und",
                            ],
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            universal_newlines=True,
                            bufsize=1,
                        )

                    else:
                        mkvpropedit_process = subprocess.Popen(
                            [
                                current_app.config["MKVPROPEDIT_BIN"],
                                hidden_output_file,
                                "--edit",
                                "track:a1",
                                "--set",
                                "flag-default=1",
                            ],
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            universal_newlines=True,
                            bufsize=1,
                        )

                    for line in mkvpropedit_process.stdout:
                        line = line.replace("\n", "")
                        current_app.logger.info(
                            f"'{os.path.basename(hidden_output_file)}' {line}"
                        )

                # Change from ISO-639-2 to ISO-639-3 language code
                # if the file was written by MakeMKV

                native_language = current_app.config["NATIVE_LANGUAGE"]

                for track in media_info.tracks:
                    if (
                        track.track_type == "General"
                        and "MakeMKV" in track.writing_application
                    ):
                        native_language = iso_639_3_native_language()
                        current_app.logger.warning(
                            f"'{basename}' was created with MakeMKV. Will use ISO-639-3 "
                            f"code '{native_language}' instead of user-supplied "
                            f"ISO-639-2 '{current_app.config['NATIVE_LANGUAGE']}' when "
                            f"processing this file with mkvmerge"
                        )

                # Set the first subtitle track as default if the first audio is foreign
                # and if there isn't already a default subtitle track

                existing_default_subtitle_track = False
                for track in output_subtitle_tracks:
                    if track["default"] == True:
                        existing_default_subtitle_track = True

                if (
                    len(output_subtitle_tracks) >= 1
                    and output_audio_tracks[0].get("language") != native_language
                    and output_audio_tracks[0].get("language") != "und"
                    and not existing_default_subtitle_track
                ):
                    current_app.logger.info(
                        f"'{os.path.basename(hidden_output_file)}' "
                        f"Setting the first subtitle track as default"
                    )
                    mkvpropedit_process = subprocess.run(
                        [
                            current_app.config["MKVPROPEDIT_BIN"],
                            hidden_output_file,
                            "--edit",
                            "track:s1",
                            "--set",
                            "flag-default=1",
                        ],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        universal_newlines=True,
                        bufsize=1,
                    )
                    for line in mkvpropedit_process.stdout:
                        line = line.replace("\n", "")
                        current_app.logger.info(
                            f"'{os.path.basename(hidden_output_file)}' {line}"
                        )

                # Rebuild the audio and subtitle track info
                # now that we've possibly made modifications

                output_audio_tracks = get_audio_tracks_from_file(hidden_output_file)
                output_subtitle_tracks = get_subtitle_tracks_from_file(
                    hidden_output_file
                )

            # Set file audio track info

            possibly_foreign_language = False
            first_audio_track_lossy = True
            lossless_audio_track_present = False
            for i, track in enumerate(output_audio_tracks):
                track["file_id"] = file.id
                track["track"] = i + 1
                audio_track = FileAudioTrack(**track)
                file.audio_track = audio_track
                if track["track"] == 1 and audio_track.language not in [
                    current_app.config["NATIVE_LANGUAGE"],
                    "und",
                    "zxx",
                ]:
                    possibly_foreign_language = True
                if (
                    track["track"] == 1
                    and track.get("compression_mode", "Lossy") == "Lossless"
                ):
                    first_audio_track_lossy = False
                    lossless_audio_track_present = True
                elif track.get("compression_mode", "Lossy") == "Lossless":
                    lossless_audio_track_present = True
                current_app.logger.info(f"{file} Adding audio track {audio_track}")
                db.session.add(audio_track)

            # Set file subtitle track info

            possibly_forced_subtitle = False
            if len(output_subtitle_tracks) > 1:
                main_subtitle_track = output_subtitle_tracks[0].get("elements")
                for i, track in enumerate(output_subtitle_tracks[1:]):
                    track_length = track.get("elements")
                    forced_flag = track.get("forced")

                    # If a track is less than 1/3 the length of the first subtitle track,
                    # but it's not marked as forced, speculate that it might be a forced
                    # subtitle track

                    if (
                        track_length > 0
                        and track_length <= (main_subtitle_track * 0.3)
                        and not forced_flag
                    ):
                        current_app.logger.warning(
                            f"{file} Subtitle track {i+2} has {track_length} elements "
                            f"and may be a forced subtitle track!"
                        )
                        output_subtitle_tracks[i + 1]["forced"] = None
                        possibly_forced_subtitle = True

            for i, track in enumerate(output_subtitle_tracks):
                track["file_id"] = file.id
                track["track"] = i + 1
                subtitle_track = FileSubtitleTrack(**track)
                file.subtitle_track = subtitle_track
                current_app.logger.info(
                    f"{file} Adding subtitle track {subtitle_track}"
                )
                db.session.add(subtitle_track)

            # Set the localized date

            file.date_localized = datetime.now(timezone.utc)

            # Set the AWS archived fields if the file was uploaded to AWS S3 storage

            file.aws_untouched_key = file_details.get("aws_untouched_key")
            file.aws_untouched_date_uploaded = file_details.get(
                "aws_untouched_date_uploaded"
            )

            bytes = os.path.getsize(hidden_output_file)
            kilobytes = bytes / 1024
            megabytes = (bytes / 1024) / 1024
            gigabytes = ((bytes / 1024) / 1024) / 1024

            file.filesize_bytes = bytes
            file.filesize_megabytes = round(megabytes, 1)
            file.filesize_gigabytes = round(gigabytes, 1)
            current_app.logger.info(
                f"'{os.path.basename(hidden_output_file)}' {file.filesize_bytes} bytes"
            )

            # Get or refresh movie or tv series details and download images

            try:
                # Establish a savepoint with db.session.begin_nested(), so if any of the
                # queries to get show metadata fail, we can just roll back those changes to
                # the savepoint and still commit the movie / tv show, file, and its tracks.

                savepoint = db.session.begin_nested()
                if file.movie_id:
                    if movie.tmdb_id == None:
                        movie.tmdb_movie_query()

                elif file.series_id:
                    if tv_series.tmdb_id == None:
                        tv_series.tmdb_tv_query()

                savepoint.commit()

            except:
                current_app.logger.error(traceback.format_exc())
                savepoint.rollback()
                pass

            # Find and remove any worse-quality files before moving the new file into place
            # so we don't delete any special features where old and new filenames are the same

            worse_files = file.find_worse_files()
            current_app.logger.info(f"{file} worse files: {worse_files}")

            for worse in worse_files:
                worse.delete_local_file()

                # If the new file is from digital media, delete only worse digital-media files
                # (we always want to keep the best physical-media file)
                #
                # Otherwise, if the new file is from physical media, delete all worse files
                # regardless of media source

                if (
                    worse.quality.physical_media == file.quality.physical_media
                    or file.quality.physical_media == True
                ):
                    if worse.aws_untouched_date_uploaded:
                        # S3 delete logic needs to be placed in here directly, since it
                        # won't work if called with app.app_context() (like in aws_delete())
                        s3_client = boto3.client(
                            "s3",
                            aws_access_key_id=current_app.config["AWS_ACCESS_KEY"],
                            aws_secret_access_key=current_app.config["AWS_SECRET_KEY"],
                        )
                        response = s3_client.delete_object(
                            Bucket=current_app.config["AWS_BUCKET"],
                            Key=worse.aws_untouched_key,
                        )
                    db.session.delete(worse)

                if (
                    worse.quality.physical_media == True
                    and file.quality.physical_media == True
                ):
                    admin_user = User.query.filter(User.admin == True).first()
                    send_email(
                        "Fitzflix - Replaced a physical media file",
                        sender=("Fitzflix", current_app.config["SERVER_EMAIL"]),
                        recipients=[admin_user.email],
                        text_body=render_template(
                            "email/replaced_physical_media.txt",
                            user=admin_user.email,
                            file=file,
                            worse=worse,
                        ),
                        html_body=render_template(
                            "email/replaced_physical_media.html",
                            user=admin_user.email,
                            file=file,
                            worse=worse,
                        ),
                    )

                    if current_app.config["TODO_EMAIL"]:
                        send_email(
                            f"Find and dispose of the media for '{worse.untouched_basename}'",
                            sender=("Fitzflix", current_app.config["SERVER_EMAIL"]),
                            recipients=[current_app.config["TODO_EMAIL"]],
                            text_body=render_template(
                                "email/replaced_physical_media.txt",
                                user=admin_user.email,
                                file=file,
                                worse=worse,
                            ),
                            html_body=render_template(
                                "email/replaced_physical_media.html",
                                user=admin_user.email,
                                file=file,
                                worse=worse,
                            ),
                        )

            # Move the new file into place

            os.rename(hidden_output_file, output_file)

            db.session.commit()

            # Remove the file that was imported unless it was replaced by the localized file
            # (we don't want to remove the file we just created!)

            if file_path != output_file:
                try:
                    os.remove(file_path)

                except FileNotFoundError:
                    pass

            # Pass the TV series title to Sonarr to refresh the series data
            # (ignore any exceptions, since it's no big deal if Sonarr can't refresh)

            if file.series_id:
                try:
                    file.refresh_sonarr()

                except Exception:
                    pass

            if file.media_library == "Movies" and movie.tmdb_id == None:
                admin_user = User.query.filter(User.admin == True).first()
                send_email(
                    "Fitzflix - Added a movie without a TMDb ID",
                    sender=("Fitzflix", current_app.config["SERVER_EMAIL"]),
                    recipients=[admin_user.email],
                    text_body=render_template(
                        "email/no_tmdb_id.txt", user=admin_user.email, movie=movie
                    ),
                    html_body=render_template(
                        "email/no_tmdb_id.html", user=admin_user.email, movie=movie
                    ),
                )

            if possibly_foreign_language == True and len(output_audio_tracks) > 1:
                admin_user = User.query.filter(User.admin == True).first()
                send_email(
                    "Fitzflix - Foreign audio track added",
                    sender=("Fitzflix", current_app.config["SERVER_EMAIL"]),
                    recipients=[admin_user.email],
                    text_body=render_template(
                        "email/possibly_foreign_audio.txt",
                        user=admin_user.email,
                        file=file,
                    ),
                    html_body=render_template(
                        "email/possibly_foreign_audio.html",
                        user=admin_user.email,
                        file=file,
                    ),
                )

            current_app.logger.info(f"{file} File ID {file.id}")

            if possibly_forced_subtitle == True:
                admin_user = User.query.filter(User.admin == True).first()
                send_email(
                    "Fitzflix - Possibly forced subtitle track",
                    sender=("Fitzflix", current_app.config["SERVER_EMAIL"]),
                    recipients=[admin_user.email],
                    text_body=render_template(
                        "email/possibly_forced_subtitle.txt",
                        user=admin_user.email,
                        file=file,
                    ),
                    html_body=render_template(
                        "email/possibly_forced_subtitle.html",
                        user=admin_user.email,
                        file=file,
                    ),
                )

            if first_audio_track_lossy and lossless_audio_track_present:
                admin_user = User.query.filter(User.admin == True).first()
                send_email(
                    "Fitzflix - Added a file that has a lossless audio track ",
                    sender=("Fitzflix", current_app.config["SERVER_EMAIL"]),
                    recipients=[admin_user.email],
                    text_body=render_template(
                        "email/lossy_audio.txt",
                        user=admin_user.email,
                        file=file,
                    ),
                    html_body=render_template(
                        "email/lossy_audio.html",
                        user=admin_user.email,
                        file=file,
                    ),
                )

        except Exception:
            current_app.logger.error(traceback.format_exc())
            move_to_rejects(file_path, "exception")
            db.session.rollback()

        else:
            current_app.logger.info(f"'{file_path}' processed as '{output_file}'")

        finally:
            current_app.lock_manager.unlock(lock)
            current_app.logger.info(f"Removed lock {lock}")


def finalize_transcoding(file_id, lock):
    """Update a file with details about its transcoding and move it into position."""

    with app.app_context():
        # Initalize the database
        # https://stackoverflow.com/a/60438156
        db.init_app(app)

        try:
            job = get_current_job()

            file = File.query.filter_by(id=file_id).first()
            ext = current_app.config["HANDBRAKE_EXTENSION"]

            # Determine output directories and file to be created

            output_directory = os.path.join(
                current_app.config["TRANSCODES_DIR"], file.dirname
            )
            hidden_output_file = os.path.join(
                output_directory, f".{file.plex_title}.{ext}"
            )
            output_file = os.path.join(output_directory, f"{file.plex_title}.{ext}")

            # Move the transcoded file into place

            os.rename(hidden_output_file, output_file)

            # Update the file record with the date it was transcoded
            file.date_transcoded = datetime.now(timezone.utc)

            db.session.commit()

        except Exception:
            current_app.logger.error(traceback.format_exc())
            db.session.rollback()

        else:
            current_app.logger.info(f"{file.plex_title}' Transcode complete")

        finally:
            current_app.lock_manager.unlock(lock)
            current_app.logger.info(f"Removed lock {lock}")


def manual_import_task():
    """Scan the Import directory and import files that aren't already in the queue."""

    with app.app_context():
        # Initalize the database
        # https://stackoverflow.com/a/60438156
        db.init_app(app)

        try:
            import_directory_files = os.listdir(current_app.config["IMPORT_DIR"])
            import_directory_files.sort()
            qualities = (
                db.session.query(RefQuality.quality_title)
                .order_by(RefQuality.preference.asc())
                .all()
            )
            qualities = [quality_title for (quality_title,) in qualities]
            for quality_title in qualities:
                for file in import_directory_files:
                    if (
                        (not os.path.basename(file).startswith("."))
                        and f"[{quality_title}]" in file
                        and os.path.isfile(
                            os.path.join(current_app.config["IMPORT_DIR"], file)
                        )
                    ):
                        lock = current_app.lock_manager.lock(
                            os.path.basename(file), 1000
                        )
                        if lock:
                            job_queue = []
                            localization_tasks_running = StartedJobRegistry(
                                "fitzflix-import", connection=current_app.redis
                            )
                            job_queue.extend(localization_tasks_running.get_job_ids())
                            job_queue.extend(current_app.import_queue.job_ids)
                            if os.path.basename(file) not in job_queue:
                                current_app.logger.info(
                                    f"'{os.path.basename(file)}' Found in import directory"
                                )
                                job = current_app.import_queue.enqueue(
                                    "app.videos.localization_task",
                                    args=(
                                        os.path.join(
                                            current_app.config["IMPORT_DIR"], file
                                        ),
                                    ),
                                    job_timeout=current_app.config[
                                        "LOCALIZATION_TASK_TIMEOUT"
                                    ],
                                    description=f"'{os.path.basename(file)}'",
                                    job_id=os.path.basename(file),
                                )

                            current_app.lock_manager.unlock(lock)

        except Exception:
            current_app.logger.error(traceback.format_exc())

        else:
            return True


def track_metadata_scan_library():
    """Add all files in the library to the metadata scan queue."""

    with app.app_context():
        # Initalize the database
        # https://stackoverflow.com/a/60438156
        db.init_app(app)

        try:
            job = get_current_job()

            files = File.query.all()
            for file in files:
                current_app.sql_queue.enqueue(
                    "app.videos.track_metadata_scan_task",
                    args=(file.id,),
                    job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
                    description=f"{file.basename} – Scanning track metadata",
                )

        except Exception:
            current_app.logger.error(traceback.format_exc())
            raise

        return True


def track_metadata_scan_task(file_id):
    """Scan a file's metadata in the background."""

    with app.app_context():
        # Initalize the database
        # https://stackoverflow.com/a/60438156
        db.init_app(app)

        try:
            job = get_current_job()

            file = File.query.filter_by(id=file_id).first()
            file_path = os.path.join(current_app.config["LIBRARY_DIR"], file.file_path)
            if os.path.isfile(file_path):
                track_metadata_scan(file.id)

        except Exception:
            current_app.logger.error(traceback.format_exc())
            db.session.rollback()
            raise

        else:
            db.session.commit()

        return True


def track_metadata_scan(file_id):
    """Rescan a file's metadata on demand."""

    try:
        file = File.query.filter_by(id=file_id).first()
        file_path = os.path.join(app.config["LIBRARY_DIR"], file.file_path)
        if not os.path.isfile(file_path):
            raise

        # Clear metadata for existing File record

        file.date_updated = datetime.now(timezone.utc)
        FileAudioTrack.query.filter_by(file_id=file.id).delete()
        FileSubtitleTrack.query.filter_by(file_id=file.id).delete()

        media_info = MediaInfo.parse(file_path)
        current_app.logger.info(
            f"'{os.path.basename(file_path)}' -> {media_info.to_json()}"
        )

        bytes = os.path.getsize(file_path)
        kilobytes = bytes / 1024
        megabytes = (bytes / 1024) / 1024
        gigabytes = ((bytes / 1024) / 1024) / 1024

        file.filesize_bytes = bytes
        file.filesize_megabytes = round(megabytes, 1)
        file.filesize_gigabytes = round(gigabytes, 1)
        current_app.logger.info(
            f"'{os.path.basename(file_path)}' {file.filesize_bytes} bytes"
        )

        # Set file video track info

        for track in media_info.tracks:
            if track.track_type == "Video" and track.format:
                file.format = track.format
                break

        for track in media_info.tracks:
            if track.track_type == "Video" and track.codec_id:
                file.codec = track.codec_id
                break

        for track in media_info.tracks:
            if track.track_type == "Video" and track.bit_rate:
                file.video_bitrate_kbps = track.bit_rate / 1000
                break

        for track in media_info.tracks:
            if track.track_type == "Video" and track.other_hdr_format:
                file.hdr_format = track.other_hdr_format[0]
                break

        output_audio_tracks = get_audio_tracks_from_file(file_path)
        output_subtitle_tracks = get_subtitle_tracks_from_file(file_path)

        # Set file audio track info

        possibly_foreign_language = False
        for i, track in enumerate(output_audio_tracks):
            track["file_id"] = file.id
            track["track"] = i + 1
            audio_track = FileAudioTrack(**track)
            file.audio_track = audio_track
            if track["track"] == 1 and audio_track.language not in [
                current_app.config["NATIVE_LANGUAGE"],
                "und",
                "zxx",
            ]:
                possibly_foreign_language = True
            current_app.logger.info(f"{file} Adding audio track {audio_track}")
            db.session.add(audio_track)

        # Set file subtitle track info

        possibly_forced_subtitle = False
        if len(output_subtitle_tracks) > 1:
            main_subtitle_track = output_subtitle_tracks[0].get("elements")
            for i, track in enumerate(output_subtitle_tracks[1:]):
                track_length = track.get("elements")
                forced_flag = track.get("forced")

                # If a track is less than 1/3 the length of the first subtitle track,
                # but it's not marked as forced, speculate that it might be a forced
                # subtitle track

                if (
                    track_length > 0
                    and track_length <= (main_subtitle_track * 0.3)
                    and not forced_flag
                ):
                    current_app.logger.warning(
                        f"{file} Subtitle track {i+2} has {track_length} elements "
                        f"and may be a forced subtitle track!"
                    )
                    output_subtitle_tracks[i + 1]["forced"] = None
                    possibly_forced_subtitle = True

        for i, track in enumerate(output_subtitle_tracks):
            track["file_id"] = file.id
            track["track"] = i + 1
            subtitle_track = FileSubtitleTrack(**track)
            file.subtitle_track = subtitle_track
            current_app.logger.info(f"{file} Adding subtitle track {subtitle_track}")
            db.session.add(subtitle_track)

    except Exception:
        current_app.logger.error(traceback.format_exc())
        db.session.rollback()
        raise

    else:
        db.session.commit()

    return True


def mkvpropedit_task(
    file_id, default_audio_track, default_subtitle_track, forced_subtitle_tracks
):
    """Update a file's MKV properties."""

    with app.app_context():
        # Initalize the database
        # https://stackoverflow.com/a/60438156
        db.init_app(app)

        try:
            job = get_current_job()

            # Get the record of the file to modify

            file = File.query.filter_by(id=file_id).first()
            file_path = os.path.join(app.config["LIBRARY_DIR"], file.file_path)

            if job:
                job.meta["description"] = f"'{file.basename}' — Updating MKV properties"
                job.meta["progress"] = -1
                job.save_meta()

            FileAudioTrack.query.filter_by(file_id=file.id).delete()
            FileSubtitleTrack.query.filter_by(file_id=file.id).delete()

            media_info = MediaInfo.parse(file_path)
            audio_tracks = get_audio_tracks_from_file(file_path)
            subtitle_tracks = get_subtitle_tracks_from_file(file_path)

            current_app.logger.info(f"{file.basename} file_id: {file_id}")
            current_app.logger.info(
                f"{file.basename} selected default_audio_track: {default_audio_track} {type(default_audio_track)}"
            )
            current_app.logger.info(
                f"{file.basename} selected default_subtitle_track: {default_subtitle_track} {type(default_subtitle_track)}"
            )
            current_app.logger.info(
                f"{file.basename} selected forced_subtitle_tracks: {forced_subtitle_tracks} {type(forced_subtitle_tracks)}"
            )

            audio_track_arguments = []
            subtitle_track_arguments = []

            for track_id, track in enumerate(audio_tracks, 1):
                track_id = str(track_id)

                if track_id == default_audio_track:
                    audio_track_arguments.append(
                        f"--edit track:a{track_id} --set flag-default=1"
                    )

                else:
                    audio_track_arguments.append(
                        f"--edit track:a{track_id} --set flag-default=0"
                    )

            if default_subtitle_track or forced_subtitle_tracks:
                for track_id, track in enumerate(subtitle_tracks, 1):
                    track_id = str(track_id)

                    if track_id == default_subtitle_track:
                        subtitle_track_arguments.append(
                            f"--edit track:s{track_id} --set flag-default=1"
                        )

                    else:
                        subtitle_track_arguments.append(
                            f"--edit track:s{track_id} --set flag-default=0"
                        )

                    if track_id in forced_subtitle_tracks:
                        subtitle_track_arguments.append(
                            f"--edit track:s{track_id} --set flag-forced=1"
                        )

                    else:
                        subtitle_track_arguments.append(
                            f"--edit track:s{track_id} --set flag-forced=0"
                        )

            current_app.logger.info(
                f"{file.basename} audio_track_arguments: {audio_track_arguments}"
            )
            current_app.logger.info(
                f"{file.basename} subtitle_track_arguments: {subtitle_track_arguments}"
            )

            # subprocess expects an array of arguments,
            # so we need to split the arguments on spaces
            localization_arguments = []
            for arg in audio_track_arguments:
                localization_arguments.extend(arg.split())

            for arg in subtitle_track_arguments:
                localization_arguments.extend(arg.split())

            current_app.logger.info(
                f"{file.basename} localization_arguments: {localization_arguments}"
            )

            mkvpropedit_task = subprocess.Popen(
                [
                    current_app.config["MKVPROPEDIT_BIN"],
                    file_path,
                ]
                + localization_arguments,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                bufsize=1,
            )
            for line in mkvpropedit_task.stdout:
                line = line.replace("\n", "")
                current_app.logger.info(line)

            # If the default audio track isn't the first track, create a new file with the
            # default audio track prioritized so Plex selects it first

            if default_audio_track != "1":
                new_track_order = []
                media_info = MediaInfo.parse(file_path)

                # Default video tracks
                for track in media_info.tracks:
                    if track.track_type == "Video" and track.default == "Yes":
                        new_track_order.append(f"0:{track.streamorder}")

                # Non-default video tracks
                for track in media_info.tracks:
                    if track.track_type == "Video" and track.default == "No":
                        new_track_order.append(f"0:{track.streamorder}")

                # Default audio tracks
                for track in media_info.tracks:
                    if track.track_type == "Audio" and track.default == "Yes":
                        new_track_order.append(f"0:{track.streamorder}")

                # Non-default audio tracks

                for track in media_info.tracks:
                    if track.track_type == "Audio" and track.default == "No":
                        new_track_order.append(f"0:{track.streamorder}")

                # Default subtitle tracks

                for track in media_info.tracks:
                    if track.track_type == "Text" and track.default == "Yes":
                        new_track_order.append(f"0:{track.streamorder}")

                # Non-default subtitle tracks

                for track in media_info.tracks:
                    if track.track_type == "Text" and track.default == "No":
                        new_track_order.append(f"0:{track.streamorder}")

                new_track_order = ",".join(new_track_order)

                current_app.logger.info(
                    f"{file.basename} new_track_order: {new_track_order}"
                )

                output_directory = os.path.join(
                    current_app.config["LIBRARY_DIR"], file.dirname
                )
                hidden_output_file = os.path.join(output_directory, f".{file.basename}")

                command = [
                    current_app.config["MKVMERGE_BIN"],
                    "--track-order",
                    new_track_order,
                    "-o",
                    hidden_output_file,
                    file_path,
                ]

                current_app.logger.info(command)

                mkvmerge_process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    universal_newlines=True,
                    bufsize=1,
                )

                for line in mkvmerge_process.stdout:
                    progress_match = re.search("Progress\: \d+\%", line)
                    if progress_match:
                        progress_match = re.match(
                            "^Progress\: (?P<percent>\d+)\%", line
                        )
                        progress = int(progress_match.group("percent"))
                        current_app.logger.info(
                            f"'{file.basename}' Remuxing: {progress}%"
                        )
                        if job:
                            job.meta["description"] = f"'{file.basename}' — Remuxing"
                            job.meta["progress"] = progress
                            job.save_meta()

                # Move the new file into place

                os.rename(hidden_output_file, file_path)

            # Rebuild the audio and subtitle track info now that we've made modifications

            output_audio_tracks = get_audio_tracks_from_file(file_path)
            output_subtitle_tracks = get_subtitle_tracks_from_file(file_path)

            # Set file audio track info

            for i, track in enumerate(output_audio_tracks):
                track["file_id"] = file.id
                track["track"] = i + 1
                audio_track = FileAudioTrack(**track)
                file.audio_track = audio_track
                current_app.logger.info(f"{file} Adding audio track {audio_track}")
                db.session.add(audio_track)

            # Set file subtitle track info

            if len(output_subtitle_tracks) > 1:
                main_subtitle_track = output_subtitle_tracks[0].get("elements")
                for i, track in enumerate(output_subtitle_tracks[1:]):
                    track_length = track.get("elements")
                    forced_flag = track.get("forced")

                    # If a track is less than 1/3 the length of the first subtitle track,
                    # but it's not marked as forced, speculate that it might be a forced
                    # subtitle track

                    if track_length <= (main_subtitle_track * 0.3) and not forced_flag:
                        current_app.logger.warning(
                            f"{file} Subtitle track {i+2} has {track_length} elements "
                            f"and may be a forced subtitle track!"
                        )
                        output_subtitle_tracks[i + 1]["forced"] = None

            for i, track in enumerate(output_subtitle_tracks):
                track["file_id"] = file.id
                track["track"] = i + 1
                subtitle_track = FileSubtitleTrack(**track)
                file.subtitle_track = subtitle_track
                current_app.logger.info(
                    f"{file} Adding subtitle track {subtitle_track}"
                )
                db.session.add(subtitle_track)

            file.date_updated = datetime.now(timezone.utc)

        except Exception:
            current_app.logger.error(traceback.format_exc())
            db.session.rollback()
            raise

        else:
            db.session.commit()

        if current_app.config["ARCHIVE_ORIGINAL_MEDIA"]:
            try:
                (
                    file.aws_untouched_key,
                    file.aws_untouched_date_uploaded,
                ) = aws_upload(
                    file_path,
                    current_app.config["AWS_UNTOUCHED_PREFIX"],
                    force_upload=True,
                    ignore_etag=True,
                )

            except Exception:
                current_app.logger.error(traceback.format_exc())
                db.session.rollback()
                raise

            else:
                db.session.commit()

        return True


def mkvmerge_task(file_id, audio_tracks, subtitle_tracks):
    """Remux a MKV file."""

    with app.app_context():
        # Initalize the database
        # https://stackoverflow.com/a/60438156
        db.init_app(app)

        try:
            job = get_current_job()

            file = File.query.filter_by(id=file_id).first()
            file_path = os.path.join(app.config["LIBRARY_DIR"], file.file_path)

            FileAudioTrack.query.filter_by(file_id=file.id).delete()
            FileSubtitleTrack.query.filter_by(file_id=file.id).delete()

            output_directory = os.path.join(
                current_app.config["LIBRARY_DIR"], file.dirname
            )
            hidden_output_file = os.path.join(output_directory, f".{file.basename}")

            audio_start = None
            subtitle_start = None

            media_info = MediaInfo.parse(file_path)
            tracks = [
                track for track in media_info.tracks if track.track_id is not None
            ]

            current_app.logger.info("MediaInfo tracks: ", tracks)

            for i, track in enumerate(tracks):
                if track.track_type == "Audio" and audio_start == None:
                    audio_start = i
                if track.track_type == "Text" and subtitle_start == None:
                    subtitle_start = i

            current_app.logger.info(f"Audio tracks: ")
            current_app.logger.info(audio_tracks)
            current_app.logger.info("Subtitle tracks: ")
            current_app.logger.info(subtitle_tracks)

            current_app.logger.info(f"First audio track: {str(audio_start)}")
            current_app.logger.info(f"First subtitle track: {str(subtitle_start)}")

            audio_tracks = [audio_start + int(track) - 1 for track in audio_tracks]
            subtitle_tracks = [
                subtitle_start + int(track) - 1 for track in subtitle_tracks
            ]

            current_app.logger.info("Modified audio tracks: ")
            current_app.logger.info(audio_tracks)
            current_app.logger.info("Modified subtitle tracks: ")
            current_app.logger.info(subtitle_tracks)

            command = [
                current_app.config["MKVMERGE_BIN"],
                "-o",
                hidden_output_file,
                "--title",
                "",
                "--track-name",
                "-1:",
            ]

            if len(audio_tracks) >= 1:
                output_audio_tracks = ",".join(map(str, audio_tracks))
                command.extend(["-a", output_audio_tracks])
            else:
                command.append("--no-audio")

            if len(subtitle_tracks) >= 1:
                output_subtitle_tracks = ",".join(map(str, subtitle_tracks))
                command.extend(["-s", output_subtitle_tracks])
            else:
                command.append("--no-subtitles")

            command.append(file_path)

            current_app.logger.info(command)

            mkvmerge_process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                bufsize=1,
            )

            for line in mkvmerge_process.stdout:
                progress_match = re.search("Progress\: \d+\%", line)
                if progress_match:
                    progress_match = re.match("^Progress\: (?P<percent>\d+)\%", line)
                    progress = int(progress_match.group("percent"))
                    current_app.logger.info(f"'{file.basename}' Remuxing: {progress}%")
                    if job:
                        job.meta["description"] = f"'{file.basename}' — Remuxing"
                        job.meta["progress"] = progress
                        job.save_meta()

            # Move the new file into place

            os.rename(hidden_output_file, file_path)

            # Rebuild the audio and subtitle track info now that we've made modifications

            output_audio_tracks = get_audio_tracks_from_file(file_path)
            output_subtitle_tracks = get_subtitle_tracks_from_file(file_path)

            # Set file audio track info

            for i, track in enumerate(output_audio_tracks):
                track["file_id"] = file.id
                track["track"] = i + 1
                audio_track = FileAudioTrack(**track)
                file.audio_track = audio_track
                current_app.logger.info(f"{file} Adding audio track {audio_track}")
                db.session.add(audio_track)

            # Set file subtitle track info

            if len(output_subtitle_tracks) > 1:
                main_subtitle_track = output_subtitle_tracks[0].get("elements")
                for i, track in enumerate(output_subtitle_tracks[1:]):
                    track_length = track.get("elements")
                    forced_flag = track.get("forced")

                    # If a track is less than 1/3 the length of the first subtitle track,
                    # but it's not marked as forced, speculate that it might be a forced
                    # subtitle track

                    if track_length <= (main_subtitle_track * 0.3) and not forced_flag:
                        current_app.logger.warning(
                            f"{file} Subtitle track {i+2} has {track_length} elements "
                            f"and may be a forced subtitle track!"
                        )
                        output_subtitle_tracks[i + 1]["forced"] = None

            for i, track in enumerate(output_subtitle_tracks):
                track["file_id"] = file.id
                track["track"] = i + 1
                subtitle_track = FileSubtitleTrack(**track)
                file.subtitle_track = subtitle_track
                current_app.logger.info(
                    f"{file} Adding subtitle track {subtitle_track}"
                )
                db.session.add(subtitle_track)

        except Exception:
            current_app.logger.error(traceback.format_exc())
            db.session.rollback()
            raise

        else:
            db.session.commit()
            mkvpropedit_task(file.id, 1, None, None)
            return True


def sync_aws_s3_storage_task():
    """Add files to AWS, and remove files that aren't in the library."""

    with app.app_context():
        # Initalize the database
        # https://stackoverflow.com/a/60438156
        db.init_app(app)

        localizations = StartedJobRegistry(
            "fitzflix-import", connection=current_app.redis
        )
        localization_tasks_running = localizations.get_job_ids()
        transcodes = StartedJobRegistry(
            "fitzflix-transcode", connection=current_app.redis
        )
        transcodes_running = transcodes.get_job_ids()

        if (
            len(current_app.import_queue.job_ids)
            + len(localization_tasks_running)
            + len(current_app.transcode_queue.job_ids)
            + len(transcodes_running)
        ) > 0:
            current_app.request_scheduler.enqueue_in(
                timedelta(minutes=5),
                "app.videos.sync_aws_s3_storage_task",
                args=None,
                job_timeout="24h",
                description=f"Syncing files with AWS S3 storage",
                at_front=True,
            )
            current_app.logger.info(
                "Waiting 5 minutes for tasks localization/transcoding tasks to finish before attempting to sync"
            )
            return True

        try:
            job = get_current_job()

            s3_keys = [
                key
                for key in get_matching_s3_keys(
                    app.config["AWS_BUCKET"],
                    prefix=f"{app.config['AWS_UNTOUCHED_PREFIX']}/",
                )
            ]

            files = File.query.all()

            movie_rank = (
                db.session.query(
                    File.id,
                    db.func.row_number()
                    .over(
                        partition_by=(
                            Movie.id,
                            File.feature_type_id,
                            File.plex_title,
                            File.edition,
                        ),
                        order_by=(File.fullscreen.asc(), RefQuality.preference.desc()),
                    )
                    .label("rank"),
                )
                .join(Movie, (Movie.id == File.movie_id))
                .join(RefQuality, (RefQuality.id == File.quality_id))
                .subquery()
            )

            tv_rank = (
                db.session.query(
                    File.id,
                    db.func.row_number()
                    .over(
                        partition_by=(
                            TVSeries.id,
                            File.season,
                            File.episode,
                        ),
                        order_by=(
                            File.fullscreen.asc(),
                            RefQuality.preference.desc(),
                            File.last_episode.desc(),
                        ),
                    )
                    .label("rank"),
                )
                .join(TVSeries, (TVSeries.id == File.series_id))
                .join(RefQuality, (RefQuality.id == File.quality_id))
                .subquery()
            )

            files = (
                db.session.query(
                    File,
                    db.case(
                        [(movie_rank.c.rank == 1, 1), (tv_rank.c.rank == 1, 1)], else_=0
                    ).label("rank"),
                )
                .join(RefQuality, (RefQuality.id == File.quality_id))
                .outerjoin(movie_rank, (movie_rank.c.id == File.id))
                .outerjoin(tv_rank, (tv_rank.c.id == File.id))
                .order_by(RefQuality.preference.asc(), File.aws_untouched_key.asc())
                .all()
            )

            current_app.logger.info(files)

            inventory_export = []
            orphaned_files = []
            unreferenced_files = []

            for i, (file, rank) in enumerate(files):
                if job:
                    job.meta["description"] = "Queuing local files for S3 upload"
                    job.meta["progress"] = int((i / len(files)) * 100)
                    job.save_meta()

                file_path = os.path.join(
                    current_app.config["LIBRARY_DIR"], file.file_path
                )

                # If the file...

                # ...is not in S3 but exists in the filesystem...
                if (
                    file.aws_untouched_key not in s3_keys
                    or file.aws_untouched_date_uploaded == None
                ) and os.path.isfile(file_path):

                    # ...then queue for upload to S3

                    current_app.logger.info(
                        f"'{file.aws_untouched_key}' Queuing for upload to AWS"
                    )

                    current_app.file_queue.enqueue(
                        "app.videos.upload_task",
                        args=(
                            file.id,
                            current_app.config["AWS_UNTOUCHED_PREFIX"],
                            True,
                        ),
                        job_timeout=current_app.config["LOCALIZATION_TASK_TIMEOUT"],
                        description=f"'{file.basename}'",
                    )

                # ...exists in s3...
                elif file.aws_untouched_key in s3_keys:

                    # ...then add it to the inventory...

                    current_app.logger.info(
                        f"'{file.aws_untouched_key}' Exists in AWS S3; rank {rank}"
                    )

                    if rank == 1:
                        inventory_export.append(
                            [current_app.config["AWS_BUCKET"], file.aws_untouched_key]
                        )

                        # ...and queue for restore if it doesn't exist locally

                        if not os.path.isfile(file_path):
                            current_app.logger.info(
                                f"'{file.aws_untouched_key}' does not exist in the local library"
                            )
                            aws_restore(file.aws_untouched_key, tier="Bulk")

                # ...is not in S3 and does not exist in the filesystem...
                elif file.aws_untouched_key not in s3_keys and not os.path.isfile(
                    file_path
                ):

                    # ...then flag as orphaned file

                    current_app.logger.info(
                        f"'{file.aws_untouched_key}' has no associated files"
                    )
                    orphaned_files.append([file.id, file.untouched_basename])

            current_app.logger.info(f"Orphaned files: {orphaned_files}")

            # Create a CSV of the best files and upload to the S3 bucket;
            # if we should ever need to do a bulk restoration of our library, we can
            # use this file to perform a restore of all our best files via
            # S3 Bulk Operation

            if inventory_export:
                f = io.StringIO()
                inventory_writer = csv.writer(f, quoting=csv.QUOTE_ALL)
                for file_object in inventory_export:
                    inventory_writer.writerow(file_object)
                inventory_file = bytes(f.getvalue(), encoding="utf-8")
                f.close()
                client = boto3.client(
                    "s3",
                    config=Config(
                        connect_timeout=20,
                        retries={"mode": "standard", "max_attempts": 10},
                    ),
                    aws_access_key_id=current_app.config["AWS_ACCESS_KEY"],
                    aws_secret_access_key=current_app.config["AWS_SECRET_KEY"],
                )
                response = client.put_object(
                    Body=inventory_file,
                    Bucket=current_app.config["AWS_BUCKET"],
                    Key="inventory/rank_1.csv",
                )

            # Delete remote S3 files that aren't in Fitzflix

            aws_untouched_keys = [
                aws_untouched_key
                for (aws_untouched_key,) in db.session.query(
                    File.aws_untouched_key
                ).all()
            ]

            for i, remote_key in enumerate(s3_keys):
                if job:
                    job.meta["description"] = "Pruning extra files from AWS S3 storage"
                    job.meta["progress"] = int((i / len(s3_keys)) * 100)
                    job.save_meta()

                if (
                    remote_key not in aws_untouched_keys
                    and remote_key != f"{app.config['AWS_UNTOUCHED_PREFIX']}/"
                ):
                    unreferenced_files.append(remote_key)
                    aws_delete(remote_key)

            if unreferenced_files:
                admin_user = User.query.filter(User.admin == True).first()
                send_email(
                    "Fitzflix - Deleted unreferenced AWS files",
                    sender=("Fitzflix", current_app.config["SERVER_EMAIL"]),
                    recipients=[admin_user.email],
                    text_body=render_template(
                        "email/unreferenced_files.txt",
                        user=admin_user.email,
                        unreferenced_files=unreferenced_files,
                    ),
                    html_body=render_template(
                        "email/unreferenced_files.html",
                        user=admin_user.email,
                        unreferenced_files=unreferenced_files,
                    ),
                )

            if orphaned_files:
                admin_user = User.query.filter(User.admin == True).first()
                send_email(
                    "Fitzflix - Orphaned file records found!",
                    sender=("Fitzflix", current_app.config["SERVER_EMAIL"]),
                    recipients=[admin_user.email],
                    text_body=render_template(
                        "email/orphaned_files.txt",
                        user=admin_user.email,
                        orphaned_files=orphaned_files,
                    ),
                    html_body=render_template(
                        "email/orphaned_files.html",
                        user=admin_user.email,
                        orphaned_files=orphaned_files,
                    ),
                )

            # Queue local files in the library folders but aren't in Fitzflix for importing

            library = []
            for path, subdirs, local_files in os.walk(
                current_app.config["MOVIE_LIBRARY"]
            ):
                for name in local_files:
                    if name.startswith(
                        ("cover", "default", "folder", "movie", "poster")
                    ) and name.endswith(("jpg", "jpeg", "png", "tbn")):
                        continue

                    if not name.startswith(".") and "@eaDir" not in path:
                        library_file = os.path.join(path, name)
                        file_path = os.path.relpath(
                            library_file, current_app.config["LIBRARY_DIR"]
                        )
                        library.append((library_file, file_path))

            for path, subdirs, local_files in os.walk(current_app.config["TV_LIBRARY"]):
                for name in local_files:
                    if name.startswith(
                        ("cover", "default", "folder", "movie", "poster")
                    ) and name.endswith(("jpg", "jpeg", "png", "tbn")):
                        continue

                    if not name.startswith(".") and "@eaDir" not in path:
                        library_file = os.path.join(path, name)
                        file_path = os.path.relpath(
                            library_file, current_app.config["LIBRARY_DIR"]
                        )
                        library.append((library_file, file_path))

            for library_file, file_path in library:
                if not File.query.filter_by(
                    file_path=file_path
                ).first() and os.path.isfile(library_file):
                    job = current_app.import_queue.enqueue(
                        "app.videos.localization_task",
                        args=(library_file,),
                        job_timeout=current_app.config["LOCALIZATION_TASK_TIMEOUT"],
                        description=f"'{os.path.basename(library_file)}'",
                        job_id=os.path.basename(library_file),
                    )
                    current_app.logger.info(
                        f"'{library_file}' isn't in library; added to import queue"
                    )

        except Exception:
            app.logger.error(traceback.format_exc())

        else:
            return True


def rename_task(file_id, new_key):
    """Rename an object already uploaded to AWS S3 storage."""

    with app.app_context():
        # Initalize the database
        # https://stackoverflow.com/a/60438156
        db.init_app(app)

        try:
            job = get_current_job()

            file = File.query.filter_by(id=file_id).first()
            old_key = file.aws_untouched_key
            if job:
                job.meta["description"] = f"Renaming AWS key '{old_key}' to '{new_key}'"
                job.meta["progress"] = -1
                job.save_meta()

            file.aws_untouched_key, file.aws_untouched_date_uploaded = aws_rename(
                old_key, new_key
            )
            db.session.commit()

        except Exception:
            current_app.logger.error(traceback.format_exc())
            db.session.rollback()

        else:
            return True


def review_task(user_id, title, rating):
    """Import movie reviews from a Netflix export."""

    with app.app_context():
        # Initalize the database
        # https://stackoverflow.com/a/60438156
        db.init_app(app)

        try:
            movie = Movie.query.filter_by(title=title).first()

            if not movie:
                tmdb_info = {}
                if not current_app.config["TMDB_API_KEY"]:
                    return False
                tmdb_api_key = current_app.config["TMDB_API_KEY"]
                tmdb_api_url = current_app.config["TMDB_API_URL"]
                requested_info = (
                    "credits,external_ids,images,keywords,release_dates,videos"
                )
                current_app.logger.info(f"'{title}' not in database, searching in TMDB")
                r = requests.get(
                    tmdb_api_url + "/search/movie",
                    params={
                        "api_key": tmdb_api_key,
                        "query": title,
                    },
                )
                r.raise_for_status()
                current_app.logger.debug(f"{r.url}: {r.json()}")
                if len(r.json().get("results")) > 0:
                    first_result = r.json().get("results")[0]
                    tmdb_id = first_result.get("id")

                    if tmdb_id and title == first_result.get("title"):
                        current_app.logger.info(f"'{title}' Getting details from TMDB")
                        r = requests.get(
                            tmdb_api_url + "/movie/" + str(tmdb_id),
                            params={
                                "api_key": tmdb_api_key,
                                "append_to_response": requested_info,
                            },
                        )
                        r.raise_for_status()
                        current_app.logger.debug(f"{r.url}: {r.json()}")
                        tmdb_info = r.json()

                        tmdb_title = tmdb_info.get("title")
                        if tmdb_info.get("release_date"):
                            tmdb_release_date = datetime.strptime(
                                tmdb_info.get("release_date"), "%Y-%m-%d"
                            )
                            tmdb_year = tmdb_release_date.year

                        if tmdb_title and tmdb_year:
                            movie = Movie(title=tmdb_title, year=tmdb_year)
                            db.session.add(movie)

                            try:
                                # Establish a savepoint with db.session.begin_nested(), so if any of the
                                # queries to get show metadata fail, we can just roll back those changes to
                                # the savepoint and still commit the movie / tv show, file, and its tracks.

                                db.session.begin_nested()
                                movie.tmdb_movie_query()
                                db.session.commit()

                            except:
                                current_app.logger.error(traceback.format_exc())
                                db.session.rollback()
                                pass

            if movie:
                modified_rating = round(rating * 2) / 2
                whole_stars = math.floor(modified_rating)
                if modified_rating % 1 == 0:
                    half_stars = 0
                else:
                    half_stars = 1

                review = UserMovieReview(
                    user_id=user_id,
                    movie_id=movie.id,
                    rating=rating,
                    modified_rating=modified_rating,
                    whole_stars=whole_stars,
                    half_stars=half_stars,
                    review="",
                    date_watched=None,
                    date_reviewed=None,
                )
                db.session.add(review)
                db.session.commit()
                current_app.logger.info(f"Rated '{title}' {rating} out of 5 stars")

        except Exception:
            current_app.logger.error(traceback.format_exc())
            db.session.rollback()

        else:
            return True


def transcode_task(file_id):
    """Transcode a file with Handbrake."""

    with app.app_context():
        # Initalize the database
        # https://stackoverflow.com/a/60438156
        db.init_app(app)

        try:
            job = get_current_job()

            # Find the file that will be transcoded

            file = File.query.filter_by(id=file_id).first()

            # Create the file identifier so we can create a lock on processing this file

            file_identifier = file.file_identifier()
            current_app.logger.debug(
                f"'{file.plex_title}' Lock identifier: {file_identifier}"
            )
            lock = current_app.lock_manager.lock(
                file.file_identifier(),
                current_app.config["TRANSCODE_TASK_TIMEOUT"] * 1000,
            )
            current_app.logger.info(f"Created lock {lock}")

            # If we didn't get a lock, return this task to the transcoding queue after
            # 45 to 75 minutes to be processed once the lock becomes available

            if not lock:
                sleep_duration = random.randint(45, 75)
                current_app.logger.warning(
                    f"'{file.plex_title}' Lock exists, "
                    f"returning to queue after {sleep_duration} minutes"
                )
                current_app.transcode_scheduler.enqueue_in(
                    timedelta(minutes=sleep_duration),
                    "app.videos.transcode_task",
                    file_id=file_id,
                    timeout=current_app.config["TRANSCODE_TASK_TIMEOUT"],
                    description=f"'{file.plex_title}'",
                )
                return False

            # Start transcoding process

            current_app.logger.info(f"'{file.plex_title}' Starting transcoding process")
            handbrake_preset = current_app.config["HANDBRAKE_PRESET"]
            ext = current_app.config["HANDBRAKE_EXTENSION"]

            # Determine output directories and files to be created

            input_file = os.path.join(current_app.config["LIBRARY_DIR"], file.file_path)
            output_directory = os.path.join(
                current_app.config["TRANSCODES_DIR"], file.dirname
            )
            hidden_output_file = os.path.join(
                output_directory, f".{file.plex_title}.{ext}"
            )
            os.makedirs(output_directory, exist_ok=True)

            # Transcode the file with Handbrake

            transcode_process = subprocess.Popen(
                [
                    current_app.config["HANDBRAKE_BIN"],
                    "--preset",
                    f"""{handbrake_preset}""",
                    "--native-language",
                    current_app.config["NATIVE_LANGUAGE"],
                    "-i",
                    input_file,
                    "-o",
                    hidden_output_file,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                bufsize=1,
            )
            for line in transcode_process.stdout:
                progress_match = re.search(
                    "Encoding\: task \d+ of \d+, \d+\.\d+ \%", line
                )
                if progress_match:
                    progress_match = re.match(
                        "^Encoding\: task (?P<this_task>\d+) of (?P<total_tasks>\d+), (?P<percent>\d+)",
                        line,
                    )
                    progress = int(progress_match.group("percent"))
                    current_app.logger.info(
                        f"'{file.plex_title}' Transcoding: {progress}%"
                    )
                    if job:
                        job.meta["description"] = (
                            f"'{file.plex_title}' — Transcoding to .{current_app.config['HANDBRAKE_EXTENSION']} file"
                        )
                        if progress_match.group("this_task") == progress_match.group(
                            "total_tasks"
                        ):
                            job.meta["progress"] = progress

                        else:
                            job.meta["progress"] = -1

                        job.save_meta()

        except Exception:
            current_app.logger.error(traceback.format_exc())
            current_app.lock_manager.unlock(lock)
            current_app.logger.info(f"Removed lock {lock}")

        else:
            current_app.sql_queue.enqueue(
                "app.videos.finalize_transcoding",
                args=(file_id, lock),
                job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
                description=f"'{file.plex_title}'",
            )

        return True


def download_task(key, basename, sqs_receipt_handle=None):
    """Download a file from AWS S3 storage."""

    with app.app_context():
        # Initalize the database
        # https://stackoverflow.com/a/60438156
        db.init_app(app)

        job = get_current_job()

        file = File.query.filter_by(aws_untouched_key=key).first()
        if file:
            basename = file.untouched_basename

        job.meta["description"] = f"'{basename}' — Downloading from AWS"
        job.save_meta()

        try:
            current_app.logger.info(
                f"Starting download of '{basename}' from AWS S3 storage"
            )
            aws_download(key, basename, sqs_receipt_handle)

        except Exception:
            current_app.logger.error(traceback.format_exc())

        else:
            return True


def sqs_retrieve_task():
    """Poll AWS SQS for possible files ready to download."""

    with app.app_context():
        # Initalize the database
        # https://stackoverflow.com/a/60438156
        db.init_app(app)

        sqs_client = boto3.client(
            "sqs",
            aws_access_key_id=current_app.config["AWS_ACCESS_KEY"],
            aws_secret_access_key=current_app.config["AWS_SECRET_KEY"],
            region_name="us-east-1",
        )

        config = Config(
            connect_timeout=20, retries={"mode": "standard", "max_attempts": 10}
        )
        s3_client = boto3.client(
            "s3",
            config=config,
            aws_access_key_id=current_app.config["AWS_ACCESS_KEY"],
            aws_secret_access_key=current_app.config["AWS_SECRET_KEY"],
        )

        # Extend timeout and restoration period for messages in download queue

        file_operations = StartedJobRegistry(
            "fitzflix-file-operation", connection=current_app.redis
        )
        file_operations_running = file_operations.get_job_ids()
        for job_id in file_operations_running:
            job = current_app.file_queue.fetch_job(job_id)
            if job:
                if job.meta.get("sqs_receipt_handle"):
                    response = sqs_client.change_message_visibility(
                        QueueUrl=current_app.config["AWS_SQS_URL"],
                        ReceiptHandle=job.meta.get("sqs_receipt_handle"),
                        VisibilityTimeout=600,
                    )
                    job_description = job.meta.get("description", job.description)
                    current_app.logger.info(
                        f"'{job_description}' Extending SQS message timeout by 600 seconds"
                    )
                    response = s3_client.restore_object(
                        Bucket=current_app.config["AWS_BUCKET"],
                        Key=job.args[0],
                        RestoreRequest={
                            "Days": 1,
                            "GlacierJobParameters": {"Tier": "Standard"},
                        },
                    )
                    current_app.logger.info(
                        f"'{job.args[0]}' Extending restoration period by 1 day"
                    )
        for job_id in current_app.file_queue.job_ids:
            job = current_app.file_queue.fetch_job(job_id)
            if job:
                if job.meta.get("sqs_receipt_handle"):
                    response = sqs_client.change_message_visibility(
                        QueueUrl=current_app.config["AWS_SQS_URL"],
                        ReceiptHandle=job.meta.get("sqs_receipt_handle"),
                        VisibilityTimeout=600,
                    )
                    job_description = job.meta.get("description", job.description)
                    current_app.logger.info(
                        f"'{job_description}' Extending SQS message timeout by 600 seconds"
                    )
                    response = s3_client.restore_object(
                        Bucket=current_app.config["AWS_BUCKET"],
                        Key=job.args[0],
                        RestoreRequest={
                            "Days": 1,
                            "GlacierJobParameters": {"Tier": "Standard"},
                        },
                    )
                    current_app.logger.info(
                        f"'{job.args[0]}' Extending restoration period by 1 day"
                    )

        response = sqs_client.receive_message(
            QueueUrl=current_app.config["AWS_SQS_URL"],
            AttributeNames=["SentTimestamp"],
            MaxNumberOfMessages=1,
            MessageAttributeNames=["All"],
            VisibilityTimeout=600,
            WaitTimeSeconds=0,
        )

        while response.get("Messages"):
            response_body = json.loads(response["Messages"][0]["Body"])

            receipt_handle = response["Messages"][0]["ReceiptHandle"]
            key = urllib.parse.unquote_plus(
                response_body["Records"][0]["s3"]["object"]["key"]
            )

            current_app.file_queue.enqueue(
                "app.videos.download_task",
                args=(key, os.path.basename(key), receipt_handle),
                job_timeout=current_app.config["TRANSCODE_TASK_TIMEOUT"],
                description=f"'{os.path.basename(key)}' — Downloading from AWS",
                meta={"sqs_receipt_handle": receipt_handle},
            )

            response = sqs_client.receive_message(
                QueueUrl=current_app.config["AWS_SQS_URL"],
                AttributeNames=["SentTimestamp"],
                MaxNumberOfMessages=1,
                MessageAttributeNames=["All"],
                VisibilityTimeout=600,
                WaitTimeSeconds=0,
            )

        return True


def upload_task(
    file_id,
    key_prefix="",
    force_upload=False,
    ignore_etag=False,
    storage_class="STANDARD",
):
    """Upload a file to AWS S3 storage."""

    with app.app_context():
        # Initalize the database
        # https://stackoverflow.com/a/60438156
        db.init_app(app)

        try:
            # Get the record of the file to be uploaded to AWS S3 storage

            file = File.query.filter_by(id=file_id).first()
            file_path = os.path.join(app.config["LIBRARY_DIR"], file.file_path)

            # Pass to the aws_upload() function for uploading.
            # Update the File record with the remote key and date it was uploaded.

            if file.aws_untouched_key:
                file.aws_untouched_key, file.aws_untouched_date_uploaded = aws_upload(
                    file_path=file_path,
                    key_name=file.aws_untouched_key,
                    force_upload=force_upload,
                    ignore_etag=ignore_etag,
                    storage_class=storage_class,
                )

            else:
                file.aws_untouched_key, file.aws_untouched_date_uploaded = aws_upload(
                    file_path=file_path,
                    key_prefix=key_prefix,
                    force_upload=force_upload,
                    ignore_etag=ignore_etag,
                    storage_class=storage_class,
                )

            file.date_updated = file.aws_untouched_date_uploaded

            db.session.commit()

        except Exception:
            current_app.logger.error(traceback.format_exc())
            db.session.rollback()

        else:
            return True


# Supporting functions


def aws_delete(key):
    """Delete an object from AWS S3 storage."""

    # Needs app.app_context() in order for user to call directly from web application
    with app.app_context():
        current_app.logger.info(f"Preparing to delete '{key}' from AWS...")
        s3_client = boto3.client(
            "s3",
            aws_access_key_id=current_app.config["AWS_ACCESS_KEY"],
            aws_secret_access_key=current_app.config["AWS_SECRET_KEY"],
        )
        response = s3_client.delete_object(
            Bucket=current_app.config["AWS_BUCKET"], Key=key
        )
        current_app.logger.info(f"'{key}' deleted from AWS S3 storage")
        return datetime.now(timezone.utc)


def aws_download(key, basename, sqs_receipt_handle=None):
    """Download an object from AWS S3 storage."""

    MAX_RETRY_COUNT = 10
    retry = MAX_RETRY_COUNT

    # Rename "(edition-foo bar baz)" to "{edition-foo bar baz}"
    if "(edition-" in basename:
        basename = re.sub(
            r"\(edition\-(?P<edition>.+)\)", "{edition-\\g<edition>}", basename
        )

    current_app.logger.info(f"'{basename}' downloading from AWS S3 storage")

    s3_client = boto3.client(
        "s3",
        aws_access_key_id=current_app.config["AWS_ACCESS_KEY"],
        aws_secret_access_key=current_app.config["AWS_SECRET_KEY"],
    )
    sqs_client = boto3.client(
        "sqs",
        aws_access_key_id=current_app.config["AWS_ACCESS_KEY"],
        aws_secret_access_key=current_app.config["AWS_SECRET_KEY"],
        region_name="us-east-1",
    )

    while retry > 0:
        try:
            response = s3_client.download_file(
                current_app.config["AWS_BUCKET"],
                key,
                os.path.join(current_app.config["IMPORT_DIR"], f".{basename}"),
                Callback=DownloadProgressPercentage(
                    s3_client,
                    current_app.config["AWS_BUCKET"],
                    key,
                    basename,
                ),
            )

        # Don't resume if the file doesn't exist in AWS!
        # TODO: this code may need additional testing...
        except botocore.exceptions.ClientError as error:
            if error.response["ResponseMetadata"]["HTTPStatusCode"] == 404:
                current_app.logger.info(f"'{basename}' doesn't exist in AWS S3")
                if sqs_receipt_handle:
                    try:
                        response = sqs_client.delete_message(
                            QueueUrl=current_app.config["AWS_SQS_URL"],
                            ReceiptHandle=sqs_receipt_handle,
                        )
                        current_app.logger.info(response)

                    except:
                        current_app.logger.warn(
                            f"Unable to delete message '{sqs_receipt_handle}' from SQS"
                        )
                        return False

                    else:
                        current_app.logger.info(
                            f"Deleted message '{sqs_receipt_handle}' from SQS"
                        )
                return True

            else:
                current_app.logger.error(traceback.format_exc())
                retry = retry - 1

        except Exception:
            current_app.logger.error(traceback.format_exc())
            retry = retry - 1

        else:
            current_app.logger.info(f"'{basename}' downloaded from AWS S3 storage")

            os.rename(
                os.path.join(current_app.config["IMPORT_DIR"], f".{basename}"),
                os.path.join(current_app.config["IMPORT_DIR"], f"{basename}"),
            )

            if sqs_receipt_handle:
                try:
                    response = sqs_client.delete_message(
                        QueueUrl=current_app.config["AWS_SQS_URL"],
                        ReceiptHandle=sqs_receipt_handle,
                    )
                    current_app.logger.info(response)

                except:
                    current_app.logger.warn(
                        f"Unable to delete message '{sqs_receipt_handle}' from SQS"
                    )
                    return False

                else:
                    current_app.logger.info(
                        f"Deleted message '{sqs_receipt_handle}' from SQS"
                    )

            return True

    current_app.logger.error(
        f"Tried to download '{basename}' {str(MAX_RETRY_COUNT)} times but couldn't!"
    )
    return False


def aws_rename(old_key, new_key):
    """Rename an object in AWS S3 storage."""

    job = get_current_job()

    new_key = sanitize_s3_key(new_key)
    current_app.logger.info(f"Renaming AWS key '{old_key}' to '{new_key}'")

    # If the keys are the same after sanitizing, just pretend we renamed one to the other
    if old_key == new_key:
        return new_key, datetime.now(timezone.utc)

    session = boto3.Session(
        aws_access_key_id=current_app.config["AWS_ACCESS_KEY"],
        aws_secret_access_key=current_app.config["AWS_SECRET_KEY"],
    )
    s3 = session.resource("s3")

    if job:
        job.meta["description"] = f"Renaming '{old_key}' at AWS to '{new_key}'"
        job.meta["progress"] = -1
        job.save_meta()

    # There's no function to rename a file, so we have to make a copy of the object
    # with the new name and then delete the old object

    s3.meta.client.copy(
        {
            "Bucket": current_app.config["AWS_BUCKET"],
            "Key": old_key,
        },
        current_app.config["AWS_BUCKET"],
        new_key,
    )
    s3.meta.client.delete_object(Bucket=current_app.config["AWS_BUCKET"], Key=old_key)
    return new_key, datetime.now(timezone.utc)


def aws_restore(key, days=1, tier="Standard"):
    """Request a file at AWS to be restored from Glacier status for download."""

    with app.app_context():
        # Initalize the database
        # https://stackoverflow.com/a/60438156
        db.init_app(app)

        try:
            config = Config(
                connect_timeout=20, retries={"mode": "standard", "max_attempts": 10}
            )
            s3_client = boto3.client(
                "s3",
                config=config,
                aws_access_key_id=current_app.config["AWS_ACCESS_KEY"],
                aws_secret_access_key=current_app.config["AWS_SECRET_KEY"],
            )

            # Make sure the key exists in the AWS bucket

            response = s3_client.list_objects(
                Bucket=current_app.config["AWS_BUCKET"], Prefix=key, MaxKeys=1
            )

            # current_app.logger.info(response["Contents"])

            # If the key exists

            if response["Contents"][0]["Key"]:
                head_response = s3_client.head_object(
                    Bucket=current_app.config["AWS_BUCKET"], Key=key
                )

                # current_app.logger.info(head_response)

                if response["Contents"][0][
                    "StorageClass"
                ] == "STANDARD" or 'ongoing-request="false"' in head_response.get(
                    "Restore", 'ongoing-request="true"'
                ):
                    current_app.logger.info(
                        f"'{key}' doesn't need to be restored; attempting to download"
                    )
                    current_app.file_queue.enqueue(
                        "app.videos.download_task",
                        args=(key, os.path.basename(key)),
                        job_timeout=current_app.config["LOCALIZATION_TASK_TIMEOUT"],
                        description=f"'{os.path.basename(key)}'",
                        atfront=True,
                    )
                    return

                else:
                    response = s3_client.restore_object(
                        Bucket=current_app.config["AWS_BUCKET"],
                        Key=key,
                        RestoreRequest={
                            "Days": days,
                            "GlacierJobParameters": {"Tier": tier},
                        },
                    )
                    current_app.logger.info(
                        f"Requested '{key}' to be restored for {days} day(s) using tier '{tier}'"
                    )
                    # current_app.logger.info(response)

        except Exception as e:
            if e.response["Error"]["Code"] == "RestoreAlreadyInProgress":
                current_app.logger.info(
                    f"'{key}' is already in process of being restored"
                )

            else:
                current_app.logger.error(e)
                raise

        else:
            return


def aws_upload(
    file_path,
    key_prefix="",
    key_name=None,
    force_upload=False,
    ignore_etag=False,
    storage_class="STANDARD",
):
    """Search for a file in AWS S3, and upload if it doesn't exist or if it differs."""

    if not os.path.isfile(file_path):
        current_app.logger.error(
            f"'{file_path}' can't be uploaded to AWS since it's not a file!"
        )
        return None

    if key_name:
        key = sanitize_s3_key(key_name)

    else:
        key = sanitize_s3_key(os.path.basename(file_path))

    key = os.path.join(key_prefix, key)

    config = Config(
        connect_timeout=20, retries={"mode": "standard", "max_attempts": 10}
    )
    s3_client = boto3.client(
        "s3",
        config=config,
        aws_access_key_id=current_app.config["AWS_ACCESS_KEY"],
        aws_secret_access_key=current_app.config["AWS_SECRET_KEY"],
    )

    # See if the key already exists in the AWS bucket

    response = s3_client.list_objects(
        Bucket=current_app.config["AWS_BUCKET"], Prefix=key, MaxKeys=1
    )

    # If the key already exists, check to see if the local and remote ETags match.
    # If the ETags match, then the files are the same and there's no need to re-upload.
    # If the IGNORE_ETAGS flag is set, only compare the file/key names, not their data.

    if not force_upload:
        if response.get("Contents"):
            for object in response.get("Contents"):
                if object.get("Key") == key:
                    remote_etag = object.get("ETag").replace('"', "")
                    date_uploaded = object.get("LastModified")

            if ignore_etag or current_app.config["IGNORE_ETAGS"]:
                current_app.logger.info(
                    f"'{file_path}' matches '{key}' and ETags are ignored, "
                    f"no need to re-upload"
                )
                return key, date_uploaded

            local_etag = calculate_etag(file_path)
            if local_etag == remote_etag:
                current_app.logger.info(
                    f"'{file_path}' is the same as '{key}', no need to re-upload"
                )
                return key, date_uploaded

            current_app.logger.info(
                f"Local ETag '{local_etag}' ('{file_path}') "
                f"differs from remote ETag '{remote_etag}' ('{key}'), "
                f"re-uploading to AWS"
            )

        else:
            current_app.logger.info(
                f"'s3://{os.path.join(current_app.config['AWS_BUCKET'], key)}' "
                f"doesn't exist at AWS"
            )

    current_app.logger.info(
        f"Uploading '{file_path}' to "
        f"'s3://{os.path.join(current_app.config['AWS_BUCKET'], key)}'"
    )

    # Upload the file to AWS S3 storage

    # Thanks to https://codeflex.co/python-s3-multipart-file-upload-with-metadata-and-progress-indicator/
    # for the logic on how to handle failures; I couldn't figure out that
    # botocore.exceptions.ClientError and boto3.exceptions.S3UploadFailedError
    # returned different error formats until I saw this post.

    MAX_RETRY_COUNT = 10
    retry = MAX_RETRY_COUNT

    while retry > 0:
        try:
            response = s3_client.upload_file(
                file_path,
                current_app.config["AWS_BUCKET"],
                key,
                ExtraArgs={"StorageClass": storage_class},
                Callback=UploadProgressPercentage(file_path),
            )
            retry = 0

        except boto3.exceptions.S3UploadFailedError as e:
            retry = retry - 1
            if "BadDigest" in str(e):
                current_app.logger.warn(e)
                current_app.logger.warn(
                    f"'{file_path}' Retrying upload, "
                    f"this is retry {MAX_RETRY_COUNT - retry} out of {MAX_RETRY_COUNT}"
                )

            else:
                current_app.logger.error(e)
                raise

        except:
            raise

        else:
            current_app.logger.info(f"Uploaded '{file_path}' to AWS")
            return key, datetime.now(timezone.utc)

    current_app.logger.error(
        f"Tried to upload '{file_path}' {str(MAX_RETRY_COUNT)} times but couldn't!"
    )
    move_to_rejects(file_path, "upload error")


def calculate_etag(file_path):
    """Calculate the unique ETag for a local file."""

    basename = os.path.basename(file_path)
    current_app.logger.info(f"'{basename}' Calculating ETag")
    job = get_current_job()

    file_size = os.path.getsize(file_path)
    if file_size < EIGHT_MEGABYTES:
        # The file is less than 8 MB, so read the file in one go, and return its MD5 hash

        with open(file_path, "rb") as f:
            md5_hash = hashlib.md5(f.read())

        return md5_hash.hexdigest()

    else:
        md5_digests = []

        # Read a file in 8 MB chunks, and get the MD5 hash of each chunk

        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(EIGHT_MEGABYTES), b""):
                # Concatenate all of the MD5 hashes together
                md5_digests.append(hashlib.md5(chunk).digest())
                progress = int((f.tell() / file_size) * 100)
                current_app.logger.info(f"'{basename}' Calculating ETag: {progress}%")
                if job:
                    job.meta["description"] = f"'{basename}' — Calculating ETag"
                    job.meta["progress"] = progress
                    job.save_meta()

        # Get an MD5 hash of the concatenated hashes, and append the number of parts
        # e.g. "c7c2300fd47954c421d5fe0bc7910ca3-64"
        # c7c2300fd47954c421d5fe0bc7910ca3 is the hash of the concatenated MD5 hashes,
        # and there were 64 parts/individual MD5 hashes for the uploaded file

        return (
            hashlib.md5(b"".join(md5_digests)).hexdigest() + "-" + str(len(md5_digests))
        )


def evaluate_filename(file_path, tmdb_id=None):
    """Review a file name string and return info about what movie or TV show it is."""

    file_details = {}
    basename = os.path.basename(file_path)

    # Determine if basename matches movie or tv formats

    movie_match = re.search(
        "(.+) \((\d{4})\)(?: (\{edition\-(.+)\}) | )\-(?: (.+) | )\[(.+)\]\.(.+)",
        basename,
    )
    tv_match = re.search(
        "(.+) \- S(\d+)E(\d+)(?:\-E(\d+))? \-(?: (.+) | )\[(.+)\]\.(.+)", basename
    )

    # Need to try to match TV series first, otherwise a tv series with a year in the
    # name (e.g. "Doctor Who (2005) - S01E01 - [DVD].mkv") is matched as
    # movie: "Doctor Who", year: 2005, version: "S01E01"!

    if tv_match:
        tv = re.match(
            "(?P<title>.+) \- S(?P<season>\d+)E(?P<episode>\d+)"
            "(?:\-E(?P<last_episode>\d+))? \-(?: (?P<version>.+) | )"
            "\[(?P<quality_title>.+)\]\.(?P<extension>.+)",
            basename,
        )

        media_library = "TV Shows"
        title = tv.group("title")
        season = int(tv.group("season"))
        episode = int(tv.group("episode"))
        season_episode = (
            f"S{tv.group('season').zfill(2)}E{tv.group('episode').zfill(2)}"
        )
        if tv.group("last_episode"):
            last_episode = int(tv.group("last_episode"))
            season_episode = f"{season_episode}-E{tv.group('last_episode').zfill(2)}"

        else:
            last_episode = int(tv.group("episode"))

        # If the file quality name doesn't match a expected name, then we must reject

        quality_title = tv.group("quality_title")
        if not RefQuality.query.filter_by(quality_title=quality_title).first():
            return False

        extension = tv.group("extension")

        # Remove spaces and periods from end of folder name, like Sonarr
        # https://github.com/Sonarr/Sonarr/blob/phantom-develop/src/NzbDrone.Core/Organizer/FileNameBuilder.cs#L353

        folder_title = title
        while folder_title.endswith(" ") or folder_title.endswith("."):
            folder_title = folder_title.strip(" ")
            folder_title = folder_title.strip(".")

        if season == 0:
            dirname = os.path.join(media_library, folder_title, "Specials")

        else:
            dirname = os.path.join(
                media_library,
                folder_title,
                f"Season {tv.group('season').zfill(2)}",
            )

        fullscreen = False
        if tv.group("version"):
            version = tv.group("version")
            version_strings = version.split(" - ")

            # Standardize all instances of "Full Screen" in the version string

            for i, string in enumerate(version_strings):
                if string.upper().replace(" ", "") == "FULLSCREEN":
                    fullscreen = True
                    version_strings[i] = "Full Screen"

            if fullscreen == True:
                version_strings.pop(version_strings.index("Full Screen"))
                version_strings.append("Full Screen")

            version = " - ".join(version_strings)
            plex_title = f"{title} - {season_episode} - {version}"
            basename = f"{plex_title} [{quality_title}].{extension}"

        else:
            version = None
            plex_title = f"{title} - {season_episode}"
            basename = f"{plex_title} - [{quality_title}].{extension}"

        file_details["media_library"] = (
            " ".join(media_library.split()).strip() if media_library else None
        )
        file_details["file_path"] = (
            " ".join(os.path.join(dirname, basename).split()).strip()
            if os.path.join(dirname, basename)
            else None
        )
        file_details["dirname"] = " ".join(dirname.split()).strip() if dirname else None
        file_details["basename"] = (
            " ".join(basename.split()).strip() if basename else None
        )
        file_details["plex_title"] = (
            " ".join(plex_title.split()).strip() if plex_title else None
        )
        file_details["title"] = " ".join(title.split()).strip() if title else None
        file_details["season"] = season
        file_details["episode"] = episode
        file_details["last_episode"] = last_episode
        file_details["edition"] = " ".join(version.split()).strip() if version else None
        file_details["quality_title"] = (
            " ".join(quality_title.split()).strip() if quality_title else None
        )
        file_details["fullscreen"] = True if fullscreen else None
        file_details["extension"] = (
            " ".join(extension.split()).strip() if extension else None
        )

    elif movie_match:
        movie = re.match(
            "(?P<title>.+) \((?P<year>\d{4})\)(?: \{edition\-(?P<edition>.+)\} | )\-(?: (?P<version>.+) | )"
            "\[(?P<quality_title>.+)\]\.(?P<extension>.+)",
            basename,
        )

        media_library = "Movies"
        title = movie.group("title")
        year = int(movie.group("year"))

        # If the file quality name doesn't match a expected name, then we must reject

        quality_title = movie.group("quality_title")
        if not RefQuality.query.filter_by(quality_title=quality_title).first():
            return False

        # Name the film according to how it's named in TMDb, as a film can have alternate
        # titles / spellings. For example:
        # A Fistful of Dynamite == Duck, You Sucker
        # Fifth Avenue Girl == 5th Avenue Girl
        # etc.

        try:
            if tmdb_id:
                params = {
                    "api_key": current_app.config["TMDB_API_KEY"],
                    "append_to_response": "credits,external_ids,images,keywords,release_dates,videos",
                }
                url = "/movie/" + str(tmdb_id)
            else:
                params = {
                    "api_key": current_app.config["TMDB_API_KEY"],
                    "query": title,
                    "primary_release_year": year,
                }
                url = "/search/movie"
            r = requests.get(current_app.config["TMDB_API_URL"] + url, params=params)
            current_app.logger.debug(r.json())
            r.raise_for_status()

        except Exception:
            # Don't let a TMDb API issue prevent us from importing the file

            current_app.logger.warning(traceback.format_exc())
            tmdb_result = None
            pass

        else:
            tmdb_result = r.json()

        if tmdb_result:
            tmdb_results = tmdb_result.get("results")
            if tmdb_results:
                current_app.logger.info(tmdb_results)
                tmdb_film = tmdb_results[0]

                # See if we already have this tmdb_id in the database

                m = (
                    Movie.query.filter_by(tmdb_id=tmdb_film.get("id"))
                    .order_by(Movie.date_created.asc())
                    .first()
                )

                current_app.logger.info(m)

                # If so, use the existing film title and year instead of what we parsed

                if m:
                    title = m.title
                    year = m.year

                # If not, use the title and year we got from TMDb

                else:
                    title = tmdb_film.get("title", title)
                    release_date = tmdb_film.get("release_date", f"{year}-01-01")
                    release_date = datetime.strptime(release_date, "%Y-%m-%d")
                    year = release_date.year

        current_app.logger.info(f"File: {basename}")
        current_app.logger.info(f"Movie: {title} ({year})")
        edition = None
        feature_type = None
        special_feature = None
        fullscreen = False
        extension = movie.group("extension")

        if movie.group("edition"):
            edition = movie.group("edition")
            version = edition
            dirname = os.path.join(
                media_library,
                sanitize_filename(unidecode(f"{title} ({year}) {{edition-{edition}}}")),
            )

        else:
            dirname = os.path.join(
                media_library, sanitize_filename(unidecode(f"{title} ({year})"))
            )

        if movie.group("version"):
            version = movie.group("version")
            version_strings = version.split(" - ")

            # Standardize all instances of "Full Screen" in the version string

            for i, string in enumerate(version_strings):
                if string.upper().replace(" ", "") == "FULLSCREEN":
                    fullscreen = True
                    version_strings[i] = "Full Screen"

            # Get a list of the current possible special feature types

            special_feature_types = db.session.query(RefFeatureType.feature_type).all()
            special_feature_types = [result[0] for result in special_feature_types]

            if fullscreen == True:
                # Rearrange "Full Screen" in the version string.
                # I'd like "Full Screen" to go at the end of the version string
                # if there's no special feature type:
                #
                # Fullscreen - Director's Cut
                # - should be -
                # Director's Cut - Full Screen
                #
                # because it's more of a full screen version of the Director's Cut,
                # than a Director's Cut of the full screen version.
                #
                # But I also need to be sure not to put "Full Screen" after any
                # special feature types if it's not already there. Otherwise we get:
                #
                # Clang Clang Boogie (2019) - Interviews - Full Screen - I Like Salad [Bluray-1080p].mkv
                # - which turns into -
                # Clang Clang Boogie (2019)/Interviews/Full Screen - I Like Salad.mkv
                # - which should just be -
                # Clang Clang Boogie (2019)/Interviews/I Like Salad.mkv

                # Comparing uppercase versions of the special feature types to match
                # cases e.g. "Behind the Scenes" instead of "Behind The Scenes"

                if not bool(
                    set([v.upper() for v in version_strings]).intersection(
                        [t.upper() for t in special_feature_types]
                    )
                ):
                    version_strings.pop(version_strings.index("Full Screen"))
                    version_strings.append("Full Screen")

            for type in special_feature_types:
                # If it has a special feature identifier, get everything after the
                # identifier, and use that as the name of the special feature

                if type.upper() in [string.upper() for string in version_strings]:
                    type_position = [
                        string.upper() for string in version_strings
                    ].index(type.upper()) + 1
                    feature_type = type
                    special_feature = " - ".join(version_strings[type_position:])
                    dirname = os.path.join(
                        dirname, sanitize_filename(unidecode(feature_type))
                    )
                    break

            # Special features have only the special feature as their file name,
            # no movie title, year, or version (the version string is now the name)

            if special_feature:
                version = None
                plex_title = special_feature
                basename = f"{special_feature}.{extension}"

            elif fullscreen and len(version_strings) == 1:
                if edition:
                    plex_title = f"{title} ({year}) {{edition-{edition}}}"
                else:
                    version = None
                    plex_title = f"{title} ({year})"
                basename = f"{plex_title} - Full Screen [{quality_title}].{extension}"

            elif fullscreen:
                version_strings.pop(version_strings.index("Full Screen"))
                version = " - ".join(version_strings)
                if edition:
                    plex_title = f"{title} ({year}) {{edition-{edition}}} - {version}"
                else:
                    plex_title = f"{title} ({year}) - {version}"
                basename = f"{plex_title} - Full Screen [{quality_title}].{extension}"

            else:
                version = " - ".join(version_strings)
                if edition:
                    plex_title = f"{title} ({year}) {{edition-{edition}}} - {version}"
                else:
                    plex_title = f"{title} ({year}) - {version}"
                basename = f"{plex_title} [{quality_title}].{extension}"

        else:
            if edition:
                version = edition
                plex_title = f"{title} ({year}) {{edition-{edition}}}"
            else:
                version = None
                plex_title = f"{title} ({year})"
            basename = f"{plex_title} - [{quality_title}].{extension}"

        basename = sanitize_filename(unidecode(basename))

        file_details["media_library"] = (
            " ".join(media_library.split()).strip() if media_library else None
        )
        file_details["file_path"] = (
            " ".join(os.path.join(dirname, basename).split()).strip()
            if os.path.join(dirname, basename)
            else None
        )
        file_details["dirname"] = " ".join(dirname.split()).strip() if dirname else None
        file_details["basename"] = (
            " ".join(basename.split()).strip() if basename else None
        )
        file_details["plex_title"] = (
            " ".join(plex_title.split()).strip() if plex_title else None
        )
        file_details["title"] = " ".join(title.split()).strip() if title else None
        file_details["year"] = year
        file_details["feature_type_name"] = (
            " ".join(feature_type.split()).strip() if feature_type else None
        )
        file_details["edition"] = " ".join(version.split()).strip() if version else None
        file_details["quality_title"] = (
            " ".join(quality_title.split()).strip() if quality_title else None
        )
        file_details["fullscreen"] = True if fullscreen else None
        file_details["extension"] = (
            " ".join(extension.split()).strip() if extension else None
        )

    else:
        return False

    return file_details


def get_audio_tracks_from_file(file_path):
    """Parse a file with MediaInfo and return its audio tracks."""

    audio_tracks = []
    media_info = MediaInfo.parse(file_path)
    current_app.logger.debug(f"{os.path.basename(file_path)} -> {media_info.to_json()}")

    for track in media_info.tracks:
        if track.track_type == "Audio":
            audio_track = {}
            language = track.to_data().get("other_language", "und")

            if language == "und":
                audio_track["language"] = "und"
                audio_track["language_name"] = "Undetermined"

            elif "zxx" in language:
                audio_track["language"] = "zxx"
                audio_track["language_name"] = "Not applicable"

            elif len(language) >= 4:
                audio_track["language"] = language[3]
                audio_track["language_name"] = language[0]

            else:
                audio_track["language"] = "und"
                audio_track["language_name"] = "Undetermined"

            audio_track["streamorder"] = (
                int(track.to_data().get("streamorder"))
                if str(track.to_data().get("streamorder", "")).isdigit()
                else None
            )
            audio_track["format"] = track.to_data().get("format")

            audio_track["channels"] = (
                float(track.to_data().get("channel_s"))
                if str(track.to_data().get("channel_s", "")).isdigit()
                else None
            )

            # Change track channel layout to include LFE track if present
            if audio_track["channels"] and "LFE" in track.to_data().get(
                "channel_layout", ""
            ):
                audio_track["channels"] = str(audio_track["channels"] - 1 + 0.1)
            else:
                audio_track["channels"] = str(audio_track["channels"] * 1.0)

            audio_track["default"] = (
                True if track.to_data().get("default") == "Yes" else False
            )
            audio_track["codec"] = track.to_data().get("commercial_name")
            audio_track["bitrate"] = (
                int(track.to_data().get("bit_rate"))
                if str(track.to_data().get("bit_rate", "")).isdigit()
                else None
            )
            audio_track["bitrate_kbps"] = (
                round(track.to_data().get("bit_rate") / 1000)
                if str(track.to_data().get("bit_rate", "")).isdigit()
                else None
            )
            audio_track["bit_depth"] = (
                int(track.to_data().get("bit_depth"))
                if str(track.to_data().get("bit_depth", "")).isdigit()
                else None
            )
            audio_track["sampling_rate"] = (
                int(track.to_data().get("sampling_rate"))
                if str(track.to_data().get("sampling_rate", "")).isdigit()
                else None
            )
            audio_track["sampling_rate_khz"] = (
                int(track.to_data().get("sampling_rate") / 1000)
                if str(track.to_data().get("sampling_rate", "")).isdigit()
                else None
            )
            audio_track["compression_mode"] = track.to_data().get("compression_mode")
            if (
                audio_track["compression_mode"] is None
                and audio_track["codec"] == "PCM"
            ):
                audio_track["compression_mode"] = "Lossless"

            audio_tracks.append(audio_track)

    current_app.logger.info(
        f"'{os.path.basename(file_path)}' Audio tracks: {audio_tracks}"
    )
    return audio_tracks


def get_criterion_collection_from_wikipedia():
    """Scrape Wikipedia for Criterion Collection information."""

    url = current_app.config["WIKIPEDIA_CRITERION_COLLECTION_URL"]
    criterion_collection = []

    if url:
        r = requests.get(url)
        r.raise_for_status()
        soup = BeautifulSoup(r.content, features="html.parser")
        table = soup.find_all("table")[1]
        headers = table.findAll("th")

        # Column 2 = title
        # Column 4 = original release year
        # Column 0 = spine
        # Column 7 = box set title
        # Column 5 = blu-ray version

        contents = table.findAll("tr")
        for row in contents:
            columns = row.findAll("td")
            if not columns:
                continue

            title = re.search(r"^([^\[]+)", columns[2].text.strip())
            if not title:
                continue

            title = title.group(1)

            year = re.search(r"(\d{4})$", columns[4].text.strip())
            if not year:
                continue

            year = int(year.group())

            spine_number = re.search(r"^(\d+)", columns[0].text.strip())
            spine_number = spine_number.group(1)
            if columns[0].get("style") == "background:gray;":
                in_print = False

            else:
                in_print = True

            if columns[5].text.strip()[0:3] == "Yes":
                bluray = True

            else:
                bluray = False

            set = columns[7].get_text(separator=" ").strip()
            if not set:
                set = None

            else:
                while "  " in set:
                    set = set.replace("  ", " ")

            criterion_collection.append(
                {
                    "title": title.upper(),
                    "year": year,
                    "spine_number": spine_number,
                    "set": set,
                    "in_print": in_print,
                    "bluray": bluray,
                }
            )

    return criterion_collection


def get_matching_s3_objects(bucket, prefix="", suffix=""):
    """Iterate through objects in S3 storage.

    https://alexwlchan.net/2019/07/listing-s3-keys/

    Copyright (c) 2012-2019 Alex Chan

    Permission is hereby granted, free of charge, to any person obtaining a
    copy of this software and associated documentation files (the "Software"),
    to deal in the Software without restriction, including without limitation
    the rights to use, copy, modify, merge, publish, distribute, sublicense,
    and/or sell copies of the Software, and to permit persons to whom the Software
    is furnished to do so, subject to the following conditions:

    The above copyright notice and this permission notice shall be included in
    all copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
    THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR
    OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE,
    ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
    OTHER DEALINGS IN THE SOFTWARE.
    """

    s3 = boto3.client(
        "s3",
        aws_access_key_id=current_app.config["AWS_ACCESS_KEY"],
        aws_secret_access_key=current_app.config["AWS_SECRET_KEY"],
    )
    paginator = s3.get_paginator("list_objects_v2")
    kwargs = {"Bucket": bucket}
    if isinstance(prefix, str):
        prefixes = (prefix,)
    else:
        prefixes = prefix

    for key_prefix in prefixes:
        kwargs["Prefix"] = key_prefix
        for page in paginator.paginate(**kwargs):
            try:
                contents = page["Contents"]
            except KeyError:
                break
            for obj in contents:
                key = obj["Key"]
                if key.endswith(suffix):
                    yield obj


def get_matching_s3_keys(bucket, prefix="", suffix=""):
    """Return objects in S3 storage.

    https://alexwlchan.net/2019/07/listing-s3-keys/

    Copyright (c) 2012-2019 Alex Chan

    Permission is hereby granted, free of charge, to any person obtaining a
    copy of this software and associated documentation files (the "Software"),
    to deal in the Software without restriction, including without limitation
    the rights to use, copy, modify, merge, publish, distribute, sublicense,
    and/or sell copies of the Software, and to permit persons to whom the Software
    is furnished to do so, subject to the following conditions:

    The above copyright notice and this permission notice shall be included in
    all copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
    THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR
    OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE,
    ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
    OTHER DEALINGS IN THE SOFTWARE.
    """

    for obj in get_matching_s3_objects(bucket, prefix, suffix):
        yield obj["Key"]


def get_subtitle_tracks_from_file(file_path):
    """Parse a file with MediaInfo and return its subtitle tracks."""

    subtitle_tracks = []
    media_info = MediaInfo.parse(file_path)
    current_app.logger.debug(f"{os.path.basename(file_path)} -> {media_info.to_json()}")

    for track in media_info.tracks:
        if track.track_type == "Text":
            subtitle_track = {}
            language = track.to_data().get("other_language", "und")

            if language == "und" or len(language) == 0:
                subtitle_track["language"] = "und"
                subtitle_track["language_name"] = "Undetermined"

            elif "zxx" in language:
                subtitle_track["language"] = "zxx"
                subtitle_track["language_name"] = "Not applicable"

            elif len(language) <= 3:
                # The 3-character language code is usually in the 4th position in the
                # other_language variable, but sometimes the other_language variable only
                # has 3 elements. If other_language doesn't have a 4th element, default
                # to "Undetermined" / "und", check to see if any values are 3 characters
                # long, and use it if it exists.

                subtitle_track["language"] = "und"
                subtitle_track["language_name"] = "Undetermined"

                for l in language:
                    if len(l) == 3:
                        subtitle_track["language"] = l
                        subtitle_track["language_name"] = language[0]
                        break

            else:
                subtitle_track["language"] = language[3]
                subtitle_track["language_name"] = language[0]

            subtitle_track["streamorder"] = (
                int(track.to_data().get("streamorder"))
                if str(track.to_data().get("streamorder", "")).isdigit()
                else None
            )
            subtitle_track["elements"] = int(
                track.to_data().get("count_of_elements", 0)
            )
            subtitle_track["default"] = (
                True if track.to_data().get("default") == "Yes" else False
            )
            subtitle_track["forced"] = (
                True if track.to_data().get("forced") == "Yes" else False
            )
            subtitle_track["format"] = track.to_data().get("format")
            subtitle_tracks.append(subtitle_track)

    current_app.logger.info(
        f"'{os.path.basename(file_path)}' Subtitle tracks: {subtitle_tracks}"
    )
    return subtitle_tracks


def iso_639_3_native_language():
    """Determine the ISO-639-2 native language code.

    MakeMKV uses ISO-639-3 when it writes its MKV files, but the Matroska spec
    calls for using ISO-639-*2* bibliographic language codes. It's fine in most
    cases, but a few languages differ... e.g. I have an French MKV with the
    639-3 "fra" as its language code from MakeMKV, but mkvtoolnix tools don't
    recognize "fra", and expects "fre". If the file was created by MakeMKV we
    need to convert the user's native language code from 639-2 to 639-3 in
    order to check to see if it exists in the file.
    https://www.makemkv.com/forum/viewtopic.php?t=3271
    """

    iso = {
        # 639   639
        #  -2    -3
        "alb": "sqi",
        "arm": "hye",
        "baq": "eus",
        "bur": "mya",
        "chi": "zho",
        "cze": "ces",
        "dut": "nld",
        "fre": "fra",
        "geo": "kat",
        "ger": "deu",
        "gre": "ell",
        "ice": "isl",
        "mac": "mkd",
        "mao": "mri",
        "may": "msa",
        "per": "fas",
        "rum": "ron",
        "slo": "slk",
        "tib": "bod",
        "wel": "cym",
    }

    native_language = current_app.config["NATIVE_LANGUAGE"]
    if native_language in iso:
        current_app.logger.info(
            f"Native language '{native_language}' has different codes "
            f"for ISO-639-2 and ISO-639-3; switching to '{iso.get(native_language)}'"
        )
        native_language = iso.get(native_language)

    else:
        current_app.logger.info(
            f"Native language is '{native_language}', no need to translate ISO code"
        )

    return native_language


def move_to_rejects(file_path, reason=""):
    """Move a file to the rejects directory."""

    reject_directory = os.path.join(current_app.config["REJECTS_DIR"], reason)
    os.makedirs(reject_directory, exist_ok=True)
    shutil.move(file_path, os.path.join(reject_directory, os.path.basename(file_path)))
    current_app.logger.info(
        f"'{os.path.basename(file_path)}' Moved to rejects directory"
    )
    return True


def refresh_criterion_collection_info(movie_id=None):
    """Refresh Criterion Collection information from Wikipedia."""

    with app.app_context():
        # Initalize the database
        # https://stackoverflow.com/a/60438156
        db.init_app(app)

        try:
            job = get_current_job()

            # If the user specified a particular movie to be updated, update the
            # Criterion Collection info for just that one movie. Otherwise, update all.

            if movie_id:
                movies = Movie.query.filter_by(id=movie_id).first()

            else:
                movies = Movie.query.all()

            criterion_collection = get_criterion_collection_from_wikipedia()

            for movie in movies:
                for release in criterion_collection:
                    # See if the title and year for movies in our library match what we
                    # scraped from Wikipedia. If so, update it with Criterion release info.

                    if (movie.title.upper() == release.get("title")) and (
                        movie.year == release.get("year")
                    ):
                        movie.criterion_spine_number = release.get("spine_number")

                        # Decided against automatically adding the set title
                        # movie.criterion_set_title = release.get("set")

                        movie.criterion_in_print = release.get("in_print")
                        movie.criterion_bluray = release.get("bluray")
                        if movie.criterion_disc_owned == None:
                            movie.criterion_disc_owned = False

                        current_app.logger.info(
                            f"{movie} Assigning Criterion Collection "
                            f"spine #{movie.criterion_spine_number}"
                        )

            db.session.commit()

        except Exception:
            current_app.logger.error(traceback.format_exc())
            db.session.rollback()

        else:
            return True


def refresh_tmdb_info(library, id, tmdb_id=None):
    """Refresh movie or TV show information from TMDB."""

    with app.app_context():
        # Initalize the database
        # https://stackoverflow.com/a/60438156
        db.init_app(app)

        try:
            job = get_current_job()

            if library == "Movies":
                # Get the Movie record to be updated

                movie = Movie.query.filter_by(id=id).first()

                # Make a note of the original movie_id, title, year, and tmdb_id fields.

                original_movie_id = movie.id
                original_title = movie.title
                original_year = movie.year
                original_tmdb_id = movie.tmdb_id

                # See if the requested tmdb_id already exists in the Movie table.
                # If so, we'll use that existing Movie record.
                # If the user specified a tmdb_id, get the info for that tmdb_id.
                # If not, try to find a movie from TMDB based on the movie's title and year.

                current_app.logger.info(f"tmdb_id: {tmdb_id}")
                if tmdb_id != None:
                    existing_movie = Movie.query.filter_by(tmdb_id=tmdb_id).first()
                    if existing_movie:
                        movie = existing_movie
                        current_app.logger.info(f"Existing movie: {movie}")
                        existing_movie.tmdb_movie_query(tmdb_id)
                        db.session.commit()
                    else:
                        movie.tmdb_movie_query(tmdb_id)
                else:
                    movie.tmdb_movie_query()

                # Make a note of the updated movie_id, title, year, and tmdb_id fields.

                updated_movie_id = movie.id
                updated_title = movie.title
                updated_year = movie.year
                updated_tmdb_id = movie.tmdb_id

                # update files to the new movie record

                old_files = File.query.filter_by(movie_id=original_movie_id).all()

                for old_record in old_files:
                    old_record.movie_id = updated_movie_id

                try:
                    db.session.commit()

                except Exception:
                    current_app.logger.error(traceback.format_exc())
                    db.session.rollback()

                # Reconstruct untouched filenames using the new movie details

                files = File.query.filter_by(movie_id=updated_movie_id).all()

                for f in files:
                    untouched_basename = reconstruct_filename(f.id)
                    f.untouched_basename = untouched_basename
                    current_app.logger.info(
                        f"New untouched basename: '{untouched_basename}'"
                    )

                    aws_untouched_key = os.path.join(
                        current_app.config["AWS_UNTOUCHED_PREFIX"],
                        sanitize_s3_key(untouched_basename),
                    )
                    if f.aws_untouched_key != aws_untouched_key and os.path.exists(
                        os.path.join(current_app.config["LIBRARY_DIR"], f.file_path)
                    ):
                        f.aws_untouched_key = aws_untouched_key
                        current_app.logger.info(
                            f"New untouched key:      '{aws_untouched_key}'"
                        )

                try:
                    db.session.commit()

                except Exception:
                    current_app.logger.error(traceback.format_exc())
                    db.session.rollback()

                # Create new directories and move files if necessary

                files = File.query.filter_by(movie_id=updated_movie_id).all()

                for f in files:
                    if tmdb_id != None:
                        file_details = evaluate_filename(
                            f.untouched_basename, tmdb_id=tmdb_id
                        )
                    else:
                        file_details = evaluate_filename(f.untouched_basename)
                    # current_app.logger.info(file_details)

                    os.makedirs(
                        os.path.join(
                            current_app.config["LIBRARY_DIR"],
                            file_details.get("dirname"),
                        ),
                        exist_ok=True,
                    )
                    old_file = os.path.join(
                        current_app.config["LIBRARY_DIR"], f.file_path
                    )
                    old_directory = os.path.dirname(old_file)
                    new_file = os.path.join(
                        current_app.config["LIBRARY_DIR"], file_details.get("file_path")
                    )
                    if old_file != new_file and os.path.exists(old_file):
                        current_app.logger.info(
                            f"Renaming '{old_file}' to '{new_file}'"
                        )
                        try:
                            os.rename(old_file, new_file)
                        except FileNotFoundError:
                            pass

                    # delete any old local assets
                    try:
                        old_assets = os.listdir(old_directory)
                        new_directory = os.path.join(
                            current_app.config["LIBRARY_DIR"],
                            file_details.get("dirname"),
                        )
                        for old_asset in old_assets:
                            if (
                                old_asset.startswith(
                                    ("cover", "default", "movie", "poster")
                                )
                                and old_asset.endswith(("jpg", "jpeg", "png", "tbn"))
                                and f.feature_type_id is None
                                and os.path.join(old_directory, old_asset)
                                != os.path.join(new_directory, old_asset)
                                and os.path.isfile(
                                    os.path.join(old_directory, old_asset)
                                )
                            ):
                                current_app.logger.info(
                                    f"Renaming '{os.path.join(old_directory, old_asset)}' to '{os.path.join(new_directory, old_asset)}'"
                                )
                                os.rename(
                                    os.path.join(old_directory, old_asset),
                                    os.path.join(new_directory, old_asset),
                                )

                            elif old_asset == "@eaDir":
                                current_app.logger.info(
                                    f"Deleting '{os.path.join(old_directory, old_asset)}'"
                                )
                                shutil.rmtree(
                                    os.path.join(old_directory, old_asset),
                                    ignore_errors=True,
                                )

                    except FileNotFoundError:
                        pass

                    try:
                        # delete the old directory tree if it's empty
                        os.removedirs(old_directory)

                    except OSError:
                        pass

                    f.file_path = file_details.get("file_path")
                    f.dirname = file_details.get("dirname")
                    f.basename = file_details.get("basename")
                    f.plex_title = file_details.get("plex_title")

                    try:
                        db.session.commit()

                    except Exception:
                        current_app.logger.error(traceback.format_exc())
                        db.session.rollback()

                if updated_movie_id != original_movie_id:

                    # Migrate reviews to the new movie if the movie_id changed

                    reviews = UserMovieReview.query.filter_by(
                        movie_id=original_movie_id
                    ).all()
                    for review in reviews:
                        review.movie_id = movie.id

                    # Delete the old movie record from the database

                    original_movie_record = Movie.query.filter_by(
                        id=original_movie_id
                    ).first()
                    db.session.delete(original_movie_record)

                movie_poster = (
                    db.session.query(
                        Movie.tmdb_id, File.dirname, Movie.tmdb_poster_path
                    )
                    .join(Movie, (Movie.id == File.movie_id))
                    .filter(File.feature_type_id == None)
                    .filter(Movie.tmdb_id != None)
                    .filter(Movie.tmdb_poster_path != None)
                    .filter(Movie.id == updated_movie_id)
                    .first()
                )

                if movie_poster:
                    tmdb_id, dirname, tmdb_poster_path = movie_poster

                    source_poster = os.path.join(
                        os.path.abspath(os.path.dirname(__file__)),
                        "static",
                        "tmdb",
                        "movie",
                        str(tmdb_id),
                        "poster",
                        "original",
                        os.path.basename(tmdb_poster_path),
                    )

                    destination_poster = os.path.join(
                        current_app.config["LIBRARY_DIR"],
                        dirname,
                        f"poster{pathlib.Path(tmdb_poster_path).suffix}",
                    )

                    # shutil.copy(source_poster, destination_poster)

                    # current_app.logger.info(
                    #    f"Copied '{source_poster}' to '{destination_poster}'"
                    # )

            elif library == "TV Shows":
                # Get the TVSeries record to be updated

                tv_show = TVSeries.query.filter_by(id=id).first()

                # See if the requested tmdb_id already exists in the TVSeries table.
                # If so, we'll use that existing TVSeries record.

                if tv_show.tmdb_id != None:
                    existing_series = TVSeries.query.filter_by(
                        tmdb_id=tv_show.tmdb_id
                    ).first()
                    current_app.logger.info(f"Existing TV Series: {existing_series}")
                    if existing_series:
                        tv_show = existing_series

                # If the user specified a tmdb_id, get the info for that tmdb_id.
                # If not, try to find a tv show from TMDB based on the show's title.

                if tmdb_id:
                    tv_show.tmdb_tv_query(tmdb_id)

                else:
                    tv_show.tmdb_tv_query()

            db.session.commit()

        except Exception:
            current_app.logger.error(traceback.format_exc())
            db.session.rollback()

        else:
            return True


def sanitize_s3_key(key):
    """Sanitize the key name to remove problematic characters.

    See https://docs.aws.amazon.com/AmazonS3/latest/dev/UsingMetadata.html
    """

    # fmt: off
    aws_bad_chars  = [   "&",  "$",   "@",  "=", ";", ":", "+", ",", "?", "\\", "{", "^", "}", "%", "`", '"', ">", "~", "<", "#", "|"]
    aws_good_chars = [" and ",  "", " at ", "-", "-", "-", " ",  "",  "",  " ", "(",  "", ")",  "", "'",  "",  "", "-",  "",  "",  ""]
    # fmt: on

    key = os.path.normpath(key)
    key_components = key.split(os.sep)
    key = os.path.join(
        *[
            sanitize_string(component, aws_bad_chars, aws_good_chars)
            for component in key_components
        ]
    )
    return key


def sanitize_string(
    string, additional_bad_characters=[], additional_good_characters=[]
):
    """Remove or replace bad characters in a string and convert it to ASCII."""

    # Default set of bad/good character mapping is based on Sonarr's character replacement
    # https://github.com/Sonarr/Sonarr/blob/phantom-develop/src/NzbDrone.Core/Organizer/FileNameBuilder.cs#L329

    # fmt: off
    bad_characters  = ["\\", "/", "<", ">", "?", "!", "*", ":", "|", '"',   "…", "“", "”", "‘", "’"]
    good_characters = ["+",  "+",  "",  "",  "",  "", "-", "-",  "",  "", "...",  "",  "", "'", "'"]
    # fmt: on

    if len(additional_bad_characters) != len(additional_good_characters):
        raise IndexError(
            f"{additional_bad_characters} and {additional_good_characters} "
            f"are different lengths"
        )

    bad_characters = bad_characters + additional_bad_characters
    good_characters = good_characters + additional_good_characters

    # Do the unidecode first in case it adds an unwanted character

    string = unidecode(string)

    # Substitute good characters for bad characters

    for i, bad_char in enumerate(bad_characters):
        string = string.replace(bad_char, good_characters[i])

    # Make sure the string is suitable for the filesystem

    string = sanitize_filename(string)

    # Remove duplicate spaces

    while "  " in string:
        string = string.replace("  ", " ")

    string = string.strip()

    # Remove leading period if name begins with a period, so it won't be invisible
    if string[0] == ".":
        string = string[1:]

    return string


def lossless_to_flac(file_path, file_id=None):
    """Convert any lossless tracks to FLAC if the file isn't from physical media."""

    # If the file was from physical media, it should already have a FLAC
    # version of any lossless tracks included because we would have ripped it
    # using the FLAC Plus Original Audio.mmcp.xml MakeMKV profile. (We kept
    # the original format around, just in case.)

    with app.app_context():
        # Initalize the database
        # https://stackoverflow.com/a/60438156
        db.init_app(app)

        try:
            job = get_current_job()

            dirname = os.path.dirname(file_path)
            basename = os.path.basename(file_path)
            file_details = evaluate_filename(file_path)

            quality = RefQuality.query.filter(
                RefQuality.quality_title == file_details.get("quality_title")
            ).first()
            audio_tracks = get_audio_tracks_from_file(file_path)

            current_app.logger.info(f"'{basename}' Parsing with MediaInfo")
            media_info = MediaInfo.parse(file_path)
            current_app.logger.debug(f"'{basename}' -> {media_info.to_json()}")

            for track in media_info.tracks:
                if track.track_type == "General" and track.format:
                    current_app.logger.info(
                        f"'{basename}' File container {track.format}"
                    )
                    file_details["container"] = track.format

                    # Convert the file duration from milliseconds to seconds
                    file_duration = int(track.duration) / 1000
                    current_app.logger.info(file_duration)

            if len(audio_tracks) > 0 and quality.physical_media == False:
                audio_map = []
                for track_num, track in enumerate(audio_tracks):
                    if track.get("compression_mode") == "Lossless" and track.get(
                        "format"
                    ) not in ["FLAC", "PCM"]:
                        audio_map.extend(
                            [
                                "-map",
                                f"0:a:{track_num}",
                                f"-c:a:{track_num}",
                                "flac",
                            ]
                        )
                    else:
                        audio_map.extend(
                            [
                                "-map",
                                f"0:a:{track_num}",
                                f"-c:a:{track_num}",
                                "copy",
                            ]
                        )

                current_app.logger.info(f"Audio map: {audio_map}")

                if "flac" in audio_map and file_details.get("container") == "Matroska":
                    current_app.logger.info(
                        f"'{basename}' Converting lossless tracks to FLAC"
                    )
                    temp_flac_file = f"{dirname}/.{basename}"

                    flac_track_process = subprocess.Popen(
                        [
                            current_app.config["FFMPEG_BIN"],
                            "-y",
                            "-i",
                            file_path,
                            "-map",
                            "0:v:0",
                            "-c:v:0",
                            "copy",
                        ]
                        + audio_map
                        + [
                            "-map",
                            "0:s:?",
                            "-c:s",
                            "copy",
                            "-disposition:a:0",
                            "default",
                            "-disposition:a:1",
                            "none",
                            temp_flac_file,
                        ],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        universal_newlines=True,
                        bufsize=1,
                    )
                    progress = 0
                    for line in flac_track_process.stdout:
                        progress_match = re.search(
                            "time\=(?P<hour>\d{2})\:(?P<minute>\d{2}):(?P<seconds>\d{2})",
                            line,
                        )
                        if progress_match:
                            hour = int(progress_match.group("hour"))
                            minutes = int(progress_match.group("minute"))
                            seconds = int(progress_match.group("seconds"))
                            progress = int(
                                (
                                    ((hour * 3600) + (minutes * 60) + seconds)
                                    / file_duration
                                )
                                * 100
                            )
                        current_app.logger.info(
                            f"'{basename}' Converting lossless tracks to FLAC: {progress}%"
                        )
                        if job:
                            job.meta["description"] = (
                                f"'{basename}' — Converting lossless tracks to FLAC"
                            )
                            job.meta["progress"] = progress
                            job.save_meta()

                    current_app.logger.info(
                        f"'{basename}' Converted lossless tracks to FLAC"
                    )
                    current_app.logger.info(
                        f"Moving '{temp_flac_file}' to '{file_path}'"
                    )
                    shutil.move(temp_flac_file, file_path)

                    if file_id:
                        track_metadata_scan_task(file_id)

                elif file_details.get("container") != "Matroska":
                    current_app.logger.warn(
                        f"'{basename}' Unable to convert lossless tracks as is not a MKV file!"
                    )
                    return False

        except Exception:
            current_app.logger.error(traceback.format_exc())
            raise

        else:
            return True


def reconstruct_filename(file_id):
    """Reconstruct and save untouched filenames using the current details."""

    # TODO: currently only reconstructs movie filenames

    f = File.query.filter_by(id=file_id).first()
    if not f:
        return False
    if f.media_library != "Movies":
        return f.untouched_basename

    file = (
        db.session.query(File, Movie, RefQuality, RefFeatureType)
        .join(Movie, (Movie.id == File.movie_id))
        .join(RefQuality, (RefQuality.id == File.quality_id))
        .outerjoin(RefFeatureType, (RefFeatureType.id == File.feature_type_id))
        .filter(File.id == file_id)
        .first()
    )

    if not file:
        return False

    (f, m, q, ft) = file

    _, ext = os.path.splitext(f.untouched_basename)

    if m.tmdb_title == None and f.edition != None:
        beginning = f"{m.title} ({m.year}) {{edition-{f.edition}}} - "
    elif m.tmdb_title != None and f.edition != None:
        beginning = (
            f"{m.tmdb_title} ({m.tmdb_release_date.year}) {{edition-{f.edition}}} - "
        )
    elif m.tmdb_title == None:
        beginning = f"{m.title} ({m.year}) - "
    else:
        beginning = f"{m.tmdb_title} ({m.tmdb_release_date.year}) - "

    if f.fullscreen == True:
        ending = f"Full Screen [{q.quality_title}]{ext}"
    elif f.feature_type_id != None:
        ending = f"{ft.feature_type} - {f.plex_title} [{q.quality_title}]{ext}"
    else:
        ending = f"[{q.quality_title}]{ext}"

    reconstructed_filename = sanitize_filename(f"{beginning}{ending}")
    reconstructed_filename = " ".join(reconstructed_filename.split()).strip()

    return reconstructed_filename


app = create_app()
