import errno
import json
import os
import random
import re
import shutil
import subprocess
import time
import traceback
import zlib

from datetime import datetime, timedelta, timezone


from pathvalidate import sanitize_filename
from pymediainfo import MediaInfo
from rq import get_current_job
from rq.registry import StartedJobRegistry
from unidecode import unidecode

from flask import current_app, render_template
from werkzeug.local import LocalProxy

from app import db, get_app, retry_job_id, safe_job_id
from app.email import send_email as send_email_async
from app.email import task_send_email as send_email
from app.maintenance import volume_alive
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
    tmdb_get,
)

# The AWS storage layer moved to app.aws_storage (#17); these re-exports
# keep every stored rq job string and import site resolving through
# app.videos, and keep this module's own callers working unchanged

from app.aws_storage import (
    EIGHT_MEGABYTES,
    DownloadProgressPercentage,
    UploadProgressPercentage,
    aws_delete,
    aws_download,
    aws_restore,
    aws_s3_client,
    aws_sqs_client,
    aws_upload,
    calculate_etag,
    delete_sqs_message,
    download_task,
    get_matching_s3_objects,
    rename_untouched_object,
    sanitize_s3_key,
    sqs_retrieve_task,
    sync_aws_s3_storage_task,
    untouched_key_still_claimed,
    upload_task,
)

# The Criterion spine catalog moved to app.criterion_catalog and the
# diary writers to app.diary (#17); same re-export contract as above

from app.criterion_catalog import (
    CRITERION_CACHE_KEY,
    CRITERION_CACHE_SECONDS,
    assign_criterion_release,
    create_criterion_catalog_records,
    criterion_release_lookups,
    get_criterion_collection_from_wikidata,
    refresh_criterion_collection_info,
    wikidata_retry_after_seconds,
)
from app.diary import (
    apply_letterboxd_import,
    apply_plex_watch,
    clear_not_interested,
    clear_watchlist,
    letterboxd_import_task,
    parse_letterboxd_export,
    plex_history_poll,
    review_task,
    star_rating_fields,
)

# __all__ marks the re-exports as deliberately public: rq job strings
# and import sites resolve them through app.videos (it also tells
# pyflakes they're used)

__all__ = [
    "EIGHT_MEGABYTES",
    "DownloadProgressPercentage",
    "UploadProgressPercentage",
    "aws_delete",
    "aws_download",
    "aws_restore",
    "aws_s3_client",
    "aws_sqs_client",
    "aws_upload",
    "calculate_etag",
    "delete_sqs_message",
    "download_task",
    "get_matching_s3_objects",
    "rename_untouched_object",
    "sanitize_s3_key",
    "sqs_retrieve_task",
    "sync_aws_s3_storage_task",
    "untouched_key_still_claimed",
    "upload_task",
    "CRITERION_CACHE_KEY",
    "CRITERION_CACHE_SECONDS",
    "assign_criterion_release",
    "create_criterion_catalog_records",
    "criterion_release_lookups",
    "get_criterion_collection_from_wikidata",
    "refresh_criterion_collection_info",
    "wikidata_retry_after_seconds",
    "apply_letterboxd_import",
    "apply_plex_watch",
    "clear_not_interested",
    "clear_watchlist",
    "letterboxd_import_task",
    "parse_letterboxd_export",
    "plex_history_poll",
    "review_task",
    "star_rating_fields",
]


def watch_mkvmerge_progress(process, job, name, activity):
    """Stream a process's output, logging its progress and updating job meta."""

    previous_percent = None
    for line in process.stdout:
        progress_match = re.search(r"Progress\: \d+\%", line)
        if progress_match:
            progress_match = re.match(r"^Progress\: (?P<percent>\d+)\%", line)
            percent = int(progress_match.group("percent"))
            if previous_percent != percent:
                current_app.logger.info(f"'{name}' {activity}: {percent}%")
                previous_percent = percent
            if job:
                job.meta["description"] = f"'{name}' — {activity}"
                job.meta["progress"] = percent
                job.save_meta()


def convert_to_matroska(file_path, output_file, job, name):
    """Remux a non-Matroska file into a Matroska container.

    Returns True on success. On failure — a format mkvmerge can't carry —
    any partial output is removed and False is returned, so the caller can
    fall back to importing the file as-is.
    """

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


# OSError numbers that signal a dropped or flaky network mount rather than a
# problem with the file itself — e.g. macOS smbfs revokes a copy's open file
# handles with EBADF when the SMB session they belong to resets mid-operation.
# These deserve a retry, never an immediate reject

TRANSIENT_COPY_ERRNOS = {
    errno.EBADF,
    errno.EIO,
    errno.ESTALE,
    errno.ENOTCONN,
    errno.ETIMEDOUT,
    errno.EHOSTDOWN,
    errno.ENETDOWN,
}

MAX_TRANSIENT_RETRIES = 3

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


def copy_with_progress(src, dst, job, name, activity="Copying to library"):
    """Copy a file in chunks, reporting progress like the external tools do."""

    total = os.path.getsize(src)
    copied = 0
    previous_percent = None

    fsrc = open(src, "rb")
    try:
        with open(dst, "wb") as fdst:
            while chunk := fsrc.read(32 * 1024 * 1024):
                fdst.write(chunk)
                copied += len(chunk)
                percent = int(copied / total * 100) if total else 100
                if previous_percent != percent:
                    current_app.logger.info(f"'{name}' {activity}: {percent}%")
                    previous_percent = percent
                    if job:
                        job.meta["description"] = f"'{name}' — {activity}"
                        job.meta["progress"] = percent
                        job.save_meta()
    except BaseException:
        try:
            fsrc.close()
        except OSError:
            pass
        raise

    # Every byte has been read and written by now, so a source that fails
    # its own close has nothing left to tell us about the copy. An SMB
    # server that has lost its handle for a file answers close() with EBADF
    # every time while still serving reads perfectly, and throwing away a
    # byte-complete copy over it just repeats the whole transfer for as
    # long as the share stays in that state

    try:
        fsrc.close()
    except OSError as e:
        if copied != total:
            raise
        current_app.logger.warning(
            f"'{name}' Source failed to close ({e}) after a complete "
            f"{total:,}-byte copy; keeping the copy"
        )


def _rename_with_retries(src, dst, attempts=5, delay=5):
    """Rename with retries.

    A busy SMB volume can briefly return spurious errors — including ENOENT
    for names that exist — so transient failures get another try before the
    error is allowed to propagate.
    """

    for attempt in range(1, attempts + 1):
        try:
            os.rename(src, dst)
            return
        except OSError as e:
            if attempt == attempts:
                raise
            current_app.logger.warning(
                f"Renaming '{os.path.basename(dst)}' failed ({e}); "
                f"retrying in {delay} seconds ({attempt}/{attempts})"
            )
            time.sleep(delay)


def _dead_volumes(paths):
    """The /Volumes mount roots among paths that aren't responding."""

    mounts = set()
    for path in paths:
        if path and path.startswith("/Volumes/"):
            mounts.add("/".join(path.split("/")[:3]))
    return sorted(mount for mount in mounts if not volume_alive(mount))


