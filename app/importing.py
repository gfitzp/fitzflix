"""The import pipeline (the strangler split from app.videos): the
chain a file walks from the import directory to the library.

evaluate_filename parses and prices the name; localization_task
archives the untouched original and strips non-native tracks;
inspect/move/finalize carry the localized output to its library home
and catalog it; transcode_task and finalize_transcoding produce the
Handbrake copies; manual_import_task is the hourly sweep; and the
rejects door, language plumbing, and filename sanitizers live here
with them.

app.videos re-exports every name here, so stored rq job strings
("app.videos.localization_task") and import sites keep resolving. The
shared lock/retry/copy plumbing stays in app.videos and is imported
lazily inside functions, keeping the module import direction one-way;
the track and S3 layers are imported directly — they never import
back.
"""

import json
import os
import re
import shutil
import subprocess
import time
import traceback

from datetime import datetime, timedelta, timezone

from pathvalidate import sanitize_filename
from pymediainfo import MediaInfo
from rq import get_current_job
from rq.registry import StartedJobRegistry
from unidecode import unidecode

from flask import current_app, render_template
from werkzeug.local import LocalProxy

from app import db, get_app, importable_basename, retry_job_id, safe_job_id
from app.aws_storage import aws_upload, untouched_key_still_claimed
from app.criterion_catalog import (
    assign_criterion_release,
    criterion_release_lookups,
    get_criterion_collection_from_wikidata,
)
from app.email import send_email as send_email_async
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
    tmdb_get,
)
from app.pipeline import migrate_trail, record_task_stage
from app.tracks import (
    _extract_media_details,
    flag_possibly_forced_subtitles,
    get_audio_tracks_from_file,
    get_subtitle_tracks_from_file,
    record_filesize,
    remove_empty_subtitle_tracks,
    supplement_lossless_tracks,
    watch_mkvmerge_progress,
)


