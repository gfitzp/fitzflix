import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import threading
import time
import traceback

from datetime import datetime
from datetime import timedelta

import boto3
import requests
import rq

from botocore.client import Config
from bs4 import BeautifulSoup
from pathvalidate import sanitize_filename
from pymediainfo import MediaInfo
from rq import get_current_job
from unidecode import unidecode

from flask import current_app, flash, render_template

from app import create_app, db
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
                self._job.meta[
                    "description"
                ] = f"'{os.path.basename(self._file_path)}' — Uploading to AWS"
                self._job.meta["progress"] = progress
                self._job.save_meta()


# Tasks


def localization_task(file_path):
    """Archive an untouched file and remove unnecessary language tracks.

    - Untouched file is uploaded to AWS S3 storage for safekeeping.
    - File is localized by keeping all native-language audio and subtitle tracks, as well
      as the first audio track if the first audio track is not in the native language.
    - Pass the localized file to a separate process to add to the database.
    """

    app.app_context().push()

    try:
        job = get_current_job()

        # If the incoming file doesn't exist, there's nothing for us to do

        if not os.path.exists(file_path):
            return False

        # Parse movie or TV show info from the file name

        basename = os.path.basename(file_path)
        file_details = evaluate_filename(file_path)
        current_app.logger.debug(file_details)
        if not file_details:
            current_app.logger.error(
                f"'{basename}' doesn't match expected naming formats!"
            )
            move_to_rejects(file_path, "incorrect filename")

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
            current_app.task_scheduler.enqueue_in(
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
            return False

        # Save the untouched filename in case we need to recreate the file

        file_details["untouched_basename"] = os.path.basename(file_path)

        # Upload the untouched file to AWS S3 storage for safekeeping

        if current_app.config["ARCHIVE_ORIGINAL_MEDIA"]:
            (
                file_details["aws_untouched_key"],
                file_details["aws_untouched_date_uploaded"],
            ) = aws_upload(file_path, current_app.config["AWS_UNTOUCHED_PREFIX"])

        # Start localization process

        current_app.logger.info(f"'{basename}' Starting localization process")

        # Determine output directories and files to be created

        output_directory = os.path.join(
            current_app.config["LOCALIZED_DIR"], file_details.get("dirname")
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
                current_app.logger.info(f"'{basename}' File container {track.format}")
                file_details["container"] = track.format

        # Export a localized version of the incoming file

        if file_details.get("container") == "Matroska":
            current_app.logger.info(f"'{basename}' Localizing as a Matroska file")

            # Sometimes the input mkv file is missing track details, such as the number
            # of subtitle elements in a subtitle track, which we need for us to tell
            # whether or not there is possibly a forced subtitle track; this command
            # adds those details to the file if they are missing.

            current_app.logger.info(f"'{basename}' Adding track statistics tags")
            if job:
                job.meta["description"] = f"'{basename}' — Adding track statistics tags"
                job.save_meta()

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
                    progress_match = re.match("^Progress\: (?P<percent>\d+)\%", line)
                    progress = int(progress_match.group("percent"))
                    current_app.logger.info(
                        f"'{basename}' Adding track statistics tags: {progress}%"
                    )
                    if job:
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
                if (
                    track.track_type == "General"
                    and "MakeMKV" in track.writing_application
                ):
                    native_language = iso_639_3_native_language()
                    current_app.logger.warn(
                        f"'{basename}' was created with MakeMKV. Will use ISO-639-3 "
                        f"code '{native_language}' instead of user-supplied "
                        f"ISO-639-2 '{current_app.config['NATIVE_LANGUAGE']}' when "
                        f"processing this file with mkvmerge"
                    )

            # Determine which audio tracks to export

            # If the first audio track is in our native language, remove all other languages

            if (
                len(audio_tracks) >= 1
                and audio_tracks[0].get("language") == native_language
            ):
                current_app.logger.info(
                    f"'{basename}' First audio track matches native language "
                    f"'{native_language}'"
                )
                output_audio_langs = native_language

            # If the first audio track isn't our native language, but our language is present,
            # export the first audio track + all other native-language audio
            # (it's probably a dub, or there are native-language commentary tracks, etc.)

            elif native_language in [track["language"] for track in audio_tracks]:
                current_app.logger.info(
                    f"'{basename}' First audio track is foreign, "
                    f"but '{native_language}' audio is present"
                )
                output_audio_langs = f"1,{native_language}"

            # If no native-language track is present, export only the first audio track
            # (it's probably a subtitled movie with no commentary track)

            else:
                current_app.logger.info(
                    f"'{basename}' No '{native_language}' audio track"
                )
                output_audio_langs = "1"

            # Determine which tracks to export and create the output file

            os.makedirs(output_directory, exist_ok=True)

            # Non-native audio, native-language subtitles present

            if (
                len(audio_tracks) >= 1
                and audio_tracks[0].get("language") != native_language
                and native_language in [track["language"] for track in subtitle_tracks]
            ):
                current_app.logger.info(
                    f"'{basename}' Non-native audio, "
                    f"but '{native_language}' subtitles are present"
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

            elif native_language in [track["language"] for track in subtitle_tracks]:
                current_app.logger.info(
                    f"'{basename}' '{native_language}' audio and subtitles"
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
                    progress_match = re.match("^Progress\: (?P<percent>\d+)\%", line)
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
                    current_app.logger.info(f"'{os.path.basename(file_path)}' {line}")
                    if job:
                        job.meta[
                            "description"
                        ] = f"'{os.path.basename(file_path)}' — Removing MPEG-4 metadata"
                        job.meta["progress"] = -1
                        job.save_meta()

                current_app.logger.info(f"'{basename}' Removed MPEG-4 metadata")

            else:
                current_app.logger.info(
                    f"'{basename}' Not Matroska or MPEG-4, importing as-is"
                )

            current_app.logger.info(f"'{basename}' Copying to '{hidden_output_file}'")
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

    app.app_context().push()

    try:
        job = get_current_job()

        # Determine output directories and file to be created

        output_directory = os.path.join(
            current_app.config["LOCALIZED_DIR"], file_details.get("dirname")
        )
        hidden_output_file = os.path.join(
            output_directory, f".{file_details.get('basename')}"
        )
        output_file = os.path.join(
            current_app.config["LOCALIZED_DIR"], file_details.get("file_path")
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

            file.date_updated = datetime.utcnow()
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

        # Put the final touches on the output file and move it into place

        if file_details.get("container") == "Matroska":

            # Set the first audio track as default

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
                    current_app.logger.warn(
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
            output_subtitle_tracks = get_subtitle_tracks_from_file(hidden_output_file)

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
            current_app.logger.info(f"{file} Adding subtitle track {subtitle_track}")
            db.session.add(subtitle_track)

        # Set the localized date

        file.date_localized = datetime.utcnow()

        # Set the AWS archived fields if the file was uploaded to AWS S3 storage

        file.aws_untouched_key = file_details.get("aws_untouched_key")
        file.aws_untouched_date_uploaded = file_details.get(
            "aws_untouched_date_uploaded"
        )

        # Get or refresh movie or tv series details and download images

        try:

            # Establish a savepoint with db.session.begin_nested(), so if any of the
            # queries to get show metadata fail, we can just roll back those changes to
            # the savepoint and still commit the movie / tv show, file, and its tracks.

            db.session.begin_nested()
            if file.movie_id:
                if movie.tmdb_id == None:
                    movie.tmdb_movie_query()

            elif file.series_id:
                if tv_series.tmdb_id == None:
                    tv_series.tmdb_tv_query()

        except:
            current_app.logger.error(traceback.format_exc())
            db.session.rollback()
            pass

        else:
            db.session.commit()

        # Find and remove any worse-quality files before moving the new file into place
        # so we don't delete any special features where old and new filenames are the same

        worse_files = file.find_worse_files()
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
                    worse.aws_untouched_date_deleted = aws_delete(
                        worse.aws_untouched_key
                    )
                    worse.aws_untouched_date_uploaded = None
                db.session.delete(worse)

        # Move the new file into place

        os.rename(hidden_output_file, output_file)

        db.session.commit()

    except Exception:
        current_app.logger.error(traceback.format_exc())
        move_to_rejects(file_path, "exception")
        db.session.rollback()

    else:
        current_app.logger.info(f"'{file_path}' processed as '{output_file}'")

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

    finally:
        current_app.lock_manager.unlock(lock)
        current_app.logger.info(f"Removed lock {lock}")


def finalize_transcoding(file_id, lock):
    """Update a file with details about its transcoding and move it into position."""

    app.app_context().push()

    try:
        job = get_current_job()

        file = File.query.filter_by(id=file_id).first()
        ext = current_app.config["HANDBRAKE_EXTENSION"]

        # Determine output directories and file to be created

        output_directory = os.path.join(
            current_app.config["TRANSCODES_DIR"], file.dirname
        )
        hidden_output_file = os.path.join(output_directory, f".{file.plex_title}.{ext}")
        output_file = os.path.join(output_directory, f"{file.plex_title}.{ext}")

        # Move the transcoded file into place

        os.rename(hidden_output_file, output_file)

        # Update the file record with the date it was transcoded
        file.date_transcoded = datetime.utcnow()

        db.session.commit()

    except Exception:
        current_app.logger.error(traceback.format_exc())
        db.session.rollback()

    else:
        current_app.logger.info(f"{file.plex_title}' Transcode complete")

    finally:
        current_app.lock_manager.unlock(lock)
        current_app.logger.info(f"Removed lock {lock}")


def mkvpropedit_task(
    file_id, default_audio_track, default_subtitle_track, forced_subtitle_tracks
):
    """Update a file's MKV properties."""

    app.app_context().push()

    try:
        job = get_current_job()

        # TODO: Create lock (is one really necessary?)

        # Get the record of the file to modify

        file = File.query.filter_by(id=file_id).first()
        file_path = os.path.join(app.config["LOCALIZED_DIR"], file.file_path)

        FileAudioTrack.query.filter_by(file_id=file.id).delete()
        FileSubtitleTrack.query.filter_by(file_id=file.id).delete()

        media_info = MediaInfo.parse(file_path)
        audio_tracks = get_audio_tracks_from_file(file_path)
        subtitle_tracks = get_subtitle_tracks_from_file(file_path)

        audio_track_arguments = []
        subtitle_track_arguments = []
        for track_id, track in enumerate(audio_tracks, 1):
            if str(track_id) == default_audio_track:
                audio_track_arguments.append(
                    f"--edit track:a{track_id} --set flag-default=1"
                )

            else:
                audio_track_arguments.append(
                    f"--edit track:a{track_id} --set flag-default=0"
                )

        if default_subtitle_track or forced_subtitle_tracks:
            for track_id, track in enumerate(subtitle_tracks, 1):
                if str(track_id) == default_subtitle_track:
                    subtitle_track_arguments.append(
                        f"--edit track:s{track_id} --set flag-default=1"
                    )

                else:
                    subtitle_track_arguments.append(
                        f"--edit track:s{track_id} --set flag-default=0"
                    )

                if str(track_id) in forced_subtitle_tracks:
                    subtitle_track_arguments.append(
                        f"--edit track:s{track_id} --set flag-forced=1"
                    )

                else:
                    subtitle_track_arguments.append(
                        f"--edit track:s{track_id} --set flag-forced=0"
                    )

        # subprocess expects an array of arguments,
        # so we need to split the arguments on spaces
        localization_arguments = []
        for arg in audio_track_arguments:
            localization_arguments.extend(arg.split())

        for arg in subtitle_track_arguments:
            localization_arguments.extend(arg.split())

        current_app.logger.info(localization_arguments)

        mkvpropedit_task = subprocess.Popen(
            [current_app.config["MKVPROPEDIT_BIN"], file_path,]
            + localization_arguments,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            bufsize=1,
        )
        for line in mkvpropedit_task.stdout:
            line = line.replace("\n", "")
            current_app.logger.info(line)

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
            current_app.logger.info(f"{file} Adding subtitle track {subtitle_track}")
            db.session.add(subtitle_track)

        db.session.commit()

    except Exception:
        current_app.logger.error(traceback.format_exc())
        db.session.rollback()

    else:
        return True


def rename_task(file_id, new_key):
    """Rename an object already uploaded to AWS S3 storage."""

    app.app_context().push()

    try:
        job = get_current_job()

        # TODO: Create lock (is one really necessary?)

        file = File.query.filter_by(id=file_id).first()
        old_key = file.aws_untouched_key
        current_app.logger.info(f"Renaming AWS key '{old_key}' to '{new_key}'")
        if job:
            job.meta["description"] = f"Renaming AWS key '{old_key}' to '{new_key}'"
            job.meta["progress"] = -1

        file.aws_untouched_key, file.aws_untouched_date_uploaded = aws_rename(
            old_key, new_key
        )
        db.session.commit()

    except Exception:
        current_app.logger.error(traceback.format_exc())
        db.session.rollback()

    else:
        return True


def transcode_task(file_id):
    """Transcode a file with Handbrake."""

    app.app_context().push()

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
            file.file_identifier(), current_app.config["TRANSCODE_TASK_TIMEOUT"] * 1000
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
                job_description=f"'{file.plex_title}'",
            )
            return False

        # Start transcoding process

        current_app.logger.info(f"'{file.plex_title}' Starting transcoding process")
        handbrake_preset = current_app.config["HANDBRAKE_PRESET"]
        ext = current_app.config["HANDBRAKE_EXTENSION"]

        # Determine output directories and files to be created

        input_file = os.path.join(current_app.config["LOCALIZED_DIR"], file.file_path)
        output_directory = os.path.join(
            current_app.config["TRANSCODES_DIR"], file.dirname
        )
        hidden_output_file = os.path.join(output_directory, f".{file.plex_title}.{ext}")
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
            progress_match = re.search("Encoding\: task \d+ of \d+, \d+\.\d+ \%", line)
            if progress_match:
                progress_match = re.match(
                    "^Encoding\: task (?P<this_task>\d+) of (?P<total_tasks>\d+), (?P<percent>\d+)",
                    line,
                )
                progress = int(progress_match.group("percent"))
                current_app.logger.info(f"'{file.plex_title}' Transcoding: {progress}%")
                if job:
                    job.meta[
                        "description"
                    ] = f"'{file.plex_title}' — Transcoding to .{current_app.config['HANDBRAKE_EXTENSION']} file"
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


def upload_task(file_id, key_prefix=""):
    """Upload a file to AWS S3 storage."""

    app.app_context().push()

    try:
        # TODO: Create lock (is one really necessary?)

        # Get the record of the file to be uploaded to AWS S3 storage

        file = File.query.filter_by(id=file_id).first()
        file_path = os.path.join(app.config["LOCALIZED_DIR"], file.file_path)

        # Pass to the aws_upload() function for uploading.
        # Update the File record with the remote key and date it was uploaded.

        file.aws_untouched_key, file.aws_untouched_date_uploaded = aws_upload(
            file_path, key_prefix
        )

        db.session.commit()

    except Exception:
        current_app.logger.error(traceback.format_exc())
        db.session.rollback()

    else:
        return True


# Supporting functions


def aws_delete(key):
    """Delete an object from AWS S3 storage."""

    s3_client = boto3.client(
        "s3",
        aws_access_key_id=current_app.config["AWS_ACCESS_KEY"],
        aws_secret_access_key=current_app.config["AWS_SECRET_KEY"],
    )
    response = s3_client.delete_object(Bucket=current_app.config["AWS_BUCKET"], Key=key)
    current_app.logger.info(f"'{key}' deleted from AWS S3 storage")
    return datetime.utcnow()


def aws_rename(old_key, new_key):
    """Rename an object in AWS S3 storage."""

    job = get_current_job()

    #     s3_client = boto3.client(
    #         "s3",
    #         aws_access_key_id=current_app.config["AWS_ACCESS_KEY"],
    #         aws_secret_access_key=current_app.config["AWS_SECRET_KEY"],
    #     )

    session = boto3.Session(
        aws_access_key_id=current_app.config["AWS_ACCESS_KEY"],
        aws_secret_access_key=current_app.config["AWS_SECRET_KEY"],
    )
    s3 = session.resource("s3")

    if job:
        job.meta["description"] = f"Renaming '{old_key}' at AWS to '{new_key}'"
        job.meta["progress"] = -1

    # There's no function to rename a file, so we have to make a copy of the object
    # with the new name and then delete the old object

    s3.meta.client.copy(
        {"Bucket": current_app.config["AWS_BUCKET"], "Key": old_key,},
        current_app.config["AWS_BUCKET"],
        new_key,
    )
    s3.meta.client.delete_object(Bucket=current_app.config["AWS_BUCKET"], Key=old_key)
    return new_key, datetime.utcnow()


def aws_upload(file_path, key_prefix="", key_name=None):
    """Search for a file in AWS S3, and upload if it doesn't exist or if it differs."""

    if not os.path.isfile(file_path):
        current_app.logger.error(
            f"'{file_path}' can't be uploaded to AWS since it's not a file!"
        )
        return None

    # Sanitize the key name to remove problematic characters
    # see https://docs.aws.amazon.com/AmazonS3/latest/dev/UsingMetadata.html

    # fmt: off
    aws_bad_chars  = [   "&",  "$",   "@",  "=", ";", ":", "+", ",", "?", "\\", "{", "^", "}", "%", "`", '"', ">", "~", "<", "#", "|"]
    aws_good_chars = [" and ",  "", " at ", "-", "-", "-", " ",  "",  "",  " ", "(",  "", ")",  "", "'",  "",  "", "-",  "",  "",  ""]
    # fmt: on

    if key_name:
        key = sanitize_string(key_name, aws_bad_chars, aws_good_chars)

    else:
        key = sanitize_string(
            os.path.basename(file_path), aws_bad_chars, aws_good_chars
        )

    key = os.path.join(key_prefix, key)

    config = Config(connect_timeout=20, retries={"max_attempts": 3})
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

    if response.get("Contents"):
        local_etag = calculate_etag(file_path)
        for object in response.get("Contents"):
            if object.get("Key") == key:
                remote_etag = object.get("ETag").replace('"', "")
                date_uploaded = object.get("LastModified")

        if local_etag == remote_etag:
            current_app.logger.info(
                f"'{file_path}' is the same as '{key}', no need to re-upload"
            )
            return key, date_uploaded

        else:
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

    response = s3_client.upload_file(
        file_path,
        current_app.config["AWS_BUCKET"],
        key,
        Callback=UploadProgressPercentage(file_path),
    )

    current_app.logger.info(f"Uploaded '{file_path}' to AWS")
    return key, datetime.utcnow()


def calculate_etag(file_path):
    """Calculate the unique ETag for a local file."""

    basename = os.path.basename(file_path)
    current_app.logger.info(f"'{basename}' Calculating ETag")
    job = get_current_job()
    if job:
        job.meta["description"] = f"'{basename}' — Calculating ETag"
        job.save_meta()

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
                    job.meta["progress"] = progress
                    job.save_meta()

        # Get an MD5 hash of the concatenated hashes, and append the number of parts
        # e.g. "c7c2300fd47954c421d5fe0bc7910ca3-64"
        # c7c2300fd47954c421d5fe0bc7910ca3 is the hash of the concatenated MD5 hashes,
        # and there were 64 parts/individual MD5 hashes for the uploaded file

        return (
            hashlib.md5(b"".join(md5_digests)).hexdigest() + "-" + str(len(md5_digests))
        )


def evaluate_filename(file_path):
    """Review a file name string and return info about what movie or TV show it is."""

    file_details = {}
    basename = os.path.basename(file_path)

    # Determine if basename matches movie or tv formats

    movie_match = re.search("(.+) \((\d{4})\) \-(?: (.+) | )\[(.+)\]\.(.+)", basename)
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

        if season == 0:
            dirname = os.path.join(media_library, title, "Specials")

        else:
            dirname = os.path.join(
                media_library, title, f"Season {tv.group('season').zfill(2)}",
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

        file_details["media_library"] = media_library
        file_details["file_path"] = os.path.join(dirname, basename)
        file_details["dirname"] = dirname
        file_details["basename"] = basename
        file_details["plex_title"] = plex_title
        file_details["title"] = title
        file_details["season"] = season
        file_details["episode"] = episode
        file_details["last_episode"] = last_episode
        file_details["version"] = version
        file_details["quality_title"] = quality_title
        file_details["fullscreen"] = fullscreen
        file_details["extension"] = extension

    elif movie_match:
        movie = re.match(
            "(?P<title>.+) \((?P<year>\d{4})\) \-(?: (?P<version>.+) | )"
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
            params = {
                "api_key": current_app.config["TMDB_API_KEY"],
                "query": title,
                "year": year,
            }
            r = requests.get(
                current_app.config["TMDB_API_URL"] + "/search/movie", params=params
            )
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
                tmdb_film = tmdb_results[0]

                # See if we already have this tmdb_id in the database

                m = (
                    Movie.query.filter_by(tmdb_id=tmdb_film.get("id"))
                    .order_by(Movie.date_created.asc())
                    .first()
                )

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
        dirname = os.path.join(media_library, sanitize_string(f"{title} ({year})"))
        feature_type = None
        special_feature = None
        fullscreen = False
        extension = movie.group("extension")

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
                    dirname = os.path.join(dirname, sanitize_string(feature_type))
                    break

            # Special features have only the special feature as their file name,
            # no movie title, year, or version (the version string is now the name)

            if special_feature:
                version = None
                plex_title = special_feature
                basename = f"{special_feature}.{extension}"

            elif fullscreen and len(version_strings) == 1:
                version = None
                plex_title = f"{title} ({year})"
                basename = f"{plex_title} - Full Screen [{quality_title}].{extension}"

            elif fullscreen:
                version_strings.pop(version_strings.index("Full Screen"))
                version = " - ".join(version_strings)
                plex_title = f"{title} ({year}) - {version}"
                basename = f"{plex_title} - Full Screen [{quality_title}].{extension}"

            else:
                version = " - ".join(version_strings)
                plex_title = f"{title} ({year}) - {version}"
                basename = f"{plex_title} [{quality_title}].{extension}"

        else:
            version = None
            plex_title = f"{title} ({year})"
            basename = f"{plex_title} - [{quality_title}].{extension}"

        basename = sanitize_string(basename)

        file_details["media_library"] = media_library
        file_details["file_path"] = os.path.join(dirname, basename)
        file_details["dirname"] = dirname
        file_details["basename"] = basename
        file_details["plex_title"] = plex_title
        file_details["title"] = title
        file_details["year"] = year
        file_details["feature_type_name"] = feature_type
        file_details["version"] = version
        file_details["quality_title"] = quality_title
        file_details["fullscreen"] = fullscreen
        file_details["extension"] = extension

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

            else:
                audio_track["language"] = language[3]

            audio_track["format"] = track.to_data().get("format")
            audio_track["channels"] = int(track.to_data().get("channel_s"))
            audio_track["default"] = (
                True if track.to_data().get("default") == "Yes" else False
            )
            audio_tracks.append(audio_track)

    current_app.logger.info(
        f"'{os.path.basename(file_path)}' Audio tracks: {audio_tracks}"
    )
    return audio_tracks


def get_criterion_collection_from_wikipedia():
    """Scrape Wikipedia for Criterion Collection information."""

    url = current_app.config["WIKIPEDIA_CRITERION_COLLECTION_URL"]
    criterion_collection = []
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
                "title": title,
                "year": year,
                "spine_number": spine_number,
                "set": set,
                "in_print": in_print,
                "bluray": bluray,
            }
        )

    return criterion_collection


def get_subtitle_tracks_from_file(file_path):
    """Parse a file with MediaInfo and return its subtitle tracks."""

    subtitle_tracks = []
    media_info = MediaInfo.parse(file_path)
    current_app.logger.debug(f"{os.path.basename(file_path)} -> {media_info.to_json()}")

    for track in media_info.tracks:
        if track.track_type == "Text":
            subtitle_track = {}
            language = track.to_data().get("other_language", "und")
            if language == "und":
                subtitle_track["language"] = "und"

            else:
                subtitle_track["language"] = language[3]

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

    app.app_context().push()

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

                if (movie.title == release.get("title")) and (
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

    app.app_context().push()

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

            existing_movie = Movie.query.filter_by(tmdb_id=tmdb_id).first()
            if existing_movie:
                movie = existing_movie

            # If the user specified a tmdb_id, get the info for that tmdb_id.
            # If not, try to find a movie from TMDB based on the movie's title and year.

            if tmdb_id:
                movie.tmdb_movie_query(tmdb_id)

            else:
                movie.tmdb_movie_query()

            # Make a note of the updated movie_id, title, year, and tmdb_id fields.

            updated_movie_id = movie.id
            updated_title = movie.title
            updated_year = movie.year
            updated_tmdb_id = movie.tmdb_id

            # See if any of the movie_id, title, year, or tmdb_id fields changed.
            # If any changed, then we need to migrate files from the old Movie to the
            # new Movie record.

            if (
                (updated_movie_id != original_movie_id)
                or (updated_title != original_title)
                or (updated_year != original_year)
                or (updated_tmdb_id != original_tmdb_id)
            ):
                current_app.logger.info(
                    f"Movie information changed! Migrating to {movie}"
                )

                # Get a list of files that were associated with the old Movie record

                old_files = File.query.filter_by(movie_id=original_movie_id).all()

                for old_record in old_files:

                    # Reconstruct the original filename but with the new movie title/year

                    basename, extension = os.path.splitext(old_record.basename)
                    updated_title = sanitize_filename(updated_title)

                    # Reconstruct the original filename used when importing,
                    # but using the new movie title and year

                    reconstructed_filename = f"{updated_title} ({updated_year}) -"

                    if old_record.feature_type:
                        reconstructed_filename = f"{reconstructed_filename} {old_record.feature_type.feature_type} - {old_record.plex_title}"

                    elif old_record.version:
                        reconstructed_filename = (
                            f"{reconstructed_filename} {old_record.version}"
                        )

                    if old_record.fullscreen:
                        reconstructed_filename = (
                            f"{reconstructed_filename} - Full Screen"
                        )

                    reconstructed_filename = f"{reconstructed_filename} [{old_record.quality.quality_title}]{extension}"
                    current_app.logger.info(
                        f"Reconstructed filename: {reconstructed_filename}"
                    )

                    # Get the file details for the reconstructed filename

                    file_details = evaluate_filename(reconstructed_filename)
                    current_app.logger.info(file_details)

                    # Update the file with the updated_movie_id and plex_title to reflect
                    # the new movie, so we can get the file's ranking.
                    # (We need movie_id, feature_type_id, plex_title, and version to rank)

                    old_record.movie_id = updated_movie_id
                    old_record.plex_title = file_details.get("plex_title")

                    # With the file's movie_id and plex_title updated, we now have
                    # everything we need to get the file's new ranking

                    ranking = (
                        db.session.query(
                            File.id,
                            File.movie_id,
                            File.feature_type_id,
                            File.plex_title,
                            File.version,
                            File.fullscreen,
                            RefQuality.preference,
                            RefQuality.quality_title,
                            RefQuality.physical_media,
                            db.func.row_number()
                            .over(
                                partition_by=(
                                    File.movie_id,
                                    File.feature_type_id,
                                    File.plex_title,
                                    File.version,
                                ),
                                order_by=(
                                    RefQuality.preference.desc(),
                                    File.fullscreen,
                                    File.date_added.asc(),
                                ),
                            )
                            .label("rank"),
                        )
                        .join(RefQuality, (RefQuality.id == File.quality_id))
                        .subquery()
                    )

                    # Find the best files across old and new movie records

                    best_files = (
                        File.query.join(ranking, (ranking.c.id == File.id))
                        .filter(File.movie_id == movie.id)
                        .filter(ranking.c.rank == 1)
                        .all()
                    )

                    for each_best_file in best_files:

                        # Look at each best file, and identify all of their worse ones

                        worse_files = each_best_file.find_worse_files()

                        # If there are any worse files, delete the worse local file, and
                        # delete the archived version from AWS S3 storage if applicable

                        for worse in worse_files:
                            worse.delete_local_file()

                            # We only delete from AWS S3 storage if both archived and new
                            # files are from digital media, or if the new file is from
                            # digital media

                            if (
                                worse.quality.physical_media
                                == each_best_file.quality.physical_media
                                or each_best_file.quality.physical_media == True
                            ):
                                if worse.aws_untouched_date_uploaded:
                                    worse.aws_untouched_date_deleted = aws_delete(
                                        worse.aws_untouched_key
                                    )
                                    worse.aws_untouched_date_uploaded = None

                                db.session.delete(worse)

                    # Start building the rest of the filename fields
                    old_record.dirname = file_details.get("dirname")
                    old_record.basename = file_details.get("basename")

                    # Create the destination directory if necessary and move the file
                    # to the new location

                    os.makedirs(
                        os.path.join(
                            current_app.config["LOCALIZED_DIR"],
                            file_details.get("dirname"),
                        ),
                        exist_ok=True,
                    )
                    current_app.logger.info(
                        f"Renaming {os.path.join(current_app.config['LOCALIZED_DIR'], old_record.file_path)}' to '{os.path.join(current_app.config['LOCALIZED_DIR'], file_details.get('file_path'))}'"
                    )
                    os.rename(
                        os.path.join(
                            current_app.config["LOCALIZED_DIR"], old_record.file_path
                        ),
                        os.path.join(
                            current_app.config["LOCALIZED_DIR"],
                            file_details.get("file_path"),
                        ),
                    )

                    # Delete the old directory tree if it's empty
                    # TODO: figure out a way to not accidentally delete any hidden files

                    try:
                        os.removedirs(
                            os.path.dirname(
                                os.path.join(
                                    current_app.config["LOCALIZED_DIR"],
                                    old_record.file_path,
                                )
                            )
                        )

                    except OSError:
                        pass

                    # Now that we don't need the old file's file_path anymore,
                    # set the file_path column to the new location

                    old_record.file_path = file_details.get("file_path")

                    if (
                        old_record.aws_untouched_key
                        and old_record.aws_untouched_date_uploaded
                    ):

                        config = Config(connect_timeout=20, retries={"max_attempts": 3})
                        s3_client = boto3.client(
                            "s3",
                            config=config,
                            aws_access_key_id=current_app.config["AWS_ACCESS_KEY"],
                            aws_secret_access_key=current_app.config["AWS_SECRET_KEY"],
                        )
                        response = s3_client.list_objects(
                            Bucket=current_app.config["AWS_BUCKET"],
                            Prefix=old_record.aws_untouched_key,
                            MaxKeys=1,
                        )

                        # Get the storage class of the existing uploaded file

                        storage_class = None
                        if response.get("Contents"):
                            for object in response.get("Contents"):
                                if object.get("Key") == old_record.aws_untouched_key:
                                    storage_class = object.get("StorageClass")

                        if storage_class == "STANDARD":

                            current_app.task_queue.enqueue(
                                "app.videos.rename_task",
                                args=(
                                    old_record.id,
                                    os.path.join(
                                        current_app.config["AWS_UNTOUCHED_PREFIX"],
                                        reconstructed_filename,
                                    ),
                                ),
                                job_timeout=current_app.config[
                                    "LOCALIZATION_TASK_TIMEOUT"
                                ],
                                description=f"'{file_details.get('basename')}'",
                            )

                        else:

                            # Request the old object to be restored at AWS

                            s3_client = boto3.client(
                                "s3",
                                aws_access_key_id=current_app.config["AWS_ACCESS_KEY"],
                                aws_secret_access_key=current_app.config[
                                    "AWS_SECRET_KEY"
                                ],
                            )
                            response = s3_client.restore_object(
                                Bucket=current_app.config["AWS_BUCKET"],
                                Key=old_record.aws_untouched_key,
                                RestoreRequest={
                                    "Days": 1,
                                    "GlacierJobParameters": {"Tier": "Standard",},
                                },
                            )

                            # Standard restoration takes up to 12 hours,
                            # so schedule the restoration to take place in 13 hours

                            current_app.task_scheduler.enqueue_in(
                                timedelta(hours=13),
                                "app.videos.rename_task",
                                file_id=old_record.id,
                                new_key=os.path.join(
                                    current_app.config["AWS_UNTOUCHED_PREFIX"],
                                    reconstructed_filename,
                                ),
                                timeout=current_app.config["LOCALIZATION_TASK_TIMEOUT"],
                                job_description=f"'{file_details.get('basename')}'",
                            )

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

        elif library == "TV Shows":

            # Get the TVSeries record to be updated

            tv_show = TVSeries.query.filter_by(id=id).first()

            # See if the requested tmdb_id already exists in the TVSeries table.
            # If so, we'll use that existing TVSeries record.

            existing_series = TVSeries.query.filter_by(tmdb_id=tmdb_id).first()

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
    return string


app = create_app()