def acquire_lock_or_defer(
    resource,
    ttl_ms,
    queue,
    func,
    minutes,
    timeout,
    description,
    args=(),
    kwargs=None,
):
    """Take the redlock for a title, or schedule the task to retry later.

    Returns the lock on success, or None after scheduling the retry.
    The retry lands in the queue's native ScheduledJobRegistry (#22);
    scheduler.py's mover enqueues it when due.
    """

    lock = current_app.lock_manager.lock(resource, ttl_ms)
    if lock:
        current_app.logger.info(f"Created lock {lock}")
        return lock

    sleep_duration = random.randint(*minutes)
    current_app.logger.warning(
        f"{description} Lock exists, "
        f"returning to queue after {sleep_duration} minutes"
    )

    # The deterministic id makes repeat deferrals replace the pending retry
    # instead of stacking new ones; the day-long result ttl keeps a finished
    # retry's record alive long enough for any retry it scheduled itself

    queue.enqueue_in(
        timedelta(minutes=sleep_duration),
        func,
        *args,
        **(kwargs or {}),
        job_timeout=timeout,
        job_id=safe_job_id(f"retry:{func.rsplit('.', 1)[-1]}:{description}"),
        result_ttl=86400,
        description=description,
    )
    return None


def wait_for_subprocess(process, ok_returncodes=(0,)):
    """Wait for an external tool to finish, and raise if it exited with an error.

    Accepts either a subprocess.Popen or a subprocess.CompletedProcess. The
    mkvtoolnix tools (mkvmerge, mkvpropedit) exit with 1 for warnings and 2 for
    errors, so callers for those tools should pass ok_returncodes=(0, 1).
    """

    if hasattr(process, "wait"):
        process.wait()
    if process.returncode not in ok_returncodes:
        raise subprocess.CalledProcessError(process.returncode, process.args)


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
                try:
                    copy_with_progress(
                        file_path,
                        staged_path,
                        job,
                        basename,
                        "Copying to local staging",
                    )
                except OSError as e:
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

            current_app.file_queue.enqueue(
                "app.videos.move_localized_file",
                args=(source_path, file_details, lock, hidden_output_file),
                job_timeout=current_app.config["MOVE_TASK_TIMEOUT"],
                description=f"'{basename}'",
            )

        return True


def parse_dolby_vision_profile(hdr_format):
    """The Dolby Vision flavor ("5", "7", "8.1", …) from MediaInfo's
    combined HDR-format string, or None when the video isn't DV.

    The profile number rides in the codec-profile token (dvhe.08.06 →
    profile 8; dvhe and dvh1 are HEVC, dvav/dva1 AVC, dav1 AV1). For
    profile 8 the meaningful flavor is the cross-compatibility target,
    which MediaInfo reports as compatibility text in the same string:
    HDR10-compatible is 8.1, HLG 8.4, plain-SDR 8.2.
    """

    if not hdr_format or "dolby vision" not in hdr_format.lower():
        return None
    text = hdr_format.lower()
    profile_match = re.search(r"(?:dv(?:he|h1|av|a1)|dav1)\.0?(\d{1,2})", text)
    if not profile_match:
        return None
    profile = int(profile_match.group(1))
    if profile == 8:
        if "hdr10" in text:
            return "8.1"
        if "hlg" in text:
            return "8.4"
        if "sdr" in text:
            return "8.2"
        return "8"
    return str(profile)


def _extract_media_details(file_path):
    """Parse a file and return the media details its database records need.

    Everything here is plain data — video track fields, audio and subtitle
    track dicts, and the file size — so the sql-queue tasks that write the
    records never have to open the file themselves.
    """

    media_info = MediaInfo.parse(file_path)
    current_app.logger.debug(
        f"'{os.path.basename(file_path)}' -> {media_info.to_json()}"
    )

    video = {}
    for track in media_info.tracks:
        if track.track_type == "Video" and track.format:
            video["format"] = track.format
            break

    for track in media_info.tracks:
        if track.track_type == "Video" and track.codec_id:
            video["codec"] = track.codec_id
            break

    for track in media_info.tracks:
        if track.track_type == "Video" and track.bit_rate:
            video["video_bitrate_kbps"] = track.bit_rate / 1000
            break

    # HDR fields are always present — None when absent — so a rescan
    # of a replaced file CLEARS stale values instead of keeping them

    video["hdr_format"] = None
    for track in media_info.tracks:
        if track.track_type == "Video" and track.other_hdr_format:
            if track.other_hdr_format[0]:
                video["hdr_format"] = track.other_hdr_format[0]
                break
    video["dolby_vision_profile"] = parse_dolby_vision_profile(video["hdr_format"])

    return {
        "video": video,
        "audio_tracks": get_audio_tracks_from_file(file_path),
        "subtitle_tracks": get_subtitle_tracks_from_file(file_path),
        "filesize_bytes": os.path.getsize(file_path),
    }


def inspect_localized_file(file_path, container, job=None):
    """Apply final track flags to a localized file and report its details.

    Runs where the file lives — on local staging, before the library copy —
    so the flag edits and parsing never happen on the sql queue: the first
    audio track becomes the only default, the first subtitle track becomes
    default when the audio is foreign, and empty subtitle tracks are
    dropped. Returns the media details finalize_localization needs.
    """

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