def convert_to_matroska(file_path, output_file, job, name):
    """Remux a non-Matroska file into a Matroska container.

    Returns True on success. On failure — a format mkvmerge can't carry —
    any partial output is removed and False is returned, so the caller can
    fall back to importing the file as-is.
    """

    from app.videos import wait_for_subprocess

    current_app.logger.info(f"'{name}' Converting to a Matroska container")
    process = subprocess.Popen(
        [current_app.config["MKVMERGE_BIN"], "-o", output_file, file_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
        bufsize=1,
    )
    watch_mkvmerge_progress(process, job, name, "Converting to Matroska")
    try:
        wait_for_subprocess(process, ok_returncodes=(0, 1))
    except subprocess.CalledProcessError:
        current_app.logger.warning(
            f"'{name}' mkvmerge could not convert this file to Matroska"
        )
        try:
            os.remove(output_file)
        except OSError:
            pass
        return False
    return True


# The import completeness gate: how recently a file may have been modified
# and still be trusted (when its container can't be probed), and how many
# one-minute checks a file gets before being imported anyway

COMPLETENESS_QUIET_SECONDS = 120


MAX_COMPLETENESS_RETRIES = 30


# Containers that declare their own length, letting MediaInfo prove that a
# stalled partial copy is truncated rather than complete

SELF_SIZING_FORMATS = {"Matroska", "MPEG-4"}


def probe_file_completeness(file_path):
    """Ask the container whether the file is structurally complete.

    Matroska files declare their segment size and MP4s index themselves in
    a trailing moov atom, so MediaInfo reports truncation for a partial
    copy of either — no matter how long the copy has been stalled. Returns
    True when such a container looks complete, False when it reports
    truncation, and None for anything that can't be probed (unidentifiable
    files, or formats with no declared length).
    """

    try:
        media_info = MediaInfo.parse(file_path)
    except Exception:
        return None

    general = next(
        (track for track in media_info.tracks if track.track_type == "General"),
        None,
    )
    if general is None or general.format not in SELF_SIZING_FORMATS:
        return None
    if str(general.to_data().get("istruncated", "")).lower() == "yes":
        return False
    return True


# Tasks


def localization_task(
    file_path,
    force_upload=False,
    ignore_etag=False,
    transient_retries=0,
    completeness_retries=0,
):
    """Archive an untouched file and remove unnecessary language tracks.

    - Untouched file is uploaded to AWS S3 storage for safekeeping.
    - File is localized by keeping all native-language audio and subtitle tracks, as well
      as the first audio track if the first audio track is not in the native language.
    - Pass the localized file to a separate process to add to the database.
    """

    # Shared lock/retry/copy plumbing stays in app.videos; lazy so the
    # module import direction stays one-way

    from app.videos import (
        MAX_TRANSIENT_RETRIES,
        TRANSIENT_COPY_ERRNOS,
        _dead_volumes,
        acquire_lock_or_defer,
        copy_with_progress,
        wait_for_subprocess,
    )

    with app.app_context():
        # Define up front so the exception handler can tell whether the lock
        # was acquired before the failure, and whether staging happened

        lock = None
        source_path = file_path
        staged = False
        staging_paths = []

        try:
            job = get_current_job()
            basename = os.path.basename(file_path)

            # Don't start while any needed volume is dead: a mount failing
            # mid-task strands partial files, so defer to a retry instead

            dead = _dead_volumes(
                [
                    os.path.dirname(file_path),
                    current_app.config["MOVIE_LIBRARY"],
                    current_app.config["TV_LIBRARY"],
                    current_app.config["MEDIA_LOCATION"],
                    current_app.config["STAGING_DIR"],
                ]
            )
            if dead:
                current_app.logger.warning(
                    f"'{basename}' Volumes unavailable ({', '.join(dead)}), "
                    f"returning to queue to try again in 5 minutes"
                )
                current_app.import_queue.enqueue_in(
                    timedelta(minutes=5),
                    "app.videos.localization_task",
                    file_path=file_path,
                    force_upload=force_upload,
                    ignore_etag=ignore_etag,
                    transient_retries=transient_retries,
                    completeness_retries=completeness_retries,
                    job_timeout=current_app.config["LOCALIZATION_TASK_TIMEOUT"],
                    job_id=retry_job_id(
                        "localization_task",
                        f"'{basename}'",
                        transient_retries,
                        completeness_retries,
                    ),
                    result_ttl=86400,
                    description=f"'{basename}'",
                )
                return True

            # If the incoming file doesn't exist, there's nothing for us to do

            if not os.path.exists(file_path):
                return False

            # If the file name contains "temp-1234.", then ignore it
            if re.search(r"\-temp\-\d+\.", basename):
                return False

            # Don't process a file that's still being copied into place:
            # if it's growing, check back in a minute

            size_before = os.path.getsize(file_path)
            time.sleep(5)
            if os.path.getsize(file_path) != size_before:
                current_app.logger.info(
                    f"'{basename}' is still being copied, "
                    f"returning to queue to try again in 1 minute"
                )
                current_app.import_queue.enqueue_in(
                    timedelta(minutes=1),
                    "app.videos.localization_task",
                    file_path=file_path,
                    force_upload=force_upload,
                    ignore_etag=ignore_etag,
                    transient_retries=transient_retries,
                    completeness_retries=completeness_retries,
                    job_timeout=current_app.config["LOCALIZATION_TASK_TIMEOUT"],
                    job_id=retry_job_id(
                        "localization_task",
                        f"'{basename}'",
                        transient_retries,
                        completeness_retries,
                    ),
                    result_ttl=86400,
                    description=f"'{basename}'",
                )
                return True

            # The size check above can be fooled by a stalled network copy,
            # which holds a constant size while still incomplete. Let the
            # container prove completeness where it can — a partial Matroska
            # or MP4 reports truncation no matter how long the copy stalls —
            # and give formats that can't be probed a modification-time
            # quiet period instead. A file that never proves itself within
            # the budget is imported anyway, since corrupt-but-complete
            # files are deliberately imported as-is.

            verdict = probe_file_completeness(file_path)
            if verdict is False or (
                verdict is None
                and time.time() - os.path.getmtime(file_path)
                < COMPLETENESS_QUIET_SECONDS
            ):
                if completeness_retries >= MAX_COMPLETENESS_RETRIES:
                    current_app.logger.warning(
                        f"'{basename}' Could not be confirmed complete after "
                        f"{MAX_COMPLETENESS_RETRIES} checks, importing anyway"
                    )
                else:
                    reason = (
                        "is still incomplete"
                        if verdict is False
                        else "was modified too recently"
                    )
                    current_app.logger.info(
                        f"'{basename}' {reason}, returning to queue to try "
                        f"again in 1 minute (check {completeness_retries + 1} "
                        f"of {MAX_COMPLETENESS_RETRIES})"
                    )
                    current_app.import_queue.enqueue_in(
                        timedelta(minutes=1),
                        "app.videos.localization_task",
                        file_path=file_path,
                        force_upload=force_upload,
                        ignore_etag=ignore_etag,
                        transient_retries=transient_retries,
                        completeness_retries=completeness_retries + 1,
                        job_timeout=current_app.config["LOCALIZATION_TASK_TIMEOUT"],
                        job_id=retry_job_id(
                            "localization_task",
                            f"'{basename}'",
                            transient_retries,
                            completeness_retries + 1,
                        ),
                        result_ttl=86400,
                        description=f"'{basename}'",
                    )
                    return True

            file_details = evaluate_filename(file_path)
            current_app.logger.info(f"'{basename}' File details: {file_details}")
            if not file_details:
                reason = getattr(file_details, "reason", None)
                current_app.logger.error(
                    f"'{basename}' rejected: {reason}!"
                    if reason
                    else f"'{basename}' doesn't match expected naming formats!"
                )
                move_to_rejects(file_path, reason or "incorrect filename")
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
                    "edition": file_details.get("edition"),
                }

            elif file_details.get("media_library") == "TV Shows":
                file_identifier = {
                    "title": file_details.get("title"),
                    "season": file_details.get("season"),
                    "episode": file_details.get("episode"),
                }

            file_identifier = json.dumps(file_identifier)
            current_app.logger.debug(f"'{basename}' Lock identifier: {file_identifier}")

            # If we don't get the lock, this task returns to the localization
            # queue to be retried once the lock becomes available

            lock = acquire_lock_or_defer(
                file_identifier,
                current_app.config["LOCALIZATION_TASK_TIMEOUT"] * 1000,
                current_app.import_queue,
                "app.videos.localization_task",
                minutes=(45, 75),
                timeout=current_app.config["LOCALIZATION_TASK_TIMEOUT"],
                description=f"'{basename}'",
                kwargs={"file_path": file_path},
            )
            if not lock:
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

            # Copy the source to local staging so the archive upload and the
            # localization tools do their heavy I/O against local disk; a
            # network failure then costs a retry, not a stranded partial file

            staging_dir = current_app.config["STAGING_DIR"]
            try:
                staging_free = shutil.disk_usage(staging_dir).free
            except OSError:
                staging_free = 0

            if staging_free > os.path.getsize(file_path) * 2.5:
                staged_path = os.path.join(staging_dir, basename)
                staging_paths.append(staged_path)
                record_task_stage("Copying to staging", "started")
                try:
                    copy_with_progress(
                        file_path,
                        staged_path,
                        job,
                        basename,
                        "Copying to local staging",
                    )
                except OSError as e:
                    record_task_stage("Copying to staging", "failed")
                    if (
                        e.errno not in TRANSIENT_COPY_ERRNOS
                        or transient_retries >= MAX_TRANSIENT_RETRIES
                    ):
                        raise

                    # A flaky mount revoked the copy's file handles; the
                    # source itself is fine, so clean up and retry once the
                    # mount settles rather than rejecting a healthy file

                    current_app.logger.warning(
                        f"'{basename}' Staging copy failed with a transient "
                        f"I/O error ({e}), returning to queue to try again in "
                        f"5 minutes (attempt {transient_retries + 1} of "
                        f"{MAX_TRANSIENT_RETRIES})"
                    )
                    try:
                        os.remove(staged_path)
                    except OSError:
                        pass
                    if lock:
                        current_app.lock_manager.unlock(lock)
                        current_app.logger.info(f"Removed lock {lock}")
                    current_app.import_queue.enqueue_in(
                        timedelta(minutes=5),
                        "app.videos.localization_task",
                        file_path=source_path,
                        force_upload=force_upload,
                        ignore_etag=ignore_etag,
                        transient_retries=transient_retries + 1,
                        completeness_retries=completeness_retries,
                        job_timeout=current_app.config["LOCALIZATION_TASK_TIMEOUT"],
                        job_id=retry_job_id(
                            "localization_task",
                            f"'{basename}'",
                            transient_retries + 1,
                            completeness_retries,
                        ),
                        result_ttl=86400,
                        description=f"'{basename}'",
                    )
                    return True

                record_task_stage("Copying to staging", "done")
                file_path = staged_path
                staged = True

            else:
                current_app.logger.warning(
                    f"'{basename}' Staging space is insufficient, "
                    f"processing on the source volume instead"
                )

            # Upload the untouched file to AWS S3 storage for safekeeping

            if current_app.config["ARCHIVE_ORIGINAL_MEDIA"]:
                (
                    file_details["aws_untouched_key"],
                    file_details["aws_untouched_date_uploaded"],
                    file_details["aws_untouched_filesize_bytes"],
                ) = aws_upload(
                    file_path,
                    current_app.config["AWS_UNTOUCHED_PREFIX"],
                    force_upload=force_upload,
                    ignore_etag=ignore_etag,
                )

            # Start localization process

            current_app.logger.info(f"'{basename}' Starting localization process")

            # Determine the output directory

            output_directory = os.path.join(
                current_app.config["LIBRARY_DIR"], file_details.get("dirname")
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

            # A non-Matroska file is remuxed into a Matroska container first,
            # so every importable format gets the same localization treatment;
            # a format mkvmerge can't carry falls through to be imported as-is

            if file_details.get("container") != "Matroska":
                scratch_dir = staging_dir if staged else output_directory
                os.makedirs(scratch_dir, exist_ok=True)
                converted_file = os.path.join(scratch_dir, f".{basename}.convert.mkv")
                staging_paths.append(converted_file)

                if convert_to_matroska(file_path, converted_file, job, basename):
                    # Adopt the converted file, and rename the eventual
                    # output to match its new container

                    if file_path != source_path:
                        try:
                            os.remove(file_path)
                        except OSError:
                            pass
                    file_path = converted_file
                    file_details["container"] = "Matroska"
                    stem = file_details["basename"].rsplit(".", 1)[0]
                    file_details["basename"] = f"{stem}.mkv"
                    file_details["extension"] = "mkv"
                    file_details["file_path"] = os.path.join(
                        file_details["dirname"], file_details["basename"]
                    )

                else:
                    current_app.logger.warning(
                        f"'{basename}' Can't be converted to Matroska, "
                        f"importing as-is"
                    )

            # The localized output is written next to the staged copy when
            # staging is on, so only the finished file crosses to the library

            hidden_output_file = os.path.join(
                staging_dir if staged else output_directory,
                f".{file_details.get('basename')}",
            )
            if staged:
                staging_paths.append(hidden_output_file)

            # Export a localized version of the incoming file

            if file_details.get("container") == "Matroska":
                current_app.logger.info(f"'{basename}' Localizing as a Matroska file")

                # Give any lossless track that isn't already FLAC or PCM a
                # FLAC twin placed just before it — natively playable on
                # Apple TV clients — while always keeping the original for
                # direct play and future passthrough. Files whose twins
                # already exist (MakeMKV "FLAC Plus Original Audio" rips,
                # re-downloads of supplemented uploads) pass through as-is.

                supplement_lossless_tracks(file_path)

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
                watch_mkvmerge_progress(
                    statistics_tags_process,
                    job,
                    basename,
                    "Adding track statistics tags",
                )

                wait_for_subprocess(statistics_tags_process, ok_returncodes=(0, 1))

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

                watch_mkvmerge_progress(mkvmerge_process, job, basename, "Localizing")

                wait_for_subprocess(mkvmerge_process, ok_returncodes=(0, 1))

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

                    wait_for_subprocess(atomicparsley_process)

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

            # Remove any staged copies; the original source is what gets
            # rejected, and it hasn't been touched since staging

            for stray in staging_paths:
                try:
                    os.remove(stray)
                except OSError:
                    pass

            # Don't let a failed move to the rejects directory prevent us from
            # releasing the lock; otherwise re-imports of this same title stay
            # blocked until the lock's timeout expires

            try:
                move_to_rejects(source_path, "exception")
            except Exception:
                current_app.logger.error(traceback.format_exc())

            if lock:
                current_app.lock_manager.unlock(lock)
                current_app.logger.info(f"Removed lock {lock}")

        else:
            # The working copy (staged source or conversion temp) served its
            # purpose; the localized output is carried to the library by the
            # file-operation queue, which then hands the quick database work
            # to the sql queue

            if file_path != source_path:
                try:
                    os.remove(file_path)
                except OSError:
                    pass

            # The parse may have renamed the file (title canonicalized
            # against an existing series, container swapped to .mkv);
            # the trail is keyed by basename, so merge it under the new
            # name before the move job stamps "queued" against it

            if file_details.get("basename") != basename:
                migrate_trail(current_app.redis, basename, file_details.get("basename"))

            current_app.file_queue.enqueue(
                "app.videos.move_localized_file",
                args=(source_path, file_details, lock, hidden_output_file),
                job_timeout=current_app.config["MOVE_TASK_TIMEOUT"],
                description=f"'{basename}'",
            )

        return True


def inspect_localized_file(file_path, container, job=None):
    """Apply final track flags to a localized file and report its details.

    Runs where the file lives — on local staging, before the library copy —
    so the flag edits and parsing never happen on the sql queue: the first
    audio track becomes the only default, the first subtitle track becomes
    default when the audio is foreign, and empty subtitle tracks are
    dropped. Returns the media details finalize_localization needs.
    """

    from app.videos import wait_for_subprocess

    name = os.path.basename(file_path)

    if container == "Matroska":
        media_info = MediaInfo.parse(file_path)
        audio_tracks = get_audio_tracks_from_file(file_path)
        subtitle_tracks = get_subtitle_tracks_from_file(file_path)

        # Set the first audio track as the only default audio track

        if len(audio_tracks) >= 1:
            current_app.logger.info(
                f"'{name}' Setting the first audio track as the only default"
            )

            audio_flag_args = [
                "--edit",
                "track:a1",
                "--set",
                "flag-default=1",
            ]

            if audio_tracks[0].get("language") == "und":
                audio_flag_args.extend(["--edit", "track:a1", "--set", "language=und"])

            # Clear the default flag from every other audio track, so
            # players don't choose between multiple defaults unpredictably

            for track_number in range(2, len(audio_tracks) + 1):
                audio_flag_args.extend(
                    [
                        "--edit",
                        f"track:a{track_number}",
                        "--set",
                        "flag-default=0",
                    ]
                )

            mkvpropedit_process = subprocess.Popen(
                [current_app.config["MKVPROPEDIT_BIN"], file_path] + audio_flag_args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                bufsize=1,
            )
            for line in mkvpropedit_process.stdout:
                current_app.logger.info(f"'{name}' {line.rstrip()}")

            wait_for_subprocess(mkvpropedit_process, ok_returncodes=(0, 1))

        # Change from ISO-639-2 to ISO-639-3 language code
        # if the file was written by MakeMKV

        native_language = current_app.config["NATIVE_LANGUAGE"]

        for track in media_info.tracks:
            if (
                track.track_type == "General"
                and track.writing_application
                and "MakeMKV" in track.writing_application
            ):
                native_language = iso_639_3_native_language()
                current_app.logger.warning(
                    f"'{name}' was created with MakeMKV. Will use ISO-639-3 "
                    f"code '{native_language}' instead of user-supplied "
                    f"ISO-639-2 '{current_app.config['NATIVE_LANGUAGE']}' when "
                    f"processing this file with mkvmerge"
                )

        # Set the first subtitle track as default if the first audio is foreign
        # and if there isn't already a default subtitle track

        existing_default_subtitle_track = any(
            track["default"] == True for track in subtitle_tracks
        )

        if (
            len(subtitle_tracks) >= 1
            and len(audio_tracks) >= 1
            and audio_tracks[0].get("language") != native_language
            and audio_tracks[0].get("language") != "und"
            and not existing_default_subtitle_track
        ):
            current_app.logger.info(
                f"'{name}' Setting the first subtitle track as default"
            )
            mkvpropedit_process = subprocess.run(
                [
                    current_app.config["MKVPROPEDIT_BIN"],
                    file_path,
                    "--edit",
                    "track:s1",
                    "--set",
                    "flag-default=1",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
            )
            for line in mkvpropedit_process.stdout.splitlines():
                current_app.logger.info(f"'{name}' {line.rstrip()}")

            wait_for_subprocess(mkvpropedit_process, ok_returncodes=(0, 1))

        # Remove any subtitle tracks that have zero elements

        remove_empty_subtitle_tracks(file_path)

    return _extract_media_details(file_path)


def move_localized_file(
    source_path, file_details, lock, hidden_output_file, transient_retries=0
):
    """Carry the localized output to a hidden name at its library destination.

    This is the long file copy, split out of finalize_localization so it runs
    on the file-operation queue: several copies can run in parallel, and the
    single-worker sql queue only ever sees the quick database work plus an
    instant same-volume rename. The title lock passes through to finalize.
    """

    from app.videos import (
        MAX_TRANSIENT_RETRIES,
        TRANSIENT_COPY_ERRNOS,
        _dead_volumes,
        _rename_with_retries,
        copy_with_progress,
    )

    with app.app_context():
        basename = file_details.get("basename")
        output_directory = os.path.join(
            current_app.config["LIBRARY_DIR"], file_details.get("dirname")
        )

        # Defer if a needed volume is dead — the title lock stays held for
        # the retry

        dead = _dead_volumes(
            [
                output_directory,
                os.path.dirname(hidden_output_file),
                os.path.dirname(source_path),
            ]
        )
        if dead:
            current_app.logger.warning(
                f"'{basename}' Volumes unavailable ({', '.join(dead)}), "
                f"retrying the library copy in 5 minutes"
            )
            current_app.file_queue.enqueue_in(
                timedelta(minutes=5),
                "app.videos.move_localized_file",
                source_path,
                file_details,
                lock,
                hidden_output_file,
                transient_retries=transient_retries,
                job_timeout=current_app.config["MOVE_TASK_TIMEOUT"],
                job_id=retry_job_id(
                    "move_localized_file", f"'{basename}'", transient_retries
                ),
                result_ttl=86400,
                description=f"'{basename}'",
            )
            return False

        destination_hidden = os.path.join(output_directory, f".{basename}")

        try:
            job = get_current_job()

            # Final flag edits and metadata extraction happen here, while the
            # file is still on local staging, so the sql queue never has to
            # open the file at all

            inspection = inspect_localized_file(
                hidden_output_file, file_details.get("container"), job
            )

            try:
                os.makedirs(output_directory, exist_ok=True)

                if hidden_output_file == destination_hidden:
                    # Legacy unstaged processing already left it at the destination
                    pass

                elif (
                    os.stat(os.path.dirname(hidden_output_file)).st_dev
                    == os.stat(output_directory).st_dev
                ):
                    _rename_with_retries(hidden_output_file, destination_hidden)

                else:
                    copy_with_progress(
                        hidden_output_file, destination_hidden, job, basename
                    )
                    os.remove(hidden_output_file)

            except OSError as e:
                if (
                    e.errno not in TRANSIENT_COPY_ERRNOS
                    or transient_retries >= MAX_TRANSIENT_RETRIES
                ):
                    raise

                # A flaky mount interrupted the library copy, but the
                # localized output is still intact on staging: remove the
                # partial destination and retry just this copy rather than
                # rejecting and redoing the whole import. The title lock
                # stays held for the retry

                current_app.logger.warning(
                    f"'{basename}' Library copy failed with a transient I/O "
                    f"error ({e}), retrying in 5 minutes (attempt "
                    f"{transient_retries + 1} of {MAX_TRANSIENT_RETRIES})"
                )
                try:
                    os.remove(destination_hidden)
                except OSError:
                    pass
                current_app.file_queue.enqueue_in(
                    timedelta(minutes=5),
                    "app.videos.move_localized_file",
                    source_path,
                    file_details,
                    lock,
                    hidden_output_file,
                    transient_retries=transient_retries + 1,
                    job_timeout=current_app.config["MOVE_TASK_TIMEOUT"],
                    job_id=retry_job_id(
                        "move_localized_file", f"'{basename}'", transient_retries + 1
                    ),
                    result_ttl=86400,
                    description=f"'{basename}'",
                )
                return False

        except Exception:
            current_app.logger.error(traceback.format_exc())

            # Remove both hidden copies; the original source is untouched and
            # is what gets rejected (best effort)

            for stray in (hidden_output_file, destination_hidden):
                try:
                    os.remove(stray)
                except OSError:
                    pass

            try:
                move_to_rejects(source_path, "exception")
            except Exception:
                current_app.logger.error(traceback.format_exc())

            if lock:
                current_app.lock_manager.unlock(lock)
                current_app.logger.info(f"Removed lock {lock}")

        else:
            current_app.sql_queue.enqueue(
                "app.videos.finalize_localization",
                args=(source_path, file_details, lock, destination_hidden, inspection),
                job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
                description=f"'{basename}'",
            )

        return True


def finalize_localization(
    file_path, file_details, lock, hidden_output_file=None, inspection=None
):
    """Add a localized file to the database and move it into position.

    - A record of the localized file is added to the database.
    - The movie or tv show is updated with data from either TheMovieDB or TheTVDB.
    - Supplemental movie / tv show files (e.g. images) are downloaded.
    - The localized file is moved into position.
    - Changes are committed to the database.

    hidden_output_file is where localization left the processed file; when
    omitted (jobs from before local staging existed), it's assumed to be
    hidden in the destination directory.
    """

    from app.videos import _dead_volumes, _rename_with_retries

    with app.app_context():
        output_directory = os.path.join(
            current_app.config["LIBRARY_DIR"], file_details.get("dirname")
        )
        if hidden_output_file is None:
            hidden_output_file = os.path.join(
                output_directory, f".{file_details.get('basename')}"
            )

        # Defer if a needed volume is dead — before the try block, so the
        # title lock stays held for the retry instead of being released

        dead = _dead_volumes([output_directory, os.path.dirname(file_path)])
        if dead:
            current_app.logger.warning(
                f"'{file_details.get('basename')}' Volumes unavailable "
                f"({', '.join(dead)}), retrying finalization in 5 minutes"
            )
            current_app.sql_queue.enqueue_in(
                timedelta(minutes=5),
                "app.videos.finalize_localization",
                file_path,
                file_details,
                lock,
                hidden_output_file,
                job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
                job_id=safe_job_id(
                    f"retry:finalize_localization:'{file_details.get('basename')}'"
                ),
                result_ttl=86400,
                description=f"'{file_details.get('basename')}'",
            )
            return False

        # When the copy is handed back to move_localized_file, the title lock
        # must survive for the retried chain instead of being released below

        handed_off = False

        try:

            # Determine output file to be created

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

                    # Check the new movie against the (cached) Criterion
                    # list; a Wikidata hiccup must never fail an import —
                    # the monthly refresh catches the movie up later

                    try:
                        criterion_collection = get_criterion_collection_from_wikidata()
                    except Exception:
                        current_app.logger.warning(traceback.format_exc())
                        criterion_collection = []

                    assign_criterion_release(
                        movie, *criterion_release_lookups(criterion_collection)
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

            # Media details arrive precomputed from move_localized_file; the
            # fallback inspection covers jobs from before the split existed

            if inspection is None:
                inspection = inspect_localized_file(
                    hidden_output_file, file_details.get("container")
                )

            output_audio_tracks = inspection["audio_tracks"]
            output_subtitle_tracks = inspection["subtitle_tracks"]

            # Set file video track info

            for field, value in inspection["video"].items():
                setattr(file, field, value)

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

            # Set file subtitle track info. The flag pass marks
            # suspicious tracks' forced state unknown; whether the file
            # needs triage is decided later by the candidates query.
            # Imported content is NEW evidence: a re-imported file
            # wipes any earlier reviewed verdict and stale aids first
            # (a replacement may carry a forced track the
            # original didn't)

            from app.triage import reset_triage_state

            reset_triage_state(file)
            flag_possibly_forced_subtitles(file, output_subtitle_tracks)

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
            file.aws_untouched_filesize_bytes = file_details.get(
                "aws_untouched_filesize_bytes"
            )

            record_filesize(file, inspection["filesize_bytes"])
            current_app.logger.info(
                f"'{os.path.basename(hidden_output_file)}' {file.filesize_bytes} bytes"
            )

            # Find and remove any worse-quality files before moving the new file into place
            # so we don't delete any special features where old and new filenames are the same

            worse_files = file.find_worse_files()
            current_app.logger.info(f"{file} worse files: {worse_files}")

            worse_aws_keys = []
            worse_derived_paths = []

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
                        # Note the key now, but only delete it from AWS after the
                        # database commit succeeds, so a failed commit can't cost
                        # us the backup of a record that rolled back
                        worse_aws_keys.append(worse.aws_untouched_key)

                    # The replaced file's transcoded copies go with it
                    # — paths noted now, removed after the commit,
                    # same posture as the AWS keys; the rows themselves
                    # cascade away with the delete

                    from app.transcodes import derived_paths_for

                    worse_derived_paths += derived_paths_for(worse)
                    db.session.delete(worse)

                if (
                    worse.quality.physical_media == True
                    and file.quality.physical_media == True
                ):
                    admin_user = User.query.filter(User.admin == True).first()
                    send_email_async(
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
                        send_email_async(
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

            # Move the new file into place. move_localized_file already put it
            # on the destination volume, so this is an instant rename; if it
            # somehow isn't there, hand the copy back to the file-operation
            # queue rather than doing long file work on the sql queue

            os.makedirs(output_directory, exist_ok=True)
            if (
                os.stat(os.path.dirname(hidden_output_file)).st_dev
                != os.stat(output_directory).st_dev
            ):
                current_app.logger.warning(
                    f"'{file_details.get('basename')}' isn't at the library "
                    f"volume yet; re-queueing the library copy"
                )
                db.session.rollback()
                handed_off = True
                current_app.file_queue.enqueue(
                    "app.videos.move_localized_file",
                    args=(file_path, file_details, lock, hidden_output_file),
                    job_timeout=current_app.config["MOVE_TASK_TIMEOUT"],
                    description=f"'{file_details.get('basename')}'",
                )
                return False

            _rename_with_retries(hidden_output_file, output_file)

            db.session.commit()

            # Delete the replaced files' AWS archives now that the commit
            # succeeded, from the file-operation queue so the sql worker
            # doesn't wait on the network

            for worse_key in worse_aws_keys:
                # The new file can claim the very key its predecessor
                # held (a repointed key, or a re-import on the same
                # basename) — never delete a key a surviving row claims
                if untouched_key_still_claimed(worse_key):
                    current_app.logger.info(
                        f"Keeping '{worse_key}' in AWS — another file "
                        f"record still claims it"
                    )
                    continue
                current_app.file_queue.enqueue(
                    "app.videos.aws_delete",
                    args=(worse_key,),
                    job_timeout=current_app.config["FILE_TASK_TIMEOUT"],
                    description=f"Deleting '{worse_key}' from AWS",
                )

            if worse_derived_paths:
                from app.transcodes import purge_derived_paths

                purge_derived_paths(worse_derived_paths)

            # Remove the file that was imported unless it was replaced by the localized file
            # (we don't want to remove the file we just created!)

            if file_path != output_file:
                try:
                    os.remove(file_path)

                except FileNotFoundError:
                    pass

            # TMDB enrichment runs as its own task after the commit, so this
            # task never waits on the network; it emails if the movie still
            # can't be matched. The fetch runs on the request queue and
            # hands its payload to the sql queue for the database writes.

            # A filename id tag rides along so the enrichment fetches that
            # exact title instead of searching by name (#155)

            if file.movie_id and movie.tmdb_id == None and not movie.tmdb_ignored:
                current_app.request_queue.enqueue(
                    "app.videos.refresh_tmdb_info",
                    args=("Movies", movie.id, file_details.get("tmdb_id")),
                    kwargs={"notify_if_missing": True},
                    job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
                    description=(
                        f"Refreshing TMDB data for '{movie.title} ({movie.year})'"
                    ),
                )
            elif (
                file.series_id
                and tv_series.tmdb_id == None
                and not tv_series.tmdb_ignored
            ):
                current_app.request_queue.enqueue(
                    "app.videos.refresh_tmdb_info",
                    args=("TV Shows", tv_series.id, file_details.get("tmdb_id")),
                    job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
                    description=f"Refreshing TMDB data for '{tv_series.title}'",
                )

            # A matched series whose imported slot has no episode row yet
            # gets a refresh — a fresh season may have aired since
            # the last fetch. The membership check keeps a whole-season
            # batch import to one queued job; slots TMDB simply doesn't
            # know re-check at most once per import batch.

            elif (
                file.series_id
                and not tv_series.tmdb_ignored
                and file.season is not None
                and file.episode is not None
                and tv_series.episodes.filter_by(
                    season=file.season, episode=file.episode
                ).count()
                == 0
            ):
                refresh_job_id = safe_job_id(f"tv_episode_refresh_{tv_series.id}")
                if refresh_job_id not in current_app.request_queue.job_ids:
                    current_app.request_queue.enqueue(
                        "app.videos.refresh_tmdb_info",
                        args=("TV Shows", tv_series.id, tv_series.tmdb_id),
                        job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
                        description=f"Refreshing TMDB data for '{tv_series.title}'",
                        job_id=refresh_job_id,
                    )

            # A TrueHD Atmos track without its E-AC-3 Atmos twin earns
            # the MediaConvert supplement (#55b), queued after the
            # commit so the transcode worker sees the finished records

            from app.atmos import maybe_enqueue_atmos_supplement

            maybe_enqueue_atmos_supplement(file.id)

            if possibly_foreign_language == True and len(output_audio_tracks) > 1:
                admin_user = User.query.filter(User.admin == True).first()
                send_email_async(
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

            # Generate the triage page's inspection aids proactively,
            # while the file is fresh and certainly local — gated on the
            # SAME candidates query the triage page uses, not the
            # first-track-baseline heuristic, which misses files whose
            # suspicious track comes FIRST (Baby Driver's tracks
            # read [49, 3110, 4334] elements and nothing was flagged)

            from app.triage import maybe_enqueue_triage_snapshots

            if maybe_enqueue_triage_snapshots(file.id):
                admin_user = User.query.filter(User.admin == True).first()
                send_email_async(
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
                send_email_async(
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
            if not handed_off:
                current_app.lock_manager.unlock(lock)
                current_app.logger.info(f"Removed lock {lock}")


def finalize_transcoding(file_id, lock, transient_retries=0):
    """Update a file with details about its transcoding and move it into position."""

    from app.videos import MAX_TRANSIENT_RETRIES, TRANSIENT_COPY_ERRNOS

    with app.app_context():
        # Set if the task reschedules itself: the retry inherits the lock

        handed_off = False

        try:

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

            # Track the output as a derived file: source-linked,
            # structurally outside ranking/shopping, and purged with
            # its original

            from app.transcodes import record_transcode

            record_transcode(file, output_file)

            db.session.commit()

        except OSError as e:
            db.session.rollback()
            if (
                e.errno in TRANSIENT_COPY_ERRNOS
                and transient_retries < MAX_TRANSIENT_RETRIES
            ):
                # A flaky mount interrupted the rename; the transcoded file
                # is still at its hidden name, so retry just this step with
                # the title lock held rather than losing the transcode

                handed_off = True
                current_app.logger.warning(
                    f"'{file.plex_title}' Transcode rename failed with a "
                    f"transient I/O error ({e}), retrying in 5 minutes "
                    f"(attempt {transient_retries + 1} of {MAX_TRANSIENT_RETRIES})"
                )
                current_app.sql_queue.enqueue_in(
                    timedelta(minutes=5),
                    "app.videos.finalize_transcoding",
                    file_id,
                    lock,
                    transient_retries=transient_retries + 1,
                    job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
                    job_id=retry_job_id(
                        "finalize_transcoding", file_id, transient_retries + 1
                    ),
                    result_ttl=86400,
                    description=f"'{file.plex_title}'",
                )
                return False
            current_app.logger.error(traceback.format_exc())

        except Exception:
            current_app.logger.error(traceback.format_exc())
            db.session.rollback()

        else:
            current_app.logger.info(f"{file.plex_title}' Transcode complete")

        finally:
            if not handed_off:
                current_app.lock_manager.unlock(lock)
                current_app.logger.info(f"Removed lock {lock}")


def manual_import_task():
    """Scan the Import directory and import files that aren't already in the queue."""

    with app.app_context():
        try:
            import_directory_files = os.listdir(current_app.config["IMPORT_DIR"])
            import_directory_files.sort()
            qualities = (
                db.session.query(RefQuality.quality_title)
                .order_by(RefQuality.preference.asc())
                .all()
            )
            qualities = [quality_title for (quality_title,) in qualities]

            # A filename can contain more than one quality string; track the
            # files already handled so each is only enqueued once per scan

            handled_basenames = set()

            for quality_title in qualities:
                for file in import_directory_files:
                    if (
                        importable_basename(os.path.basename(file))
                        and f"[{quality_title}]" in file
                        and os.path.basename(file) not in handled_basenames
                        and os.path.isfile(
                            os.path.join(current_app.config["IMPORT_DIR"], file)
                        )
                    ):
                        handled_basenames.add(os.path.basename(file))
                        lock = current_app.lock_manager.lock(
                            os.path.basename(file), 30000
                        )
                        if lock:
                            job_queue = []
                            localization_tasks_running = StartedJobRegistry(
                                "fitzflix-import", connection=current_app.redis
                            )
                            job_queue.extend(localization_tasks_running.get_job_ids())
                            job_queue.extend(current_app.import_queue.job_ids)
                            if safe_job_id(os.path.basename(file)) not in job_queue:
                                current_app.logger.info(
                                    f"'{os.path.basename(file)}' Found in import directory"
                                )
                                current_app.import_queue.enqueue(
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
                                    job_id=safe_job_id(os.path.basename(file)),
                                )

                            current_app.lock_manager.unlock(lock)

        except Exception:
            current_app.logger.error(traceback.format_exc())

        else:
            return True


def transcode_task(file_id):
    """Transcode a file with Handbrake."""

    from app.videos import acquire_lock_or_defer, wait_for_subprocess

    with app.app_context():
        # Define up front so the exception handler can tell whether the lock
        # was acquired before the failure

        lock = None

        try:
            job = get_current_job()

            # Find the file that will be transcoded

            file = File.query.filter_by(id=file_id).first()

            # Create the file identifier so we can create a lock on processing this file

            file_identifier = file.file_identifier()
            current_app.logger.debug(
                f"'{file.plex_title}' Lock identifier: {file_identifier}"
            )
            lock = acquire_lock_or_defer(
                file.file_identifier(),
                current_app.config["TRANSCODE_TASK_TIMEOUT"] * 1000,
                current_app.transcode_queue,
                "app.videos.transcode_task",
                minutes=(45, 75),
                timeout=current_app.config["TRANSCODE_TASK_TIMEOUT"],
                description=f"'{file.plex_title}'",
                kwargs={"file_id": file_id},
            )
            if not lock:
                return False

            # Start transcoding process

            current_app.logger.info(f"'{file.plex_title}' Starting transcoding process")

            # Determine output directories and files to be created

            input_file = os.path.join(current_app.config["LIBRARY_DIR"], file.file_path)
            output_directory = os.path.join(
                current_app.config["TRANSCODES_DIR"], file.dirname
            )
            hidden_output_file = os.path.join(
                output_directory,
                f".{file.plex_title}.{current_app.config['HANDBRAKE_EXTENSION']}",
            )
            os.makedirs(output_directory, exist_ok=True)

            if current_app.config["HANDBRAKE_PRESET_FILE"]:
                preset_file = [
                    "--preset-import-file",
                    current_app.config["HANDBRAKE_PRESET_FILE"],
                ]
            else:
                preset_file = []

            # Transcode the file with Handbrake

            current_app.logger.info(
                [
                    current_app.config["HANDBRAKE_BIN"],
                ]
                + preset_file
                + [
                    "--preset",
                    current_app.config["HANDBRAKE_PRESET"],
                    "-i",
                    input_file,
                    "-o",
                    hidden_output_file,
                ]
            )

            transcode_process = subprocess.Popen(
                [
                    current_app.config["HANDBRAKE_BIN"],
                ]
                + preset_file
                + [
                    "--preset",
                    current_app.config["HANDBRAKE_PRESET"],
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
            previous_percent = None
            for line in transcode_process.stdout:
                progress_match = re.search(
                    r"Encoding\: task \d+ of \d+, \d+\.\d+ \%", line
                )
                if progress_match:
                    progress_match = re.match(
                        r"^Encoding\: task (?P<this_task>\d+) of (?P<total_tasks>\d+), (?P<percent>\d+)",
                        line,
                    )
                    percent = int(progress_match.group("percent"))
                    if previous_percent != percent:
                        current_app.logger.info(
                            f"'{file.plex_title}' Transcoding: {percent}%"
                        )
                        previous_percent = percent
                    if job:
                        job.meta["description"] = (
                            f"'{file.plex_title}' — Transcoding file"
                        )
                        if progress_match.group("this_task") == progress_match.group(
                            "total_tasks"
                        ):
                            job.meta["progress"] = percent

                        else:
                            job.meta["progress"] = -1

                        job.save_meta()

            wait_for_subprocess(transcode_process)

        except Exception:
            current_app.logger.error(traceback.format_exc())
            if lock:
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


# Plex external-id tags (#155): a movie name or a show-folder name may
# carry {tmdb-NNN}, {imdb-ttNNN}, or {tvdb-NNN} after the year, in either
# order with {edition-...}. The id names the exact title, so when present
# it picks the metadata source instead of a title search.

ID_TAG_BLOCK = r"\{(?:tmdb-\d+|tvdb-\d+|imdb-tt\d+)\}"
NAME_TAG_BLOCK = rf"(?:{ID_TAG_BLOCK}|\{{edition-[^{{}}]+\}})"

ID_TAG_RE = re.compile(
    r"\{(?:(?P<source>tmdb|tvdb)-(?P<value>\d+)|imdb-(?P<imdb_value>tt\d+))\}"
)
EDITION_TAG_RE = re.compile(r"\{edition-(?P<edition>[^{}]+)\}")


def parse_name_tags(tags_text):
    """Split a name's brace-tag region into its external-id tags and its
    edition. Returns ([(source, value, raw_text), ...], edition_or_None).
    """

    id_tags = []
    for match in ID_TAG_RE.finditer(tags_text or ""):
        source = match.group("source") or "imdb"
        value = match.group("value") or match.group("imdb_value")
        id_tags.append((source, value, match.group(0)))
    edition_match = EDITION_TAG_RE.search(tags_text or "")
    return id_tags, edition_match.group("edition") if edition_match else None


def preferred_id_tag(id_tags):
    """tmdb outranks imdb outranks tvdb when a name carries several tags."""

    order = {"tmdb": 0, "imdb": 1, "tvdb": 2}
    return min(id_tags, key=lambda tag: order[tag[0]], default=None)


def resolve_external_id_tag(source, value, kind, log=True):
    """Resolve an imdb/tvdb tag to a TMDB id via TMDB's /find endpoint,
    so TMDB stays the single metadata source.

    kind ("movie" or "tv") selects which /find results bucket to read.
    Returns the TMDB id; False when TMDB answered and knows nothing under
    that id — the caller must reject the file rather than guess by title;
    or None when TMDB couldn't be reached, which the caller may tolerate.
    """

    try:
        r = tmdb_get(
            current_app.config["TMDB_API_URL"] + f"/find/{value}",
            params={
                "api_key": current_app.config["TMDB_API_KEY"],
                "external_source": f"{source}_id",
            },
        )
        if log:
            current_app.logger.debug(r.json())
        r.raise_for_status()
        results = r.json().get(f"{kind}_results")

    except Exception as e:
        response = getattr(e, "response", None)
        if response is not None and response.status_code == 404:
            # TMDB answered: no such external id
            return False
        current_app.logger.warning(traceback.format_exc())
        return None

    if not results:
        return False

    return results[0].get("id") or False


class FilenameRejection:
    """A falsy evaluate_filename result that still names why the file must
    be rejected, so the reject is loud and lands in a labeled rejects
    subfolder instead of the generic "incorrect filename" bucket."""

    def __init__(self, reason):
        self.reason = reason

    def __bool__(self):
        return False


def evaluate_filename(file_path, tmdb_id=None, log=True):
    """Review a file name string and return info about what movie or TV show it is.

    Pass log=False when only previewing a filename (e.g. the admin filename
    tester) so the dry run doesn't clutter the log like a real import would.
    """

    file_details = {}
    basename = os.path.basename(file_path)

    # Determine if basename matches movie or tv formats. A movie name may
    # carry Plex id/edition tags between the year and the dash (#155), and
    # Plex's yearless "Title {tmdb-NNN}" form is admitted too — but only
    # with an id tag, so "Title - [Quality].ext" stays rejected

    movie_match = re.match(
        r"(?P<title>.+) \((?P<year>\d{4})\)"
        rf"(?P<tags>(?: {NAME_TAG_BLOCK})*)"
        r" \-(?: (?P<version>.+) | )\[(?P<quality_title>.+)\]\.(?P<extension>.+)",
        basename,
    )
    yearless_movie_match = re.match(
        r"(?P<title>.+?)"
        rf"(?P<tags>(?: {NAME_TAG_BLOCK})+)"
        r" \-(?: (?P<version>.+) | )\[(?P<quality_title>.+)\]\.(?P<extension>.+)",
        basename,
    )
    tv_match = re.search(
        r"(.+) \- S(\d+)E(\d+)(?:\-E(\d+))? \-(?: (.+) | )\[(.+)\]\.(.+)", basename
    )

    # Need to try to match TV series first, otherwise a tv series with a year in the
    # name (e.g. "Doctor Who (2005) - S01E01 - [DVD].mkv") is matched as
    # movie: "Doctor Who", year: 2005, version: "S01E01"!

    if tv_match:
        tv = re.match(
            r"(?P<title>.+) \- S(?P<season>\d+)E(?P<episode>\d+)"
            r"(?:\-E(?P<last_episode>\d+))? \-(?: (?P<version>.+) | )"
            r"\[(?P<quality_title>.+)\]\.(?P<extension>.+)",
            basename,
        )

        media_library = "TV Shows"
        title = tv.group("title")

        # A Plex id tag rides on the show-folder portion of the name
        # (#155): strip it from the series title, but remember it — the
        # show folder keeps the tag, since that's where Plex reads it

        series_id_tag = None
        series_tmdb_id = None
        title_tags = re.fullmatch(
            rf"(?P<base>.+?)(?P<tags>(?: {ID_TAG_BLOCK})+)", title
        )
        if title_tags:
            title = title_tags.group("base")
            series_id_tag = preferred_id_tag(
                parse_name_tags(title_tags.group("tags"))[0]
            )

        if series_id_tag:
            source, value, _ = series_id_tag
            if source == "tmdb":
                series_tmdb_id = int(value)
            else:
                # A matched series already stores its external ids, so an
                # imdb/tvdb tag usually resolves without the network
                existing = (
                    TVSeries.query.filter(
                        (TVSeries.tvdb_id == int(value))
                        if source == "tvdb"
                        else (TVSeries.imdb_id == value)
                    )
                    .filter(TVSeries.tmdb_id != None)
                    .first()
                )
                if existing:
                    series_tmdb_id = existing.tmdb_id
                else:
                    resolved = resolve_external_id_tag(source, value, "tv", log=log)
                    if resolved is False:
                        if log:
                            current_app.logger.error(
                                f"'{basename}' carries {{{source}-{value}}}, "
                                f"but TMDB knows no series under that id"
                            )
                        return FilenameRejection("id not found")
                    series_tmdb_id = resolved

            # The id names the exact series: adopt the title of the record
            # that already owns that id rather than the filename's spelling

            if series_tmdb_id:
                existing_series = TVSeries.query.filter_by(
                    tmdb_id=series_tmdb_id
                ).first()
                if existing_series:
                    title = existing_series.title

        # Canonicalize against existing records, the
        # movie branch's convention: Sonarr may name a file with or
        # without the series' year, and either form must land on the
        # record that already owns the show rather than splitting it
        # into a second series. A YEARED name attaches to the
        # bare-titled record only when the year matches its TMDB
        # first-air year — "Batman (1992)" never lands on the 1966
        # series. A BARE name attaches to a year-suffixed record only
        # when exactly one such record exists.

        if TVSeries.query.filter_by(title=title).first() is None:
            year_form = re.fullmatch(r"(?P<base>.+) \((?P<year>\d{4})\)", title)
            if year_form:
                bare = TVSeries.query.filter_by(title=year_form.group("base")).first()
                if (
                    bare is not None
                    and bare.tmdb_first_air_date is not None
                    and bare.tmdb_first_air_date.year == int(year_form.group("year"))
                ):
                    title = bare.title
            else:
                candidates = [
                    series
                    for series in TVSeries.query.filter(
                        TVSeries.title.like(f"{title} (____)")
                    ).all()
                    if re.fullmatch(re.escape(title) + r" \(\d{4}\)", series.title)
                ]
                if len(candidates) == 1:
                    title = candidates[0].title

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

        # Tag-in → tag-out: the show folder keeps an id tag the name came
        # with, normalized to the canonical tmdb form once resolved

        if series_id_tag:
            folder_tag = (
                f"{{tmdb-{series_tmdb_id}}}" if series_tmdb_id else series_id_tag[2]
            )
            folder_title = f"{folder_title} {folder_tag}"

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
        file_details["tmdb_id"] = series_tmdb_id
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

    elif movie_match or yearless_movie_match:
        movie = movie_match or yearless_movie_match

        media_library = "Movies"
        title = movie.group("title")
        year = int(movie.group("year")) if movie_match else None

        id_tags, edition = parse_name_tags(movie.group("tags"))
        id_tag = preferred_id_tag(id_tags)

        # The yearless Plex form is only meaningful with an id tag; an
        # edition tag alone doesn't identify the film

        if year is None and id_tag is None:
            return False

        # If the file quality name doesn't match a expected name, then we must reject

        quality_title = movie.group("quality_title")
        if not RefQuality.query.filter_by(quality_title=quality_title).first():
            return False

        # An id tag picks the metadata source (#155): a tmdb tag is the id
        # itself, and imdb/tvdb tags resolve through the library first
        # (matched movies store their imdb id), then TMDB's /find. An id
        # TMDB affirmatively doesn't know is a loud reject — falling back
        # to a title search could attach the wrong film. An explicit
        # tmdb_id argument (the TMDB-refresh rename path) outranks the tag.

        id_from_tag = False
        if id_tag and not tmdb_id:
            source, value, _ = id_tag
            id_from_tag = True
            if source == "tmdb":
                tmdb_id = int(value)
            else:
                if source == "imdb":
                    existing = (
                        Movie.query.filter_by(imdb_id=value)
                        .filter(Movie.tmdb_id != None)
                        .order_by(Movie.date_created.asc())
                        .first()
                    )
                    if existing:
                        tmdb_id = existing.tmdb_id
                if not tmdb_id:
                    resolved = resolve_external_id_tag(source, value, "movie", log=log)
                    if resolved is False:
                        if log:
                            current_app.logger.error(
                                f"'{basename}' carries {{{source}-{value}}}, "
                                f"but TMDB knows no movie under that id"
                            )
                        return FilenameRejection("id not found")
                    tmdb_id = resolved

        # Name the film according to how it's named in TMDB, as a film can have alternate
        # titles / spellings. For example:
        # A Fistful of Dynamite == Duck, You Sucker
        # Fifth Avenue Girl == 5th Avenue Girl
        # etc.

        tmdb_result = None
        m = None
        if tmdb_id:
            # A record that already owns this id carries the canonical
            # title and year — no network needed

            m = (
                Movie.query.filter_by(tmdb_id=tmdb_id)
                .order_by(Movie.date_created.asc())
                .first()
            )

        if m:
            if log:
                current_app.logger.info(f"Existing movie with this TMDB id: {m}")
            title = m.title
            year = m.year

        elif tmdb_id or id_tag is None:
            # With an id tag that couldn't resolve (TMDB unreachable),
            # never run the title search — it could attach the wrong film

            try:
                if tmdb_id:
                    # Only the id, title, and release date are read here, so no
                    # appended blocks are requested
                    params = {
                        "api_key": current_app.config["TMDB_API_KEY"],
                    }
                    url = "/movie/" + str(tmdb_id)
                else:
                    params = {
                        "api_key": current_app.config["TMDB_API_KEY"],
                        "query": title,
                        "primary_release_year": year,
                    }
                    url = "/search/movie"
                r = tmdb_get(current_app.config["TMDB_API_URL"] + url, params=params)
                current_app.logger.debug(r.json())
                r.raise_for_status()

            except Exception as e:
                # Don't let a TMDB API issue prevent us from importing the
                # file — except a 404 on an id the filename itself named:
                # that id doesn't exist, and guessing would be worse

                response = getattr(e, "response", None)
                if id_from_tag and response is not None and response.status_code == 404:
                    if log:
                        current_app.logger.error(
                            f"'{basename}' carries {{tmdb-{tmdb_id}}}, "
                            f"but TMDB knows no movie under that id"
                        )
                    return FilenameRejection("id not found")
                current_app.logger.warning(traceback.format_exc())
                tmdb_result = None

            else:
                tmdb_result = r.json()

        if tmdb_result:
            if tmdb_id:
                # /movie/<id> returns the movie object itself, not a results array
                tmdb_results = [tmdb_result] if tmdb_result.get("id") else None
            else:
                tmdb_results = tmdb_result.get("results")
            if tmdb_results:
                current_app.logger.debug(f"TMDB results: {tmdb_results}")
                tmdb_film = tmdb_results[0]

                # See if we already have this tmdb_id in the database

                m = (
                    Movie.query.filter_by(tmdb_id=tmdb_film.get("id"))
                    .order_by(Movie.date_created.asc())
                    .first()
                )

                if log:
                    current_app.logger.info(f"Existing movie with this TMDB id: {m}")

                # If so, use the existing film title and year instead of what we parsed

                if m:
                    title = m.title
                    year = m.year

                # If not, use the title and year we got from TMDB

                else:
                    title = tmdb_film.get("title", title)
                    release_date = tmdb_film.get("release_date") or (
                        f"{year}-01-01" if year else None
                    )
                    if release_date:
                        release_date = datetime.strptime(release_date, "%Y-%m-%d")
                        year = release_date.year

        # The yearless form has no fallback: without a library record or a
        # reachable TMDB there is no year to file the movie under

        if year is None:
            if log:
                current_app.logger.error(
                    f"'{basename}' has no year, and its id tag couldn't be "
                    f"resolved to one"
                )
            return FilenameRejection("id not resolvable")

        if log:
            current_app.logger.info(f"File: {basename}")
            current_app.logger.info(f"Movie: {title} ({year})")

        # Tag-in → tag-out: a name that came with an id tag keeps one on
        # its library paths, normalized to the canonical tmdb form once
        # resolved, with {edition-...} after it

        if id_tag:
            id_tag_text = f"{{tmdb-{tmdb_id}}}" if tmdb_id else id_tag[2]
            display_title = f"{title} ({year}) {id_tag_text}"
        else:
            display_title = f"{title} ({year})"

        feature_type = None
        special_feature = None
        fullscreen = False
        extension = movie.group("extension")

        if edition:
            version = edition
            dirname = os.path.join(
                media_library,
                sanitize_filename(unidecode(f"{display_title} {{edition-{edition}}}")),
            )

        else:
            dirname = os.path.join(
                media_library, sanitize_filename(unidecode(display_title))
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
                    # The version string is only "Full Screen"; report the
                    # edition name, not the raw version, as the edition
                    version = edition
                    plex_title = f"{display_title} {{edition-{edition}}}"
                else:
                    version = None
                    plex_title = display_title
                basename = f"{plex_title} - Full Screen [{quality_title}].{extension}"

            elif fullscreen:
                version_strings.pop(version_strings.index("Full Screen"))
                version = " - ".join(version_strings)
                if edition:
                    plex_title = f"{display_title} {{edition-{edition}}} - {version}"
                else:
                    plex_title = f"{display_title} - {version}"
                basename = f"{plex_title} - Full Screen [{quality_title}].{extension}"

            else:
                version = " - ".join(version_strings)
                if edition:
                    plex_title = f"{display_title} {{edition-{edition}}} - {version}"
                else:
                    plex_title = f"{display_title} - {version}"
                basename = f"{plex_title} [{quality_title}].{extension}"

        else:
            if edition:
                version = edition
                plex_title = f"{display_title} {{edition-{edition}}}"
            else:
                version = None
                plex_title = display_title
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
        file_details["tmdb_id"] = tmdb_id
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
    """Move a file to the rejects directory, best effort.

    Returns False instead of raising when a volume is unavailable: a dead
    mount shouldn't turn one failure into a cascade, and the file stays
    where it is for a later re-import.

    A cross-volume move is staged through a hidden name and only promoted
    once the copy is complete, so a failure partway can never leave a
    partial file in the rejects directory under an importable name.
    """

    basename = os.path.basename(file_path)
    reject_directory = os.path.join(current_app.config["REJECTS_DIR"], reason)
    destination = os.path.join(reject_directory, basename)
    hidden_destination = os.path.join(reject_directory, f".{basename}.partial")

    try:
        os.makedirs(reject_directory, exist_ok=True)
        try:
            os.rename(file_path, destination)
        except OSError:
            # A different volume (or a rename the filesystem refused): copy
            # to the hidden name, promote it, then delete the source. If any
            # step fails, remove both destinations so the state is exactly
            # "the source stays where it is" — complete or nothing

            try:
                shutil.copy2(file_path, hidden_destination)
                os.replace(hidden_destination, destination)
                os.remove(file_path)
            except OSError:
                for stray in (hidden_destination, destination):
                    try:
                        os.remove(stray)
                    except OSError:
                        pass
                raise

    except OSError as e:
        current_app.logger.error(
            f"'{basename}' Could not be moved to the rejects "
            f"directory ({e}); leaving it in place"
        )
        return False

    current_app.logger.info(f"'{basename}' Moved to rejects directory")
    return True


def sanitize_string(
    string, additional_bad_characters=[], additional_good_characters=[]
):
    """Remove or replace bad characters in a string and convert it to ASCII."""

    original_string = string

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
    # (startswith instead of string[0] so a fully-stripped empty string doesn't crash)
    if string.startswith("."):
        string = string[1:]

    # Fail loudly rather than let an empty name flow into file or S3 key
    # construction, where it would build degenerate paths

    if not string:
        raise ValueError(
            f"'{original_string}' sanitizes to an empty string, so it can't be "
            f"used in a file or key name"
        )

    return string


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

    f, m, q, ft = file

    _, ext = os.path.splitext(f.untouched_basename)

    if m.tmdb_title == None:
        beginning = f"{m.title} ({m.year})"
    else:
        beginning = f"{m.tmdb_title} ({m.tmdb_release_date.year})"

    # Tag-in → tag-out (#155): an untouched name that carried a Plex id
    # tag keeps one, upgraded to the record's current tmdb id when known

    id_tag = ID_TAG_RE.search(f.untouched_basename)
    if id_tag:
        tag_text = f"{{tmdb-{m.tmdb_id}}}" if m.tmdb_id else id_tag.group(0)
        beginning = f"{beginning} {tag_text}"

    if f.edition != None:
        beginning = f"{beginning} {{edition-{f.edition}}}"

    beginning = f"{beginning} - "

    if f.fullscreen == True:
        ending = f"Full Screen [{q.quality_title}]{ext}"
    elif f.feature_type_id != None:
        ending = f"{ft.feature_type} - {f.plex_title} [{q.quality_title}]{ext}"
    else:
        ending = f"[{q.quality_title}]{ext}"

    reconstructed_filename = sanitize_filename(f"{beginning}{ending}")
    reconstructed_filename = " ".join(reconstructed_filename.split()).strip()

    return reconstructed_filename


# This process's app instance, resolved lazily so importing this module from
# a process that already has an application doesn't build a second one

app = LocalProxy(get_app)