def extract_track_metadata(file_path):
    """Drop empty subtitle tracks and report a library file's media details.

    The file half of a track rescan; the returned details feed
    save_track_metadata on the sql queue.
    """

    media_info = MediaInfo.parse(file_path)
    for track in media_info.tracks:
        if track.track_type == "General" and track.format == "Matroska":
            remove_empty_subtitle_tracks(file_path)
            break

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
            # (#74 — a replacement may carry a forced track the
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

            bytes = inspection["filesize_bytes"]
            megabytes = (bytes / 1024) / 1024
            gigabytes = ((bytes / 1024) / 1024) / 1024

            file.filesize_bytes = bytes
            file.filesize_megabytes = round(megabytes, 1)
            file.filesize_gigabytes = round(gigabytes, 1)
            current_app.logger.info(
                f"'{os.path.basename(hidden_output_file)}' {file.filesize_bytes} bytes"
            )

            # Find and remove any worse-quality files before moving the new file into place
            # so we don't delete any special features where old and new filenames are the same

            worse_files = file.find_worse_files()
            current_app.logger.info(f"{file} worse files: {worse_files}")

            worse_aws_keys = []

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

            # Remove the file that was imported unless it was replaced by the localized file
            # (we don't want to remove the file we just created!)

            if file_path != output_file:
                try:
                    os.remove(file_path)

                except FileNotFoundError:
                    pass

            # TMDb enrichment runs as its own task after the commit, so this
            # task never waits on the network; it emails if the movie still
            # can't be matched. The fetch runs on the request queue and
            # hands its payload to the sql queue for the database writes.

            if file.movie_id and movie.tmdb_id == None:
                current_app.request_queue.enqueue(
                    "app.videos.refresh_tmdb_info",
                    args=("Movies", movie.id, None),
                    kwargs={"notify_if_missing": True},
                    job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
                    description=(
                        f"Refreshing TMDB data for '{movie.title} ({movie.year})'"
                    ),
                )
            elif file.series_id and tv_series.tmdb_id == None:
                current_app.request_queue.enqueue(
                    "app.videos.refresh_tmdb_info",
                    args=("TV Shows", tv_series.id, None),
                    job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
                    description=f"Refreshing TMDB data for '{tv_series.title}'",
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
            # suspicious track comes FIRST (#74: Baby Driver's tracks
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
                        (not os.path.basename(file).startswith("."))
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


def track_metadata_scan_library():
    """Add all files in the library to the metadata scan queue."""

    with app.app_context():
        try:

            files = File.query.all()
            for file in files:
                current_app.file_queue.enqueue(
                    "app.videos.track_metadata_scan_task",
                    args=(file.id,),
                    job_timeout=current_app.config["MKVPROPEDIT_TASK_TIMEOUT"],
                    description=f"{file.basename} – Scanning track metadata",
                )

        except Exception:
            current_app.logger.error(traceback.format_exc())
            raise

        return True


def track_metadata_scan_task(file_id):
    """Scan a file's track metadata from the file-operation queue.

    The file half runs here — dropping empty subtitle tracks and parsing —
    and the extracted details are handed to save_track_metadata on the sql
    queue, with the title lock passing along. Retries later if the title
    is locked by another task.
    """

    with app.app_context():
        try:
            file = File.query.filter_by(id=file_id).first()
            if file is None:
                return False

            file_path = os.path.join(current_app.config["LIBRARY_DIR"], file.file_path)
            if not os.path.isfile(file_path):
                return True

            lock = current_app.lock_manager.lock(
                file.file_identifier(),
                current_app.config["MKVPROPEDIT_TASK_TIMEOUT"] * 1000,
            )
            if not lock:
                sleep_duration = random.randint(5, 15)
                current_app.logger.warning(
                    f"'{file.basename}' Lock exists, "
                    f"returning to queue after {sleep_duration} minutes"
                )
                current_app.file_queue.enqueue_in(
                    timedelta(minutes=sleep_duration),
                    "app.videos.track_metadata_scan_task",
                    file_id=file_id,
                    job_timeout=current_app.config["MKVPROPEDIT_TASK_TIMEOUT"],
                    job_id=safe_job_id(f"retry:track_metadata_scan_task:{file_id}"),
                    result_ttl=86400,
                    description=f"'{file.basename}'",
                )
                return True

            try:
                details = extract_track_metadata(file_path)
            except Exception:
                current_app.lock_manager.unlock(lock)
                raise

            current_app.sql_queue.enqueue(
                "app.videos.save_track_metadata",
                args=(file_id, details, lock),
                job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
                description=f"{file.basename} – Saving track metadata",
            )

        except Exception:
            current_app.logger.error(traceback.format_exc())
            db.session.rollback()
            raise

        return True


def save_track_metadata(file_id, details, lock=None):
    """Write extracted track metadata to the database.

    The sql half of a track rescan: everything here is session work fed by
    the details dict. Releases the passed title lock when done.
    """

    with app.app_context():
        try:
            file = File.query.filter_by(id=file_id).first()
            if file is None:
                return False

            # Clear metadata for existing File record

            file.date_updated = datetime.now(timezone.utc)
            FileAudioTrack.query.filter_by(file_id=file.id).delete()
            FileSubtitleTrack.query.filter_by(file_id=file.id).delete()

            # Set file video track info

            for field, value in details["video"].items():
                setattr(file, field, value)

            bytes = details["filesize_bytes"]
            megabytes = (bytes / 1024) / 1024
            gigabytes = ((bytes / 1024) / 1024) / 1024

            file.filesize_bytes = bytes
            file.filesize_megabytes = round(megabytes, 1)
            file.filesize_gigabytes = round(gigabytes, 1)
            current_app.logger.info(f"{file} {file.filesize_bytes} bytes")

            # Set file audio track info

            for i, track in enumerate(details["audio_tracks"]):
                track["file_id"] = file.id
                track["track"] = i + 1
                audio_track = FileAudioTrack(**track)
                file.audio_track = audio_track
                current_app.logger.info(f"{file} Adding audio track {audio_track}")
                db.session.add(audio_track)

            # Set file subtitle track info

            for i, track in enumerate(details["subtitle_tracks"]):
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
            return True

        finally:
            if lock:
                current_app.lock_manager.unlock(lock)
                current_app.logger.info(f"Removed lock {lock}")


def track_metadata_scan(file_id):
    """Rescan a file's metadata on demand.

    Returns False without scanning when another task holds this title's lock
    (e.g. a remux, property edit, or transcode in progress).
    """

    file = File.query.filter_by(id=file_id).first()
    lock = current_app.lock_manager.lock(
        file.file_identifier(),
        current_app.config["MKVPROPEDIT_TASK_TIMEOUT"] * 1000,
    )
    if not lock:
        current_app.logger.warning(
            f"'{file.basename}' Lock exists, not rescanning track metadata"
        )
        return False

    try:
        details = extract_track_metadata(
            os.path.join(current_app.config["LIBRARY_DIR"], file.file_path)
        )
    except Exception:
        current_app.lock_manager.unlock(lock)
        raise

    return save_track_metadata(file_id, details, lock=lock)


def mkvpropedit_task(
    file_id,
    default_audio_track,
    default_subtitle_track,
    forced_subtitle_tracks,
    transient_retries=0,
):
    """Update a file's MKV properties."""

    with app.app_context():
        file = File.query.filter_by(id=file_id).first()

        # Serialize with other tasks that rewrite this title's files or
        # track records

        lock = acquire_lock_or_defer(
            file.file_identifier(),
            current_app.config["MKVPROPEDIT_TASK_TIMEOUT"] * 1000,
            current_app.file_queue,
            "app.videos.mkvpropedit_task",
            minutes=(5, 15),
            timeout=current_app.config["MKVPROPEDIT_TASK_TIMEOUT"],
            description=f"'{file.basename}'",
            args=(
                file_id,
                default_audio_track,
                default_subtitle_track,
                forced_subtitle_tracks,
            ),
            kwargs={"transient_retries": transient_retries},
        )
        if not lock:
            return True

        try:
            return mkvpropedit_unlocked(
                file_id,
                default_audio_track,
                default_subtitle_track,
                forced_subtitle_tracks,
            )
        except OSError as e:
            if (
                e.errno in TRANSIENT_COPY_ERRNOS
                and transient_retries < MAX_TRANSIENT_RETRIES
                and not getattr(e, "retry_unsafe", False)
            ):
                # A flaky mount interrupted the edit before the file was
                # restructured, so the same track arguments are still valid:
                # retry once the mount settles. The finally releases the
                # lock, and the retry takes it again like any fresh run

                current_app.logger.warning(
                    f"'{file.basename}' MKV property edit failed with a "
                    f"transient I/O error ({e}), retrying in 5 minutes "
                    f"(attempt {transient_retries + 1} of {MAX_TRANSIENT_RETRIES})"
                )
                current_app.file_queue.enqueue_in(
                    timedelta(minutes=5),
                    "app.videos.mkvpropedit_task",
                    file_id,
                    default_audio_track,
                    default_subtitle_track,
                    forced_subtitle_tracks,
                    transient_retries=transient_retries + 1,
                    job_timeout=current_app.config["MKVPROPEDIT_TASK_TIMEOUT"],
                    job_id=retry_job_id(
                        "mkvpropedit_task", file_id, transient_retries + 1
                    ),
                    result_ttl=86400,
                    description=f"'{file.basename}'",
                )
                return False
            current_app.logger.error(traceback.format_exc())
            raise
        finally:
            current_app.lock_manager.unlock(lock)


def mkvpropedit_unlocked(
    file_id, default_audio_track, default_subtitle_track, forced_subtitle_tracks
):
    """Update a file's MKV properties; the caller must hold the title's lock."""

    with app.app_context():
        # Once the reorder remux is renamed into place the file's track
        # numbering has changed, so retrying with the caller's original
        # track arguments would flag the wrong tracks

        reordered = False

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

            # The web form sends track ids as strings; mkvmerge_task sends ints,
            # and None means the file has no audio tracks to set a default on.
            # Normalize once so every comparison below compares like with like.

            if default_audio_track is not None:
                default_audio_track = str(default_audio_track)
            if default_subtitle_track is not None:
                default_subtitle_track = str(default_subtitle_track)

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

            if localization_arguments:

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
                    current_app.logger.info(f"'{file.basename}' {line}")

                wait_for_subprocess(mkvpropedit_task, ok_returncodes=(0, 1))

                # If the default audio track isn't the first track, create a new file with the
                # default audio track prioritized so Plex selects it first

                if default_audio_track is not None and default_audio_track != "1":
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
                    hidden_output_file = os.path.join(
                        output_directory, f".{file.basename}"
                    )

                    command = [
                        current_app.config["MKVMERGE_BIN"],
                        "--track-order",
                        new_track_order,
                        "-o",
                        hidden_output_file,
                        file_path,
                    ]

                    current_app.logger.info(
                        f"'{file.basename}' Running mkvmerge: {command}"
                    )

                    mkvmerge_process = subprocess.Popen(
                        command,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        universal_newlines=True,
                        bufsize=1,
                    )

                    watch_mkvmerge_progress(
                        mkvmerge_process, job, file.basename, "Remuxing"
                    )

                    wait_for_subprocess(mkvmerge_process, ok_returncodes=(0, 1))

                    # Move the new file into place

                    os.rename(hidden_output_file, file_path)
                    reordered = True

            # Remove any subtitle tracks that have zero elements

            remove_empty_subtitle_tracks(file_path)

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

            file.date_updated = datetime.now(timezone.utc)

        except OSError as e:
            # Tell the caller whether retrying with the same arguments is
            # still safe; logging (or a quiet transient defer) is its job

            e.retry_unsafe = reordered
            db.session.rollback()
            raise

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
                    file.aws_untouched_filesize_bytes,
                ) = aws_upload(
                    file_path,
                    current_app.config["AWS_UNTOUCHED_PREFIX"],
                    force_upload=True,
                    ignore_etag=True,
                )

            except OSError as e:
                # The edit itself already succeeded and committed, so a
                # whole-task retry could re-edit a restructured file; only
                # the re-upload was lost, and the S3 sync task heals that

                e.retry_unsafe = True
                current_app.logger.error(traceback.format_exc())
                db.session.rollback()
                raise

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
        file = File.query.filter_by(id=file_id).first()

        # Serialize with other tasks that rewrite this title's files or
        # track records

        lock = acquire_lock_or_defer(
            file.file_identifier(),
            current_app.config["MKVPROPEDIT_TASK_TIMEOUT"] * 1000,
            current_app.import_queue,
            "app.videos.mkvmerge_task",
            minutes=(5, 15),
            timeout=current_app.config["MKVPROPEDIT_TASK_TIMEOUT"],
            description=f"'{file.basename}'",
            args=(file_id, audio_tracks, subtitle_tracks),
        )
        if not lock:
            return True

        try:
            return mkvmerge_unlocked(file_id, audio_tracks, subtitle_tracks)
        finally:
            current_app.lock_manager.unlock(lock)


def mkvmerge_unlocked(file_id, audio_tracks, subtitle_tracks):
    """Remux a MKV file; the caller must hold the title's lock."""

    with app.app_context():
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

            current_app.logger.info(
                f"MediaInfo tracks: "
                f"{[(track.track_type, track.track_id, track.streamorder) for track in tracks]}"
            )

            for i, track in enumerate(tracks):
                if track.track_type == "Audio" and audio_start == None:
                    audio_start = i
                if track.track_type == "Text" and subtitle_start == None:
                    subtitle_start = i

            current_app.logger.info(f"Audio tracks: {audio_tracks}")
            current_app.logger.info(f"Subtitle tracks: {subtitle_tracks}")

            current_app.logger.info(f"First audio track: {audio_start}")
            current_app.logger.info(f"First subtitle track: {subtitle_start}")

            audio_tracks = [audio_start + int(track) - 1 for track in audio_tracks]
            subtitle_tracks = [
                subtitle_start + int(track) - 1 for track in subtitle_tracks
            ]

            current_app.logger.info(f"Modified audio tracks: {audio_tracks}")
            current_app.logger.info(f"Modified subtitle tracks: {subtitle_tracks}")

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

            current_app.logger.info(f"'{file.basename}' Running mkvmerge: {command}")

            mkvmerge_process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                bufsize=1,
            )

            watch_mkvmerge_progress(mkvmerge_process, job, file.basename, "Remuxing")

            wait_for_subprocess(mkvmerge_process, ok_returncodes=(0, 1))

            # Move the new file into place

            os.rename(hidden_output_file, file_path)

            # Remove any subtitle tracks that have zero elements

            remove_empty_subtitle_tracks(file_path)

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

        except Exception:
            current_app.logger.error(traceback.format_exc())
            db.session.rollback()
            raise

        else:
            db.session.commit()
            # This task already holds the title's lock, so call the unlocked
            # variant directly instead of deadlocking against ourselves.
            # OSErrors from it leave logging to the caller, so log here

            try:
                mkvpropedit_unlocked(file.id, 1, None, None)
            except OSError:
                current_app.logger.error(traceback.format_exc())
                raise
            return True


def transcode_task(file_id):
    """Transcode a file with Handbrake."""

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


def evaluate_filename(file_path, tmdb_id=None, log=True):
    """Review a file name string and return info about what movie or TV show it is.

    Pass log=False when only previewing a filename (e.g. the admin filename
    tester) so the dry run doesn't clutter the log like a real import would.
    """

    file_details = {}
    basename = os.path.basename(file_path)

    # Determine if basename matches movie or tv formats

    movie_match = re.search(
        r"(.+) \((\d{4})\)(?: (\{edition\-(.+)\}) | )\-(?: (.+) | )\[(.+)\]\.(.+)",
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
            r"(?P<title>.+) \((?P<year>\d{4})\)(?: \{edition\-(?P<edition>.+)\} | )\-(?: (?P<version>.+) | )"
            r"\[(?P<quality_title>.+)\]\.(?P<extension>.+)",
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

        except Exception:
            # Don't let a TMDb API issue prevent us from importing the file

            current_app.logger.warning(traceback.format_exc())
            tmdb_result = None
            pass

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

                # If not, use the title and year we got from TMDb

                else:
                    title = tmdb_film.get("title", title)
                    release_date = tmdb_film.get("release_date", f"{year}-01-01")
                    release_date = datetime.strptime(release_date, "%Y-%m-%d")
                    year = release_date.year

        if log:
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
                    # The version string is only "Full Screen"; report the
                    # edition name, not the raw version, as the edition
                    version = edition
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

            # Change track channel layout to include LFE track if present;
            # leave as None if MediaInfo didn't report a usable channel count
            if audio_track["channels"] and "LFE" in track.to_data().get(
                "channel_layout", ""
            ):
                audio_track["channels"] = str(audio_track["channels"] - 1 + 0.1)
            elif audio_track["channels"] is not None:
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
                round(int(track.to_data().get("bit_rate")) / 1000)
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
                int(int(track.to_data().get("sampling_rate")) / 1000)
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


def flag_possibly_forced_subtitles(file, subtitle_tracks):
    """Speculate which subtitle tracks might actually be forced subtitle tracks.

    If a track has elements, but no more than 1/3 the elements of the first
    subtitle track, and it isn't already marked as forced, mark its forced flag
    as unknown (None) and report that the file may have a forced subtitle track.
    """

    possibly_forced_subtitle = False

    if len(subtitle_tracks) > 1:
        main_subtitle_track = subtitle_tracks[0].get("elements")
        for i, track in enumerate(subtitle_tracks[1:]):
            track_length = track.get("elements")
            forced_flag = track.get("forced")

            if (
                main_subtitle_track
                and track_length
                and track_length <= (main_subtitle_track * 0.3)
                and not forced_flag
            ):
                current_app.logger.warning(
                    f"{file} Subtitle track {i+2} has {track_length} elements "
                    f"and may be a forced subtitle track!"
                )
                subtitle_tracks[i + 1]["forced"] = None
                possibly_forced_subtitle = True

    return possibly_forced_subtitle


def remove_empty_subtitle_tracks(file_path):
    """Remux a Matroska file in place to drop subtitle tracks with zero elements.

    Only tracks whose statistics tags explicitly report zero elements are
    removed; a track with no statistics at all is left alone, since we can't
    tell whether it's actually empty.

    Returns True if the file was rewritten, False if there was nothing to remove.
    """

    basename = os.path.basename(file_path)
    job = get_current_job()
    media_info = MediaInfo.parse(file_path)

    keep_track_ids = []
    empty_track_ids = []
    for track in media_info.tracks:
        if track.track_type != "Text":
            continue

        streamorder = track.to_data().get("streamorder")
        elements = track.to_data().get("count_of_elements")

        # Tracks are selected by their mkvmerge track id (the stream order),
        # so we can't safely remux if any id is unknown

        if not str(streamorder).isdigit():
            current_app.logger.warning(
                f"'{basename}' Subtitle track ids are unknown, "
                f"skipping empty-subtitle-track removal"
            )
            return False

        if str(elements).isdigit() and int(elements) == 0:
            empty_track_ids.append(int(streamorder))
        else:
            keep_track_ids.append(int(streamorder))

    if not empty_track_ids:
        return False

    current_app.logger.info(
        f"'{basename}' Removing zero-element subtitle track id(s) {empty_track_ids}"
    )

    hidden_output_file = os.path.join(
        os.path.dirname(file_path), f".{basename}.remove-empty-subs.mkv"
    )

    command = [
        current_app.config["MKVMERGE_BIN"],
        "-o",
        hidden_output_file,
    ]

    if keep_track_ids:
        command.extend(["-s", ",".join(str(id) for id in keep_track_ids)])
    else:
        command.append("--no-subtitles")

    command.append(file_path)
    current_app.logger.info(f"'{basename}' Running mkvmerge: {command}")

    mkvmerge_process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
        bufsize=1,
    )
    watch_mkvmerge_progress(
        mkvmerge_process, job, basename, "Removing empty subtitle tracks"
    )

    wait_for_subprocess(mkvmerge_process, ok_returncodes=(0, 1))

    # Move the new file into place

    os.rename(hidden_output_file, file_path)
    return True


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


def find_or_create_tmdb_movie(tmdb_id, film_title, year, details=None):
    """(movie, created): the record for a TMDb film — reusing an existing
    row by tmdb id, or a colliding canonical title+year record, before
    creating a review-only one. The movie may have appeared since the
    caller's redirect check (an import or a concurrent log). Callers
    commit and, when created, enqueue the standard TMDb refresh.

    The caller's live TMDb payload (details) primes the display fields
    — title, date, overview, poster, runtime — so the movie page the
    redirect lands on isn't bare while the queued refresh completes;
    tmdb_data_as_of stays unset until the full refresh stamps it.
    """

    movie = Movie.query.filter_by(tmdb_id=tmdb_id).first()
    if movie is None:
        movie = Movie.query.filter_by(title=film_title, year=year).first()
        if movie is not None and movie.tmdb_id is None:
            movie.tmdb_id = tmdb_id
    created = movie is None
    if created:
        movie = Movie(title=film_title, year=year, tmdb_id=tmdb_id)
        db.session.add(movie)
    if details and movie.tmdb_title is None:
        # Title and date prime together — display code treats a set
        # tmdb_title as a promise that the release date exists
        try:
            release_date = datetime.strptime(
                details.get("release_date") or "", "%Y-%m-%d"
            )
        except ValueError:
            release_date = None
        if release_date is not None:
            movie.tmdb_title = details.get("title")
            movie.tmdb_release_date = release_date
        movie.tmdb_overview = movie.tmdb_overview or details.get("overview")
        movie.tmdb_poster_path = movie.tmdb_poster_path or details.get("poster_path")
        movie.tmdb_runtime = movie.tmdb_runtime or details.get("runtime")
    if created:
        db.session.flush()
    return movie, created


def _movie_refresh_lock_resources(*movies):
    """Every title-lock resource an import of these movies could hold.

    Covers the identifier of each existing file, plus each movie's base
    main-feature identifier so a brand-new first file of the title arriving
    mid-refresh is serialized too. Sorted, so two refreshes acquiring locks
    for overlapping movies can't deadlock each other.
    """

    resources = set()
    for movie in movies:
        if movie is None:
            continue
        resources.add(
            json.dumps(
                {
                    "title": movie.title,
                    "year": movie.year,
                    "feature_type": None,
                    "plex_title": f"{movie.title} ({movie.year})",
                    "edition": None,
                }
            )
        )
        for file in movie.files.all():
            resources.add(file.file_identifier())
    return sorted(resources)


def refresh_tmdb_info(library, id, tmdb_id=None, notify_if_missing=False):
    """Network phase of a TMDb refresh: query TMDb, then hand the payload
    to apply_tmdb_refresh on the sql queue.

    This phase runs on the user-request queue, where several jobs may run
    concurrently — safe, because it writes nothing to the database. Every
    database and library-file change happens in apply_tmdb_refresh,
    serialized through the single sql worker.
    """

    with app.app_context():
        try:

            if library == "Movies":
                movie = Movie.query.filter_by(id=id).first()
                if movie is None:
                    # e.g. merged into another record by an earlier job in
                    # a bulk refresh
                    current_app.logger.warning(
                        f"Movie id {id} no longer exists, skipping TMDb refresh"
                    )
                    return False
                description = f"Updating '{movie.title} ({movie.year})' with TMDb data"
                current_app.logger.info(f"tmdb_id: {tmdb_id}")
                tmdb_info = movie.tmdb_movie_fetch(tmdb_id)

            elif library == "TV Shows":
                tv_show = TVSeries.query.filter_by(id=id).first()
                if tv_show is None:
                    current_app.logger.warning(
                        f"TV series id {id} no longer exists, skipping TMDb refresh"
                    )
                    return False

                # Search under the canonical record's title if this series
                # already shares a tmdb_id with one

                if tv_show.tmdb_id != None:
                    existing_series = TVSeries.query.filter_by(
                        tmdb_id=tv_show.tmdb_id
                    ).first()
                    if existing_series:
                        tv_show = existing_series
                description = f"Updating '{tv_show.title}' with TMDb data"
                tmdb_info = tv_show.tmdb_tv_fetch(tmdb_id)

            else:
                return False

            # Compress the payload for its trip through Redis; a details
            # response is small, but a bulk refresh can have thousands of
            # these queued at once

            tmdb_payload = None
            if tmdb_info:
                tmdb_payload = zlib.compress(json.dumps(tmdb_info).encode("utf-8"))

            current_app.sql_queue.enqueue(
                "app.videos.apply_tmdb_refresh",
                kwargs={
                    "library": library,
                    "id": id,
                    "tmdb_id": tmdb_id,
                    "tmdb_payload": tmdb_payload,
                    "notify_if_missing": notify_if_missing,
                },
                job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
                description=description,
            )

        except Exception:
            current_app.logger.error(traceback.format_exc())
            return False

        else:
            return True


def apply_tmdb_refresh(
    library, id, tmdb_id=None, tmdb_payload=None, notify_if_missing=False
):
    """Database phase of a TMDb refresh: apply a payload fetched by
    refresh_tmdb_info, rewrite file paths, and merge duplicate records.

    Runs on the single-worker sql queue so refreshes are serialized
    against each other and all other database writes. Movie refreshes
    additionally hold the affected titles' locks for the duration, so
    they can't interleave with an import of the same title. With
    notify_if_missing (used for new imports), an email goes out if the
    movie still has no TMDb match after the payload is applied.
    """

    with app.app_context():
        locks = []
        try:
            tmdb_info = None
            if tmdb_payload:
                tmdb_info = json.loads(zlib.decompress(tmdb_payload).decode("utf-8"))

            if library == "Movies":
                # Get the Movie record to be updated

                movie = Movie.query.filter_by(id=id).first()
                if movie is None:
                    current_app.logger.warning(
                        f"Movie id {id} no longer exists, skipping TMDb refresh"
                    )
                    return False

                # Make a note of the original movie_id field.

                original_movie_id = movie.id

                # See if the requested tmdb_id already exists in the Movie table.
                # If so, we'll use that existing Movie record.

                existing_movie = None
                if tmdb_id != None:
                    existing_movie = (
                        Movie.query.filter_by(tmdb_id=tmdb_id)
                        .order_by(Movie.date_created.asc())
                        .first()
                    )

                # This task rewrites file paths and — when the TMDb id
                # reveals a duplicate — merges two movie records, so it must
                # not interleave with a localization chain holding one of
                # these titles' locks. Take every lock an import of either
                # movie could hold (in sorted order, so concurrent refreshes
                # can't deadlock); if any is busy, retry later.

                for resource in _movie_refresh_lock_resources(movie, existing_movie):
                    lock = current_app.lock_manager.lock(
                        resource, current_app.config["SQL_TASK_TIMEOUT"] * 1000
                    )
                    if not lock:
                        for held in locks:
                            current_app.lock_manager.unlock(held)
                        locks = []
                        sleep_duration = random.randint(5, 15)
                        current_app.logger.warning(
                            f"'{movie.title} ({movie.year})' A file is locked "
                            f"by another task, returning the TMDb refresh to "
                            f"the queue after {sleep_duration} minutes"
                        )
                        current_app.sql_queue.enqueue_in(
                            timedelta(minutes=sleep_duration),
                            "app.videos.apply_tmdb_refresh",
                            library=library,
                            id=id,
                            tmdb_id=tmdb_id,
                            tmdb_payload=tmdb_payload,
                            notify_if_missing=notify_if_missing,
                            job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
                            job_id=safe_job_id(
                                f"retry:apply_tmdb_refresh:{library}:{id}"
                            ),
                            result_ttl=86400,
                            description=(
                                f"Updating '{movie.title} ({movie.year})' "
                                f"with TMDb data"
                            ),
                        )
                        return False
                    locks.append(lock)

                if existing_movie:
                    movie = existing_movie
                    current_app.logger.info(f"Existing movie: {movie}")
                    existing_movie.tmdb_movie_apply(tmdb_info)
                    db.session.commit()
                else:
                    movie.tmdb_movie_apply(tmdb_info)

                if notify_if_missing and movie.tmdb_id == None:
                    admin_user = User.query.filter(User.admin == True).first()
                    send_email_async(
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

                # Make a note of the updated movie_id field.

                updated_movie_id = movie.id

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
                        # Moves the S3 object (or deliberately declines,
                        # for Deep Archive) — the field only changes when
                        # the object really moved (#64)
                        try:
                            rename_untouched_object(f, aws_untouched_key)
                        except Exception:
                            current_app.logger.error(traceback.format_exc())

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

                    new_relative = file_details.get("file_path")
                    old_file = os.path.join(
                        current_app.config["LIBRARY_DIR"], f.file_path
                    )
                    old_directory = os.path.dirname(old_file)
                    new_file = os.path.join(
                        current_app.config["LIBRARY_DIR"], new_relative
                    )

                    # A merge can land this rename on a path the target
                    # movie already owns (#64, the 25 Cats incident:
                    # os.rename silently overwrote the sibling's file,
                    # then the path UPDATE died on the unique index).
                    # Refuse loudly and leave both records untouched —
                    # the admin deletes one deliberately instead. The
                    # one benign shape — old file gone, new file already
                    # in place, no sibling row — falls through so an
                    # interrupted rename can heal its record.

                    sibling = (
                        File.query.filter(File.file_path == new_relative)
                        .filter(File.id != f.id)
                        .first()
                    )
                    collision = sibling is not None or (
                        new_file != old_file
                        and os.path.exists(new_file)
                        and os.path.exists(old_file)
                    )
                    if collision:
                        detail = (
                            f"file #{sibling.id} already claims that path"
                            if sibling
                            else "a file already exists at that path"
                        )
                        current_app.logger.error(
                            f"'{f.basename}' (file #{f.id}) not renamed to "
                            f"'{new_relative}': {detail}. Delete one copy, "
                            f"then re-assign the TMDb id."
                        )
                        admin_user = User.query.filter(User.admin == True).first()
                        send_email_async(
                            "Fitzflix - Rename collision needs triage",
                            sender=("Fitzflix", current_app.config["SERVER_EMAIL"]),
                            recipients=[admin_user.email],
                            text_body=(
                                f"Renaming '{f.basename}' (file #{f.id}) to "
                                f"'{new_relative}' was refused: {detail}.\n\n"
                                f"Delete one of the copies, then re-assign "
                                f"the TMDb id to finish the rename."
                            ),
                            html_body=(
                                f"<p>Renaming '{f.basename}' (file #{f.id}) "
                                f"to '{new_relative}' was refused: {detail}."
                                f"</p><p>Delete one of the copies, then "
                                f"re-assign the TMDb id to finish the "
                                f"rename.</p>"
                            ),
                        )
                        continue

                    os.makedirs(
                        os.path.join(
                            current_app.config["LIBRARY_DIR"],
                            file_details.get("dirname"),
                        ),
                        exist_ok=True,
                    )

                    # Database first, disk second (#64): the path update
                    # flushes inside a savepoint so a unique-index
                    # conflict surfaces BEFORE the file moves, and a
                    # failed move rolls the record straight back

                    try:
                        with db.session.begin_nested():
                            f.file_path = new_relative
                            f.dirname = file_details.get("dirname")
                            f.basename = file_details.get("basename")
                            f.plex_title = file_details.get("plex_title")
                            db.session.flush()

                            if old_file != new_file and os.path.exists(old_file):
                                current_app.logger.info(
                                    f"Renaming '{old_file}' to '{new_file}'"
                                )
                                os.rename(old_file, new_file)
                    except Exception:
                        current_app.logger.error(traceback.format_exc())
                        continue

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

                    # The path fields were already updated inside the
                    # savepoint, before the physical rename

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

            elif library == "TV Shows":
                # Get the TVSeries record to be updated

                tv_show = TVSeries.query.filter_by(id=id).first()
                if tv_show is None:
                    current_app.logger.warning(
                        f"TV series id {id} no longer exists, skipping TMDb refresh"
                    )
                    return False

                # See if the requested tmdb_id already exists in the TVSeries table.
                # If so, we'll use that existing TVSeries record.

                if tv_show.tmdb_id != None:
                    existing_series = TVSeries.query.filter_by(
                        tmdb_id=tv_show.tmdb_id
                    ).first()
                    current_app.logger.info(f"Existing TV Series: {existing_series}")
                    if existing_series:
                        tv_show = existing_series

                tv_show.tmdb_tv_apply(tmdb_info)

            db.session.commit()

        except Exception:
            current_app.logger.error(traceback.format_exc())
            db.session.rollback()

        else:
            return True

        finally:
            for held in locks:
                current_app.lock_manager.unlock(held)


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


def plan_audio_supplements(audio_tracks):
    """The output audio-track order for the supplement pass, as
    (action, source index) pairs — action "flac" converts that source
    track, "copy" carries it through.

    Every lossless track that isn't already FLAC or PCM gets a FLAC
    twin placed immediately before it, mirroring the MakeMKV "FLAC
    Plus Original Audio" rip profile; the original is always kept.
    A FLAC counts as an existing twin ONLY when it sits immediately
    before a lossless track in the same language — the exact shape
    the rip profile produces. A FLAC anywhere else could be anything
    (a commentary, say), so it is never counted as a twin, never
    moved, and never given the default slot (#69, Glenn's rule); its
    neighbor earns a freshly converted twin instead. Channel counts
    deliberately do NOT have to match: MediaInfo labels DTS-ES Matrix
    sources "6.0" while their discrete content — and therefore any
    lossless FLAC decode of them — is 5.1 (the LOTR discs), so a
    channel-strict match would call correct twins imperfect and stack
    redundant ones. Files already in the twinned shape plan as pure
    copies, keeping the pass idempotent across disc rips and S3
    re-downloads.
    """

    plan = []
    for index, track in enumerate(audio_tracks):
        if track.get("compression_mode") == "Lossless" and track.get("format") not in [
            "FLAC",
            "PCM",
        ]:
            previous = audio_tracks[index - 1] if index > 0 else None
            twinned = (
                previous is not None
                and previous.get("format") == "FLAC"
                and previous.get("language") == track.get("language")
            )
            if not twinned:
                plan.append(("flac", index))
        plan.append(("copy", index))
    return plan


def build_supplement_args(plan):
    """The ffmpeg audio arguments realizing a supplement plan.

    Codec and disposition options are numbered by OUTPUT position —
    a source track mapped twice (converted twin + original) shifts
    every later output index, so the input index only ever appears in
    the -map selector. The first output track is the default and all
    others are cleared, matching the rip profile's convention that
    the natively playable track leads.
    """

    args = []
    for output_index, (action, source_index) in enumerate(plan):
        args.extend(
            [
                "-map",
                f"0:a:{source_index}",
                f"-c:a:{output_index}",
                "flac" if action == "flac" else "copy",
            ]
        )
    for output_index in range(len(plan)):
        args.extend(
            [
                f"-disposition:a:{output_index}",
                "default" if output_index == 0 else "none",
            ]
        )
    return args


def supplement_lossless_tracks(file_path, file_id=None):
    """Give every lossless non-FLAC/PCM audio track a FLAC twin placed
    just before it, keeping the original.

    The twin plays natively on Apple TV clients while the lossless
    original stays for direct play and future passthrough. Files whose
    twins already exist — MakeMKV rips made with the "FLAC Plus
    Original Audio" profile, or re-downloads of already-supplemented
    uploads — plan as pure copies and pass through untouched, so the
    pass is safe to repeat.
    """

    with app.app_context():
        try:
            job = get_current_job()

            dirname = os.path.dirname(file_path)
            basename = os.path.basename(file_path)

            audio_tracks = get_audio_tracks_from_file(file_path)
            if not audio_tracks:
                return True

            plan = plan_audio_supplements(audio_tracks)
            conversions = sum(1 for action, _ in plan if action == "flac")
            if not conversions:
                current_app.logger.info(
                    f"'{basename}' All lossless tracks already have FLAC twins"
                )
                return True

            current_app.logger.info(f"'{basename}' Parsing with MediaInfo")
            media_info = MediaInfo.parse(file_path)
            current_app.logger.debug(f"'{basename}' -> {media_info.to_json()}")

            container = None
            file_duration = 0
            for track in media_info.tracks:
                if track.track_type == "General" and track.format:
                    container = track.format
                    current_app.logger.info(f"'{basename}' File container {container}")

                    # Convert the file duration from milliseconds to seconds
                    file_duration = int(track.duration) / 1000
                    current_app.logger.info(f"'{basename}' Duration: {file_duration}s")

            if container != "Matroska":
                current_app.logger.warning(
                    f"'{basename}' Unable to supplement lossless tracks as is not a MKV file!"
                )
                return False

            current_app.logger.info(
                f"'{basename}' Supplementing {conversions} lossless "
                f"track{'s' if conversions != 1 else ''} with FLAC"
            )
            audio_args = build_supplement_args(plan)
            current_app.logger.info(f"Audio map: {audio_args}")
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
                + audio_args
                + [
                    "-map",
                    "0:s:?",
                    "-c:s",
                    "copy",
                    temp_flac_file,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                bufsize=1,
            )
            progress = 0
            previous_percent = None
            for line in flac_track_process.stdout:
                progress_match = re.search(
                    r"time\=(?P<hour>\d{2})\:(?P<minute>\d{2}):(?P<seconds>\d{2})",
                    line,
                )
                if progress_match and file_duration:
                    hour = int(progress_match.group("hour"))
                    minutes = int(progress_match.group("minute"))
                    seconds = int(progress_match.group("seconds"))
                    progress = int(
                        (((hour * 3600) + (minutes * 60) + seconds) / file_duration)
                        * 100
                    )
                if previous_percent != progress:
                    current_app.logger.info(
                        f"'{basename}' Supplementing lossless tracks with FLAC: {progress}%"
                    )
                    previous_percent = progress
                if job:
                    job.meta["description"] = (
                        f"'{basename}' — Supplementing lossless tracks with FLAC"
                    )
                    job.meta["progress"] = progress
                    job.save_meta()

            wait_for_subprocess(flac_track_process)

            current_app.logger.info(f"'{basename}' Supplemented lossless tracks")
            current_app.logger.info(f"Moving '{temp_flac_file}' to '{file_path}'")
            shutil.move(temp_flac_file, file_path)

            if file_id:
                track_metadata_scan_task(file_id)

        except Exception:
            current_app.logger.error(traceback.format_exc())
            raise

        else:
            return True


def remux_audio_plan_task(file_id, plan):
    """Task: rebuild one LIBRARY file's audio to an explicit supplement
    plan — (action, source index) pairs in plan_audio_supplements'
    format, except hand-built, so a track can be replaced or dropped
    (what the automatic planner never does). Born for #69's imperfect
    DTS-ES twins: [["flac", 1], ["copy", 1], ["copy", 2]] decodes the
    MA into a fresh 6.0 twin and drops the old 5.1 one.

    Copy-first, the atmos task's posture throughout: one staging copy
    in, remux + verification on local disk, the verified result
    replaces the library copy, the track rows rebuild, and the
    untouched archive is force-replaced.
    """

    with app.app_context():
        file = db.session.get(File, int(file_id))
        if file is None:
            return True

        lock = acquire_lock_or_defer(
            file.file_identifier(),
            current_app.config["TRANSCODE_TASK_TIMEOUT"] * 1000,
            current_app.transcode_queue,
            "app.videos.remux_audio_plan_task",
            minutes=(5, 15),
            timeout=current_app.config["TRANSCODE_TASK_TIMEOUT"],
            description=f"'{file.basename}'",
            args=(int(file_id), plan),
        )
        if not lock:
            return True

        try:
            return _remux_audio_plan_unlocked(int(file_id), plan)
        finally:
            current_app.lock_manager.unlock(lock)


def _remux_audio_plan_unlocked(file_id, plan):
    """The remux pipeline; the caller must hold the title's lock."""

    with app.app_context():
        job = get_current_job()
        file = db.session.get(File, int(file_id))
        basename = file.basename
        file_path = os.path.join(current_app.config["LIBRARY_DIR"], file.file_path)
        if not os.path.exists(file_path):
            current_app.logger.warning(f"'{basename}' No local copy, cannot remux")
            return False

        plan = [(action, int(index)) for action, index in plan]
        staging_dir = current_app.config["STAGING_DIR"]
        staging_source = os.path.join(staging_dir, f".src-{basename}")
        staging_output = os.path.join(staging_dir, basename)
        try:
            free = shutil.disk_usage(staging_dir).free
            if free < os.path.getsize(file_path) * 2.2 + 16 * 2**30:
                raise RuntimeError(f"'{basename}' not enough staging space")

            copy_with_progress(
                file_path, staging_source, job, basename, "Copying to local staging"
            )

            media_info = MediaInfo.parse(staging_source)
            container = None
            file_duration = None
            for track in media_info.tracks:
                if track.track_type == "General" and track.format:
                    container = track.format
                    file_duration = int(track.duration) / 1000
            if container != "Matroska":
                raise RuntimeError(f"'{basename}' is not a Matroska file")

            audio_tracks = get_audio_tracks_from_file(staging_source)
            if any(index >= len(audio_tracks) for _, index in plan):
                raise RuntimeError(
                    f"'{basename}' plan references a missing audio track"
                )

            audio_args = build_supplement_args(plan)
            current_app.logger.info(f"'{basename}' Remux plan: {plan}")
            result = subprocess.run(
                [
                    current_app.config["FFMPEG_BIN"],
                    "-y",
                    "-i",
                    staging_source,
                    "-map",
                    "0:v:0",
                    "-c:v:0",
                    "copy",
                ]
                + audio_args
                + ["-map", "0:s:?", "-c:s", "copy", staging_output],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"'{basename}' ffmpeg failed: {result.stderr[-500:]}"
                )

            # Never replace the library copy with a remux that didn't
            # deliver: track count and per-position codecs must match
            # the plan, and the duration must survive

            new_audio_tracks = get_audio_tracks_from_file(staging_output)
            new_subtitle_tracks = get_subtitle_tracks_from_file(staging_output)
            if len(new_audio_tracks) != len(plan):
                raise RuntimeError(
                    f"'{basename}' produced {len(new_audio_tracks)} audio "
                    f"tracks, expected {len(plan)}"
                )
            for position, (action, index) in enumerate(plan):
                produced = new_audio_tracks[position].get("format")
                expected = (
                    "FLAC" if action == "flac" else audio_tracks[index].get("format")
                )
                if produced != expected:
                    raise RuntimeError(
                        f"'{basename}' output track {position + 1} is "
                        f"{produced}, expected {expected}"
                    )
            out_info = MediaInfo.parse(staging_output)
            out_duration = None
            for track in out_info.tracks:
                if track.track_type == "General" and track.duration:
                    out_duration = int(track.duration) / 1000
            if file_duration and (
                out_duration is None or abs(out_duration - file_duration) > 5
            ):
                raise RuntimeError(
                    f"'{basename}' duration changed: {out_duration} "
                    f"vs {file_duration}"
                )

            # The output already carries the clean basename (unlike the
            # atmos task, whose OUTPUT was the dotfile) — so the upload
            # derives the right S3 key from staging_output itself; the
            # first run renamed output onto the .src- name and uploaded
            # 40GB under 'untouched/src-…' (the Fellowship incident)

            os.remove(staging_source)
            final_staging = staging_output

            hidden_library = os.path.join(os.path.dirname(file_path), f".{basename}")
            copy_with_progress(
                final_staging, hidden_library, job, basename, "Copying to library"
            )
            os.replace(hidden_library, file_path)

            # Rebuild the track records now that the file changed

            FileAudioTrack.query.filter_by(file_id=file.id).delete()
            FileSubtitleTrack.query.filter_by(file_id=file.id).delete()
            for i, track in enumerate(new_audio_tracks):
                track["file_id"] = file.id
                track["track"] = i + 1
                db.session.add(FileAudioTrack(**track))
            flag_possibly_forced_subtitles(file, new_subtitle_tracks)
            for i, track in enumerate(new_subtitle_tracks):
                track["file_id"] = file.id
                track["track"] = i + 1
                db.session.add(FileSubtitleTrack(**track))
            file.filesize_bytes = os.path.getsize(final_staging)
            file.filesize_megabytes = round(file.filesize_bytes / 1024**2, 1)
            file.filesize_gigabytes = round(file.filesize_bytes / 1024**3, 1)
            file.date_updated = datetime.now(timezone.utc)
            db.session.commit()

            if current_app.config["ARCHIVE_ORIGINAL_MEDIA"]:
                try:
                    (
                        file.aws_untouched_key,
                        file.aws_untouched_date_uploaded,
                        file.aws_untouched_filesize_bytes,
                    ) = aws_upload(
                        final_staging,
                        current_app.config["AWS_UNTOUCHED_PREFIX"],
                        force_upload=True,
                        ignore_etag=True,
                    )
                    db.session.commit()
                except Exception:
                    current_app.logger.error(traceback.format_exc())
                    db.session.rollback()
                    raise
            return True

        finally:
            for stray in (staging_source, staging_output):
                try:
                    os.remove(stray)
                except OSError:
                    pass


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


# This process's app instance, resolved lazily so importing this module from
# a process that already has an application doesn't build a second one

app = LocalProxy(get_app)
