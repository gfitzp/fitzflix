import csv
import errno
import hashlib
import io
import json
import math
import os
import random
import re
import shutil
import subprocess
import threading
import time
import traceback
import urllib.parse
import zipfile
import zlib

from datetime import datetime, timedelta, timezone

import boto3
import botocore
import requests
import rq

from botocore.client import Config
from pathvalidate import sanitize_filename
from pymediainfo import MediaInfo
from rq import get_current_job
from rq.registry import StartedJobRegistry
from unidecode import unidecode

from flask import current_app, render_template
from werkzeug.local import LocalProxy

from app import db, get_app, safe_job_id
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
    UserWatchlist,
    movie_file_rank,
    tmdb_get,
    tv_file_rank,
)


def clear_watchlist(user_id, movie_id):
    """Drop a film from a user's watchlist, if present — watching it,
    however the watch arrives, is what completes a watchlist entry.
    Callers commit."""

    UserWatchlist.query.filter_by(user_id=int(user_id), movie_id=int(movie_id)).delete()


EIGHT_MEGABYTES = 8388608


def aws_s3_client(with_retries=False):
    """Build an S3 client using the application credentials."""

    kwargs = {
        "aws_access_key_id": current_app.config["AWS_ACCESS_KEY"],
        "aws_secret_access_key": current_app.config["AWS_SECRET_KEY"],
    }
    if with_retries:
        kwargs["config"] = Config(
            connect_timeout=20, retries={"mode": "standard", "max_attempts": 10}
        )
    return boto3.client("s3", **kwargs)


def aws_sqs_client():
    """Build an SQS client using the application credentials."""

    return boto3.client(
        "sqs",
        aws_access_key_id=current_app.config["AWS_ACCESS_KEY"],
        aws_secret_access_key=current_app.config["AWS_SECRET_KEY"],
        region_name="us-east-1",
    )


def delete_sqs_message(sqs_client, receipt_handle, note="message"):
    """Delete a message from the SQS queue; returns False when deletion fails."""

    try:
        response = sqs_client.delete_message(
            QueueUrl=current_app.config["AWS_SQS_URL"],
            ReceiptHandle=receipt_handle,
        )
        current_app.logger.debug(f"SQS delete_message response: {response}")

    except:
        current_app.logger.warning(
            f"Unable to delete message '{receipt_handle}' from SQS"
        )
        return False

    current_app.logger.info(f"Deleted {note} '{receipt_handle}' from SQS")
    return True


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

    with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
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
    scheduler,
    func,
    minutes,
    timeout,
    description,
    args=(),
    kwargs=None,
):
    """Take the redlock for a title, or schedule the task to retry later.

    Returns the lock on success, or None after scheduling the retry.
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

    scheduler.enqueue_in(
        timedelta(minutes=sleep_duration),
        func,
        *args,
        **(kwargs or {}),
        timeout=timeout,
        job_id=safe_job_id(f"retry:{func.rsplit('.', 1)[-1]}:{description}"),
        job_result_ttl=86400,
        job_description=description,
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


class UploadProgressPercentage(object):
    """Return the upload progress as a callback when uploading a file to AWS S3."""

    def __init__(self, file_path):
        self._file_path = file_path
        self._size = float(os.path.getsize(file_path))
        self._seen_so_far = 0
        self._previous_percent = None
        self._lock = threading.Lock()
        self._job = rq.get_current_job()

    def __call__(self, bytes_amount):
        with self._lock:
            self._seen_so_far += bytes_amount

            # Report a zero-byte file as already complete rather than divide by zero

            percent = int((self._seen_so_far / self._size) * 100) if self._size else 100

            # Transfer callbacks fire far more often than tool output lines,
            # so both the log line and the job-meta write wait for the
            # percentage to actually change

            if percent == self._previous_percent:
                return
            self._previous_percent = percent

            app.logger.info(
                f"'{os.path.basename(self._file_path)}' Uploading to AWS: {percent}%"
            )
            if self._job:
                self._job.meta["description"] = (
                    f"'{os.path.basename(self._file_path)}' — Uploading to AWS"
                )
                self._job.meta["progress"] = percent
                self._job.save_meta()


class DownloadProgressPercentage(object):
    """Return the download progress as a callback when downloading a file from AWS S3."""

    def __init__(self, client, bucket, key, basename):
        self._file_path = basename
        self._size = client.head_object(Bucket=bucket, Key=key).get("ContentLength", 0)
        app.logger.info(f"'{basename}' Download size: {self._size} bytes")
        self._seen_so_far = 0
        self._previous_percent = None
        self._lock = threading.Lock()
        self._job = rq.get_current_job()

    def __call__(self, bytes_amount):
        with self._lock:
            self._seen_so_far += bytes_amount

            # Report a zero-byte object as already complete rather than divide by zero

            percent = int((self._seen_so_far / self._size) * 100) if self._size else 100

            # Transfer callbacks fire far more often than tool output lines,
            # so both the log line and the job-meta write wait for the
            # percentage to actually change

            if percent == self._previous_percent:
                return
            self._previous_percent = percent

            app.logger.info(
                f"'{os.path.basename(self._file_path)}' Downloading from AWS: {percent}%"
            )
            if self._job:
                self._job.meta["description"] = (
                    f"'{os.path.basename(self._file_path)}' — Downloading from AWS"
                )
                self._job.meta["progress"] = percent
                self._job.save_meta()


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
                current_app.import_scheduler.enqueue_in(
                    timedelta(minutes=5),
                    "app.videos.localization_task",
                    file_path=file_path,
                    force_upload=force_upload,
                    ignore_etag=ignore_etag,
                    transient_retries=transient_retries,
                    completeness_retries=completeness_retries,
                    timeout=current_app.config["LOCALIZATION_TASK_TIMEOUT"],
                    job_id=safe_job_id(f"retry:localization_task:'{basename}'"),
                    job_result_ttl=86400,
                    job_description=f"'{basename}'",
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
                current_app.import_scheduler.enqueue_in(
                    timedelta(minutes=1),
                    "app.videos.localization_task",
                    file_path=file_path,
                    force_upload=force_upload,
                    ignore_etag=ignore_etag,
                    transient_retries=transient_retries,
                    completeness_retries=completeness_retries,
                    timeout=current_app.config["LOCALIZATION_TASK_TIMEOUT"],
                    job_id=safe_job_id(f"retry:localization_task:'{basename}'"),
                    job_result_ttl=86400,
                    job_description=f"'{basename}'",
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
                    current_app.import_scheduler.enqueue_in(
                        timedelta(minutes=1),
                        "app.videos.localization_task",
                        file_path=file_path,
                        force_upload=force_upload,
                        ignore_etag=ignore_etag,
                        transient_retries=transient_retries,
                        completeness_retries=completeness_retries + 1,
                        timeout=current_app.config["LOCALIZATION_TASK_TIMEOUT"],
                        job_id=safe_job_id(f"retry:localization_task:'{basename}'"),
                        job_result_ttl=86400,
                        job_description=f"'{basename}'",
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
                current_app.import_scheduler,
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
                    current_app.import_scheduler.enqueue_in(
                        timedelta(minutes=5),
                        "app.videos.localization_task",
                        file_path=source_path,
                        force_upload=force_upload,
                        ignore_etag=ignore_etag,
                        transient_retries=transient_retries + 1,
                        completeness_retries=completeness_retries,
                        timeout=current_app.config["LOCALIZATION_TASK_TIMEOUT"],
                        job_id=safe_job_id(f"retry:localization_task:'{basename}'"),
                        job_result_ttl=86400,
                        job_description=f"'{basename}'",
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

    for track in media_info.tracks:
        if track.track_type == "Video" and track.other_hdr_format:
            if track.other_hdr_format[0]:
                video["hdr_format"] = track.other_hdr_format[0]
                break

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
            current_app.file_scheduler.enqueue_in(
                timedelta(minutes=5),
                "app.videos.move_localized_file",
                source_path,
                file_details,
                lock,
                hidden_output_file,
                transient_retries=transient_retries,
                timeout=current_app.config["MOVE_TASK_TIMEOUT"],
                job_id=safe_job_id(f"retry:move_localized_file:'{basename}'"),
                job_result_ttl=86400,
                job_description=f"'{basename}'",
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
                current_app.file_scheduler.enqueue_in(
                    timedelta(minutes=5),
                    "app.videos.move_localized_file",
                    source_path,
                    file_details,
                    lock,
                    hidden_output_file,
                    transient_retries=transient_retries + 1,
                    timeout=current_app.config["MOVE_TASK_TIMEOUT"],
                    job_id=safe_job_id(f"retry:move_localized_file:'{basename}'"),
                    job_result_ttl=86400,
                    job_description=f"'{basename}'",
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
            current_app.sql_scheduler.enqueue_in(
                timedelta(minutes=5),
                "app.videos.finalize_localization",
                file_path,
                file_details,
                lock,
                hidden_output_file,
                timeout=current_app.config["SQL_TASK_TIMEOUT"],
                job_id=safe_job_id(
                    f"retry:finalize_localization:'{file_details.get('basename')}'"
                ),
                job_result_ttl=86400,
                job_description=f"'{file_details.get('basename')}'",
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

            # Set file subtitle track info

            possibly_forced_subtitle = flag_possibly_forced_subtitles(
                file, output_subtitle_tracks
            )

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

            if possibly_forced_subtitle == True:
                # Generate the triage page's inspection aids proactively,
                # while the file is fresh and certainly local

                from app.triage import maybe_enqueue_triage_snapshots

                maybe_enqueue_triage_snapshots(file.id)

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
                current_app.sql_scheduler.enqueue_in(
                    timedelta(minutes=5),
                    "app.videos.finalize_transcoding",
                    file_id,
                    lock,
                    transient_retries=transient_retries + 1,
                    timeout=current_app.config["SQL_TASK_TIMEOUT"],
                    job_id=safe_job_id(f"retry:finalize_transcoding:{file_id}"),
                    job_result_ttl=86400,
                    job_description=f"'{file.plex_title}'",
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
                current_app.file_scheduler.enqueue_in(
                    timedelta(minutes=sleep_duration),
                    "app.videos.track_metadata_scan_task",
                    file_id=file_id,
                    timeout=current_app.config["MKVPROPEDIT_TASK_TIMEOUT"],
                    job_id=safe_job_id(f"retry:track_metadata_scan_task:{file_id}"),
                    job_result_ttl=86400,
                    job_description=f"'{file.basename}'",
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
            current_app.file_scheduler,
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
                current_app.file_scheduler.enqueue_in(
                    timedelta(minutes=5),
                    "app.videos.mkvpropedit_task",
                    file_id,
                    default_audio_track,
                    default_subtitle_track,
                    forced_subtitle_tracks,
                    transient_retries=transient_retries + 1,
                    timeout=current_app.config["MKVPROPEDIT_TASK_TIMEOUT"],
                    job_id=safe_job_id(f"retry:mkvpropedit_task:{file_id}"),
                    job_result_ttl=86400,
                    job_description=f"'{file.basename}'",
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
            current_app.import_scheduler,
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


def sync_aws_s3_storage_task():
    """Add files to AWS, and remove files that aren't in the library."""

    with app.app_context():
        # Only sync when every queue is idle: a file that's mid-import,
        # mid-upload, or still waiting on database writes can exist at AWS
        # without its final database record, and the prune below would see it
        # as an extra file and delete it

        job = get_current_job()
        busy = []
        for queue_name, queue in (
            ("fitzflix-import", current_app.import_queue),
            ("fitzflix-transcode", current_app.transcode_queue),
            ("fitzflix-file-operation", current_app.file_queue),
            ("fitzflix-sql", current_app.sql_queue),
            ("fitzflix-user-request", current_app.request_queue),
            # The maintenance queue runs the hourly import sweep, which can
            # feed new files into the import pipeline mid-sync
            ("fitzflix-maintenance", current_app.maintenance_queue),
        ):
            started = StartedJobRegistry(
                queue_name, connection=current_app.redis
            ).get_job_ids()

            # This task itself is in the user-request started registry

            if job:
                started = [job_id for job_id in started if job_id != job.id]

            count = len(queue.job_ids) + len(started)
            if count:
                busy.append(f"{queue_name}: {count}")

        if busy:
            current_app.request_scheduler.enqueue_in(
                timedelta(minutes=5),
                "app.videos.sync_aws_s3_storage_task",
                timeout="24h",
                job_id=safe_job_id("retry:sync_aws_s3_storage_task"),
                job_result_ttl=86400,
                job_description="Syncing files with AWS S3 storage",
                at_front=True,
            )
            current_app.logger.info(
                f"Waiting 5 minutes for other tasks to finish "
                f"before attempting to sync ({', '.join(busy)})"
            )
            return True

        try:
            job = get_current_job()

            # Map each remote key to its object size, so file records can be
            # backfilled with the exact size that AWS bills for restores

            s3_objects = {
                object["Key"]: object["Size"]
                for object in get_matching_s3_objects(
                    app.config["AWS_BUCKET"],
                    prefix=f"{app.config['AWS_UNTOUCHED_PREFIX']}/",
                )
            }
            s3_keys = list(s3_objects)

            files = File.query.all()

            movie_rank = (
                db.session.query(
                    File.id,
                    movie_file_rank(),
                )
                .join(Movie, (Movie.id == File.movie_id))
                .join(RefQuality, (RefQuality.id == File.quality_id))
                .subquery()
            )

            tv_rank = (
                db.session.query(
                    File.id,
                    tv_file_rank(),
                )
                .join(TVSeries, (TVSeries.id == File.series_id))
                .join(RefQuality, (RefQuality.id == File.quality_id))
                .subquery()
            )

            files = (
                db.session.query(
                    File,
                    db.case(
                        (movie_rank.c.rank == 1, 1), (tv_rank.c.rank == 1, 1), else_=0
                    ).label("rank"),
                )
                .join(RefQuality, (RefQuality.id == File.quality_id))
                .outerjoin(movie_rank, (movie_rank.c.id == File.id))
                .outerjoin(tv_rank, (tv_rank.c.id == File.id))
                .order_by(RefQuality.preference.asc(), File.aws_untouched_key.asc())
                .all()
            )

            current_app.logger.info(f"Evaluating {len(files)} files for S3 sync")

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

                    # Record the object's actual size if we don't have it yet
                    # or if it has changed since it was recorded

                    remote_size = s3_objects[file.aws_untouched_key]
                    if file.aws_untouched_filesize_bytes != remote_size:
                        file.aws_untouched_filesize_bytes = remote_size

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

            # Persist any backfilled AWS object sizes

            db.session.commit()

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
                client = aws_s3_client(with_retries=True)
                client.put_object(
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
                        job_id=safe_job_id(os.path.basename(library_file)),
                    )
                    current_app.logger.info(
                        f"'{library_file}' isn't in library; added to import queue"
                    )

        except Exception:
            app.logger.error(traceback.format_exc())

        else:
            return True


def star_rating_fields(rating):
    """The UserMovieReview rating columns for a 0-5 star rating (or None)."""

    if rating is None:
        return {
            "rating": None,
            "modified_rating": None,
            "whole_stars": None,
            "half_stars": None,
        }
    modified_rating = round(rating * 2) / 2
    return {
        "rating": rating,
        "modified_rating": modified_rating,
        "whole_stars": math.floor(modified_rating),
        "half_stars": 0 if modified_rating % 1 == 0 else 1,
    }


def parse_letterboxd_export(zip_bytes):
    """Parse a Letterboxd account-export zip into one record per film.

    Combines diary.csv (watch dates and per-watch ratings), ratings.csv
    (each film's current rating), reviews.csv (review text), and
    likes/films.csv (hearts). Returns a list of films, each with a list of
    entries mirroring how Letterboxd's own importer treats rows: one entry
    per watched date, plus a dateless entry for films that were only rated
    or liked.
    """

    def rows(zf, name):
        if name not in zf.namelist():
            return []
        with zf.open(name) as f:
            return list(csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")))

    def film_key(row):
        title = (row.get("Name") or "").strip()
        year = (row.get("Year") or "").strip()
        if not title or not year.isdigit():
            return None
        return (title, int(year))

    films = {}

    def film(key):
        if key not in films:
            films[key] = {
                "title": key[0],
                "year": key[1],
                "rating": None,
                "liked": False,
                "watchlist": False,
                "entries": {},
            }
        return films[key]

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for row in rows(zf, "ratings.csv"):
            key = film_key(row)
            if key and row.get("Rating"):
                film(key)["rating"] = float(row["Rating"])

        for row in rows(zf, "likes/films.csv"):
            key = film_key(row)
            if key:
                film(key)["liked"] = True

        # watchlist.csv is the CURRENT want-to-watch list, so it wins
        # over any past watches in the same export

        for row in rows(zf, "watchlist.csv"):
            key = film_key(row)
            if key:
                film(key)["watchlist"] = True

        # Diary rows and review rows describe the same watch when they
        # share a watched date, so entries are keyed by that date

        for name in ("diary.csv", "reviews.csv"):
            for row in rows(zf, name):
                key = film_key(row)
                if not key:
                    continue
                watched = (row.get("Watched Date") or "").strip() or None
                entry = film(key)["entries"].setdefault(
                    watched,
                    {
                        "watched": watched,
                        "logged": None,
                        "rating": None,
                        "review": None,
                        "rewatch": None,
                    },
                )
                if row.get("Date"):
                    entry["logged"] = entry["logged"] or row["Date"].strip()
                if row.get("Rating"):
                    entry["rating"] = float(row["Rating"])
                if row.get("Review"):
                    entry["review"] = row["Review"]

                # Stored as stated: Letterboxd knows about viewings that
                # predate this app, so a blank cell is a first watch, not
                # an unknown — only rows without the column stay None

                if "Rewatch" in row:
                    entry["rewatch"] = (row.get("Rewatch") or "").strip() == "Yes"

    results = []
    for f in films.values():
        f["entries"] = sorted(
            f["entries"].values(), key=lambda e: (e["watched"] is None, e["watched"])
        )
        if not f["entries"] and (f["rating"] is not None or f["liked"]):
            f["entries"] = [
                {
                    "watched": None,
                    "logged": None,
                    "rating": None,
                    "review": None,
                    "rewatch": None,
                }
            ]
        if f["entries"] or f["watchlist"]:
            results.append(f)
    return results


def letterboxd_import_task(user_id, films):
    """Network phase of a Letterboxd import: match each film to the library
    or to TMDb, then hand the resolved list to apply_letterboxd_import on
    the sql queue.

    Runs on the user-request queue since resolving unowned films means
    TMDb searches; nothing here writes to the database.
    """

    with app.app_context():
        try:
            tmdb_api_key = current_app.config["TMDB_API_KEY"]
            tmdb_api_url = current_app.config["TMDB_API_URL"]
            resolved = []
            skipped = []

            for film in films:
                title, year = film["title"], film["year"]

                movie = Movie.query.filter_by(title=title, year=year).first()
                if movie:
                    film["movie_id"] = movie.id
                    resolved.append(film)
                    continue

                if not tmdb_api_key:
                    skipped.append(f"{title} ({year})")
                    continue

                # Search with the year first; Letterboxd and TMDb years can
                # disagree by one, so fall back to a title-only search and
                # accept a close match

                result = None
                r = tmdb_get(
                    tmdb_api_url + "/search/movie",
                    params={
                        "api_key": tmdb_api_key,
                        "query": title,
                        "primary_release_year": year,
                    },
                )
                r.raise_for_status()
                matches = r.json().get("results") or []
                if matches:
                    result = matches[0]
                else:
                    r = tmdb_get(
                        tmdb_api_url + "/search/movie",
                        params={"api_key": tmdb_api_key, "query": title},
                    )
                    r.raise_for_status()
                    for candidate in r.json().get("results") or []:
                        candidate_year = (candidate.get("release_date") or "")[:4]
                        if (
                            (candidate.get("title") or "").lower() == title.lower()
                            and candidate_year.isdigit()
                            and abs(int(candidate_year) - year) <= 1
                        ):
                            result = candidate
                            break

                if not result:
                    skipped.append(f"{title} ({year})")
                    continue

                existing = Movie.query.filter_by(tmdb_id=result.get("id")).first()
                if existing:
                    film["movie_id"] = existing.id
                else:
                    film["tmdb_id"] = result.get("id")
                    film["canonical_title"] = result.get("title") or title
                    release_year = (result.get("release_date") or "")[:4]
                    film["canonical_year"] = (
                        int(release_year) if release_year.isdigit() else year
                    )
                resolved.append(film)

            if skipped:
                current_app.logger.warning(
                    f"Letterboxd import: no match for {len(skipped)} film(s): "
                    f"{', '.join(skipped)}"
                )

            if resolved:
                current_app.sql_queue.enqueue(
                    "app.videos.apply_letterboxd_import",
                    args=(user_id, resolved),
                    job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
                    description=(
                        f"Importing Letterboxd data for {len(resolved)} film(s)"
                    ),
                )

        except Exception:
            current_app.logger.error(traceback.format_exc())
            return False

        else:
            return True


def apply_letterboxd_import(user_id, films):
    """Database phase of a Letterboxd import: create any missing movie
    records, then insert or update one review row per watch entry.

    Mirrors Letterboxd's own importer semantics: an entry updates the
    existing review with the same film and watched date instead of
    duplicating it, so re-importing the same export is idempotent. Movies
    created here are enriched afterwards through the standard TMDb
    refresh pipeline.
    """

    with app.app_context():
        try:
            created_movie_ids = []
            imported = 0

            for film in films:
                movie = None
                if film.get("movie_id"):
                    movie = Movie.query.filter_by(id=film["movie_id"]).first()
                elif film.get("tmdb_id") is not None:
                    movie = Movie.query.filter_by(tmdb_id=film["tmdb_id"]).first()
                    if movie is None:
                        # The canonical name may collide with an existing
                        # record; reuse it rather than violating the unique
                        # title + year constraint

                        movie = Movie.query.filter_by(
                            title=film["canonical_title"],
                            year=film["canonical_year"],
                        ).first()
                        if movie is not None and movie.tmdb_id is None:
                            movie.tmdb_id = film["tmdb_id"]
                    if movie is None:
                        movie = Movie(
                            title=film["canonical_title"],
                            year=film["canonical_year"],
                            tmdb_id=film["tmdb_id"],
                        )
                        db.session.add(movie)
                        db.session.flush()
                        created_movie_ids.append(movie.id)

                if movie is None:
                    continue

                for entry in film["entries"]:
                    date_watched = (
                        datetime.strptime(entry["watched"], "%Y-%m-%d")
                        if entry["watched"]
                        else None
                    )
                    date_reviewed = (
                        datetime.strptime(entry["logged"], "%Y-%m-%d")
                        if entry["logged"]
                        else None
                    )
                    rating = (
                        entry["rating"]
                        if entry["rating"] is not None
                        else film["rating"]
                    )

                    # Match per calendar day: a Plex-recorded watch carries a
                    # time of day, and re-importing the same Letterboxd date
                    # must update that row, not sit beside it

                    if date_watched is not None:
                        review = UserMovieReview.query.filter(
                            UserMovieReview.user_id == user_id,
                            UserMovieReview.movie_id == movie.id,
                            UserMovieReview.date_watched >= date_watched,
                            UserMovieReview.date_watched
                            < date_watched + timedelta(days=1),
                        ).first()
                    else:
                        review = UserMovieReview.query.filter_by(
                            user_id=user_id, movie_id=movie.id, date_watched=None
                        ).first()
                    if review is None:
                        review = UserMovieReview(
                            user_id=user_id,
                            movie_id=movie.id,
                            review=entry["review"] or "",
                            date_watched=date_watched,
                            date_reviewed=date_reviewed,
                            rewatch=entry.get("rewatch"),
                            **star_rating_fields(rating),
                        )
                        db.session.add(review)
                    else:
                        if rating is not None:
                            for field, value in star_rating_fields(rating).items():
                                setattr(review, field, value)
                        if entry["review"]:
                            review.review = entry["review"]
                        if date_reviewed and not review.date_reviewed:
                            review.date_reviewed = date_reviewed
                        if entry.get("rewatch") is not None:
                            review.rewatch = entry["rewatch"]
                    review.liked = review.liked or film["liked"]
                    imported += 1

                # A watched import completes any old watchlist entry, but
                # watchlist.csv reflects Letterboxd's CURRENT list — so it
                # re-adds afterwards and wins over past watches

                if film["entries"]:
                    clear_watchlist(user_id, movie.id)
                if film.get("watchlist"):
                    listed = UserWatchlist.query.filter_by(
                        user_id=user_id, movie_id=movie.id
                    ).first()
                    if listed is None:
                        db.session.add(
                            UserWatchlist(user_id=user_id, movie_id=movie.id)
                        )

            db.session.commit()

            # Enrich the newly created movies through the standard two-phase
            # refresh pipeline (TMDb fetch on the request queue, database
            # apply back on this queue)

            for movie_id in created_movie_ids:
                movie = Movie.query.filter_by(id=movie_id).first()
                current_app.request_queue.enqueue(
                    "app.videos.refresh_tmdb_info",
                    args=("Movies", movie_id, movie.tmdb_id),
                    job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
                    description=(
                        f"Refreshing TMDB data for '{movie.title} ({movie.year})'"
                    ),
                )

            current_app.logger.info(
                f"Letterboxd import: {imported} review entries across "
                f"{len(films)} films ({len(created_movie_ids)} new movie records)"
            )

        except Exception:
            current_app.logger.error(traceback.format_exc())
            db.session.rollback()
            return False

        else:
            return True


def apply_plex_watch(tmdb_id, plex_username, viewed_at, source):
    """Record one Plex movie watch, from either the webhook or the poller.

    Every watch bumps the movie's household shopping-cart priority (the
    same effect as the Tautulli add-to-cart call). When the Plex account
    maps to a Fitzflix user via User.plex_username, the watch also lands
    in their diary as an unrated review row keyed on user/movie/date —
    with rewatch computed from whether any earlier row exists.

    A Redis marker keyed on account/movie/date makes the two sources
    idempotent: whichever records the watch first wins, and repeats of the
    same film on the same day don't double-count.
    """

    with app.app_context():
        try:
            # Full timestamp in local wall-clock time, like every other
            # diary writer: the calendar day used for dedup and display
            # must be the household's day, not UTC's (a 9 PM watch is not
            # tomorrow's viewing)

            watched_at = (
                datetime.fromisoformat(viewed_at).astimezone().replace(tzinfo=None)
            )
            day_start = datetime(watched_at.year, watched_at.month, watched_at.day)
            marker = (
                f"fitzflix:plex:watch:{plex_username}:{tmdb_id}:"
                f"{day_start.strftime('%Y-%m-%d')}"
            )
            if not current_app.redis.set(marker, source, nx=True, ex=172800):
                current_app.logger.debug(
                    f"Plex watch already recorded ({marker}); skipping"
                )
                return True

            movie = Movie.query.filter_by(tmdb_id=int(tmdb_id)).first()
            if movie is None:
                current_app.logger.info(
                    f"Plex watch of tmdb:{tmdb_id} by '{plex_username}' matches "
                    f"no movie in the library; ignoring"
                )
                return True

            movie.shopping_cart_add_date = datetime.now(timezone.utc)
            movie.shopping_cart_priority = (movie.shopping_cart_priority or 0) + 1

            user = None
            if plex_username:
                user = User.query.filter_by(plex_username=plex_username).first()

            if user is not None:
                # The watch completes any watchlist entry, and one diary
                # row per calendar day, whatever the exact times

                clear_watchlist(user.id, movie.id)
                existing = UserMovieReview.query.filter(
                    UserMovieReview.user_id == user.id,
                    UserMovieReview.movie_id == movie.id,
                    UserMovieReview.date_watched >= day_start,
                    UserMovieReview.date_watched < day_start + timedelta(days=1),
                ).first()
                if existing is None:
                    rewatch = (
                        db.session.query(UserMovieReview.id)
                        .filter_by(user_id=user.id, movie_id=movie.id)
                        .first()
                        is not None
                    )
                    db.session.add(
                        UserMovieReview(
                            user_id=user.id,
                            movie_id=movie.id,
                            review="",
                            date_watched=watched_at,
                            rewatch=rewatch,
                            **star_rating_fields(None),
                        )
                    )

            db.session.commit()
            current_app.logger.info(
                f"Plex watch ({source}): '{movie.title} ({movie.year})' by "
                f"'{plex_username}'"
                + ("" if user is None else f" — recorded in {user.email}'s diary")
            )

        except Exception:
            current_app.logger.error(traceback.format_exc())
            db.session.rollback()
            return False

        else:
            return True


def _plex_tmdb_id(entry, headers):
    """Resolve a Plex history entry to a TMDb id via its metadata Guid
    list, cached in Redis since rating keys are stable."""

    rating_key = entry.get("ratingKey")
    if not rating_key:
        return None
    cache_key = f"fitzflix:plex:tmdb:{rating_key}"
    cached = current_app.redis.get(cache_key)
    if cached is not None:
        # An empty value means known-unresolvable (no TMDb guid)
        return int(cached) if cached else None

    tmdb_id = None
    try:
        r = requests.get(
            f"{current_app.config['PLEX_URL']}/library/metadata/{rating_key}",
            headers=headers,
            timeout=30,
        )
        r.raise_for_status()
        items = (r.json().get("MediaContainer") or {}).get("Metadata") or []
        guids = (items[0].get("Guid") or []) if items else []
        for guid in guids:
            match = re.match(r"tmdb://(\d+)", guid.get("id") or "")
            if match:
                tmdb_id = int(match.group(1))
                break
        if tmdb_id is None and items:
            # Legacy metadata agent: the guid is a single string
            match = re.search(r"themoviedb://(\d+)", items[0].get("guid") or "")
            if match:
                tmdb_id = int(match.group(1))
    except Exception:
        # Don't cache transient failures
        current_app.logger.warning(traceback.format_exc())
        return None

    current_app.redis.set(cache_key, str(tmdb_id) if tmdb_id else "", ex=604800)
    return tmdb_id


def plex_history_poll():
    """Poll Plex's watch history for movie scrobbles past the stored cursor.

    The self-healing backstop to the real-time webhook: anything Plex
    scrobbled while Fitzflix was down is picked up here, and the shared
    dedup marker in apply_plex_watch keeps the two sources from
    double-counting. The first run only plants the cursor, so history
    predating the feature isn't ingested.
    """

    with app.app_context():
        config = current_app.config
        if not (config["PLEX_URL"] and config["PLEX_TOKEN"]):
            return True

        redis_conn = current_app.redis
        headers = {"X-Plex-Token": config["PLEX_TOKEN"], "Accept": "application/json"}
        cursor_key = "fitzflix:plex:history-cursor"

        cursor = redis_conn.get(cursor_key)
        if cursor is None:
            redis_conn.set(cursor_key, int(time.time()))
            current_app.logger.info(
                "Plex history poll: cursor initialized; watches from now on "
                "will be recorded"
            )
            return True
        cursor = int(cursor)

        try:
            r = requests.get(
                f"{config['PLEX_URL']}/status/sessions/history/all",
                headers={**headers, "X-Plex-Container-Size": "500"},
                params={"viewedAt>": cursor, "sort": "viewedAt:asc"},
                timeout=30,
            )
            r.raise_for_status()
            entries = (r.json().get("MediaContainer") or {}).get("Metadata") or []
        except Exception:
            current_app.logger.error(traceback.format_exc())
            return False

        # Server-account id -> Plex username, for watcher attribution; a
        # failure here still counts watches toward household priority

        accounts = {}
        try:
            r = requests.get(
                f"{config['PLEX_URL']}/accounts", headers=headers, timeout=30
            )
            r.raise_for_status()
            for account in (r.json().get("MediaContainer") or {}).get("Account") or []:
                accounts[account.get("id")] = account.get("name")
        except Exception:
            current_app.logger.warning(traceback.format_exc())

        newest = cursor
        queued = 0
        for entry in entries:
            viewed_at = int(entry.get("viewedAt") or 0)
            newest = max(newest, viewed_at)
            if entry.get("type") != "movie" or viewed_at <= cursor:
                continue
            tmdb_id = _plex_tmdb_id(entry, headers)
            if tmdb_id is None:
                current_app.logger.info(
                    f"Plex history entry '{entry.get('title')}' has no TMDb "
                    f"guid; ignoring"
                )
                continue
            account_id = entry.get("accountID")
            username = accounts.get(account_id) or f"account-{account_id}"
            current_app.sql_queue.enqueue(
                "app.videos.apply_plex_watch",
                args=(
                    tmdb_id,
                    username,
                    datetime.fromtimestamp(viewed_at, tz=timezone.utc).isoformat(),
                    "history",
                ),
                job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
                description=f"Recording Plex watch of tmdb:{tmdb_id} by {username}",
            )
            queued += 1

        redis_conn.set(cursor_key, newest)
        if queued:
            current_app.logger.info(f"Plex history poll: {queued} new watch(es)")
        return True


def review_task(user_id, title, rating):
    """Import movie reviews from a Netflix export."""

    with app.app_context():
        try:
            # A title alone can be ambiguous, since the Netflix export has no
            # year; if multiple movies share this title, fall through to TMDb,
            # which resolves the year, rather than guessing with .first()

            movie_matches = Movie.query.filter_by(title=title).all()

            if len(movie_matches) == 1:
                movie = movie_matches[0]

            else:
                movie = None
                if len(movie_matches) > 1:
                    current_app.logger.warning(
                        f"'{title}' matches {len(movie_matches)} movies in the "
                        f"library; resolving via TMDb"
                    )

            if not movie:
                tmdb_info = {}
                if not current_app.config["TMDB_API_KEY"]:
                    return False
                tmdb_api_key = current_app.config["TMDB_API_KEY"]
                tmdb_api_url = current_app.config["TMDB_API_URL"]
                current_app.logger.info(f"'{title}' not in database, searching in TMDB")
                r = tmdb_get(
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

                        # Only the canonical title and release date are read
                        # here — the movie's full enrichment happens in
                        # tmdb_movie_query below

                        r = tmdb_get(
                            tmdb_api_url + "/movie/" + str(tmdb_id),
                            params={"api_key": tmdb_api_key},
                        )
                        r.raise_for_status()
                        current_app.logger.debug(f"{r.url}: {r.json()}")
                        tmdb_info = r.json()

                        tmdb_title = tmdb_info.get("title")
                        tmdb_year = None
                        if tmdb_info.get("release_date"):
                            tmdb_release_date = datetime.strptime(
                                tmdb_info.get("release_date"), "%Y-%m-%d"
                            )
                            tmdb_year = tmdb_release_date.year

                        if tmdb_title and tmdb_year:
                            # A movie with the canonical title may already
                            # exist; attach the review to it instead of
                            # violating the unique title + year constraint

                            movie = Movie.query.filter_by(
                                title=tmdb_title, year=tmdb_year
                            ).first()

                            if not movie:
                                movie = Movie(title=tmdb_title, year=tmdb_year)
                                db.session.add(movie)

                                try:
                                    # Establish a savepoint with db.session.begin_nested(),
                                    # so if any of the queries to get show metadata fail,
                                    # we can just roll back those changes to the savepoint
                                    # and still commit the movie and its review.

                                    db.session.begin_nested()
                                    movie.tmdb_movie_query()
                                    db.session.commit()

                                except Exception:
                                    current_app.logger.error(traceback.format_exc())
                                    db.session.rollback()

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
                current_app.transcode_scheduler,
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


def download_task(key, basename, sqs_receipt_handle=None, transient_retries=0):
    """Download a file from AWS S3 storage."""

    with app.app_context():
        job = get_current_job()

        file = File.query.filter_by(aws_untouched_key=key).first()
        if file:
            basename = file.untouched_basename

        if job:
            job.meta["description"] = f"'{basename}' — Downloading from AWS"
            job.save_meta()

        try:
            current_app.logger.info(
                f"Starting download of '{basename}' from AWS S3 storage"
            )
            aws_download(key, basename, sqs_receipt_handle)

        except OSError as e:
            if (
                e.errno in TRANSIENT_COPY_ERRNOS
                and transient_retries < MAX_TRANSIENT_RETRIES
            ):
                # The import volume hiccuped mid-download or mid-rename; the
                # S3 object and the SQS message are unaffected, so retry
                # once the mount settles

                current_app.logger.warning(
                    f"'{basename}' Download failed with a transient I/O "
                    f"error ({e}), returning to queue to try again in 5 "
                    f"minutes (attempt {transient_retries + 1} of "
                    f"{MAX_TRANSIENT_RETRIES})"
                )
                current_app.file_scheduler.enqueue_in(
                    timedelta(minutes=5),
                    "app.videos.download_task",
                    key,
                    basename,
                    sqs_receipt_handle,
                    transient_retries=transient_retries + 1,
                    timeout=current_app.config["TRANSCODE_TASK_TIMEOUT"],
                    job_id=safe_job_id(f"retry:download_task:'{basename}'"),
                    job_result_ttl=86400,
                    job_description=f"'{basename}' — Downloading from AWS",
                )
                return False
            current_app.logger.error(traceback.format_exc())

        except Exception:
            current_app.logger.error(traceback.format_exc())

        else:
            return True


def sqs_retrieve_task():
    """Poll AWS SQS for possible files ready to download."""

    with app.app_context():
        sqs_client = aws_sqs_client()
        s3_client = aws_s3_client(with_retries=True)

        # Extend timeout and restoration period for messages whose downloads
        # are running or waiting in the download queue

        file_operations = StartedJobRegistry(
            "fitzflix-file-operation", connection=current_app.redis
        )
        download_job_ids = file_operations.get_job_ids() + list(
            current_app.file_queue.job_ids
        )
        for job_id in download_job_ids:
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
        try:
            # Get the record of the file to be uploaded to AWS S3 storage

            file = File.query.filter_by(id=file_id).first()
            file_path = os.path.join(app.config["LIBRARY_DIR"], file.file_path)

            # Pass to the aws_upload() function for uploading.
            # Update the File record with the remote key and date it was uploaded.

            if file.aws_untouched_key:
                (
                    file.aws_untouched_key,
                    file.aws_untouched_date_uploaded,
                    file.aws_untouched_filesize_bytes,
                ) = aws_upload(
                    file_path=file_path,
                    key_name=file.aws_untouched_key,
                    force_upload=force_upload,
                    ignore_etag=ignore_etag,
                    storage_class=storage_class,
                )

            else:
                (
                    file.aws_untouched_key,
                    file.aws_untouched_date_uploaded,
                    file.aws_untouched_filesize_bytes,
                ) = aws_upload(
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
        s3_client = aws_s3_client()
        s3_client.delete_object(Bucket=current_app.config["AWS_BUCKET"], Key=key)
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

    s3_client = aws_s3_client()
    sqs_client = aws_sqs_client()

    while retry > 0:
        try:
            s3_client.download_file(
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
            # boto3 signals a missing object via Error.Code ("404"/"NoSuchKey");
            # keep the HTTP status code check as a fallback
            error_code = str(error.response.get("Error", {}).get("Code", ""))
            status_code = error.response.get("ResponseMetadata", {}).get(
                "HTTPStatusCode"
            )
            if error_code in ("404", "NoSuchKey") or status_code == 404:
                current_app.logger.info(f"'{basename}' doesn't exist in AWS S3")
                if sqs_receipt_handle:
                    if not delete_sqs_message(sqs_client, sqs_receipt_handle):
                        return False
                return True

            elif error_code == "InvalidObjectState":
                # The restored copy expired before it could be downloaded, so
                # the object is back in cold storage and this SQS message is
                # stale. Request a new restore unless one is already underway;
                # its completion notification will re-trigger the download.

                head_response = s3_client.head_object(
                    Bucket=current_app.config["AWS_BUCKET"], Key=key
                )
                restore_status = head_response.get("Restore") or ""
                if 'ongoing-request="true"' in restore_status:
                    current_app.logger.info(
                        f"'{basename}' restore is already in progress, "
                        f"waiting for its completion notification"
                    )
                else:
                    current_app.logger.info(
                        f"'{basename}' restored copy expired before download, "
                        f"requesting a new restore"
                    )
                    aws_restore(key)

                if sqs_receipt_handle:
                    if not delete_sqs_message(
                        sqs_client, sqs_receipt_handle, note="stale message"
                    ):
                        return False
                return True

            else:
                current_app.logger.error(traceback.format_exc())
                retry = retry - 1

        except OSError as e:
            if e.errno in TRANSIENT_COPY_ERRNOS:
                # A dead import volume fails instantly, so burning the whole
                # in-place retry budget on it is pointless: drop the partial
                # download and let the caller defer until the mount settles

                try:
                    os.remove(
                        os.path.join(current_app.config["IMPORT_DIR"], f".{basename}")
                    )
                except OSError:
                    pass
                raise
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
                if not delete_sqs_message(sqs_client, sqs_receipt_handle):
                    return False

            return True

    current_app.logger.error(
        f"Tried to download '{basename}' {str(MAX_RETRY_COUNT)} times but couldn't!"
    )
    return False


def aws_restore(key, days=2, tier="Standard"):
    """Request a file at AWS to be restored from Glacier status for download."""

    with app.app_context():
        try:
            s3_client = aws_s3_client(with_retries=True)

            # Make sure the key exists in the AWS bucket

            response = s3_client.list_objects(
                Bucket=current_app.config["AWS_BUCKET"], Prefix=key, MaxKeys=1
            )

            # The listing has no "Contents" key at all when nothing matches

            contents = response.get("Contents")

            # If the key exists

            if contents and contents[0].get("Key"):
                head_response = s3_client.head_object(
                    Bucket=current_app.config["AWS_BUCKET"], Key=key
                )

                if contents[0].get(
                    "StorageClass"
                ) == "STANDARD" or 'ongoing-request="false"' in head_response.get(
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
                        at_front=True,
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

            else:
                current_app.logger.warning(
                    f"'{key}' does not exist in AWS S3 storage, cannot restore"
                )

        # Only botocore ClientError instances have a .response attribute;
        # let any other exception propagate unmasked

        except botocore.exceptions.ClientError as e:
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

        # Raise instead of returning None: every caller unpacks the return value
        # as a (key, date_uploaded, filesize_bytes) tuple

        raise FileNotFoundError(
            f"'{file_path}' can't be uploaded to AWS since it's not a file"
        )

    if key_name:
        key = sanitize_s3_key(key_name)

    else:
        key = sanitize_s3_key(os.path.basename(file_path))

    key = os.path.join(key_prefix, key)

    s3_client = aws_s3_client(with_retries=True)

    # See if the key already exists in the AWS bucket

    response = s3_client.list_objects(
        Bucket=current_app.config["AWS_BUCKET"], Prefix=key, MaxKeys=1
    )

    # If the key already exists, check to see if the local and remote ETags match.
    # If the ETags match, then the files are the same and there's no need to re-upload.
    # If the IGNORE_ETAGS flag is set, only compare the file/key names, not their data.

    if not force_upload and not current_app.config["FORCE_UPLOAD"]:
        # Look for an object with this exact key: since the listing is a Prefix
        # search, it can return a different, longer key instead

        remote_etag = None
        date_uploaded = None
        remote_size = None
        for object in response.get("Contents") or []:
            if object.get("Key") == key:
                remote_etag = object.get("ETag").replace('"', "")
                date_uploaded = object.get("LastModified")
                remote_size = object.get("Size")

        if remote_etag is not None:
            if ignore_etag or current_app.config["IGNORE_ETAGS"]:
                current_app.logger.info(
                    f"'{file_path}' matches '{key}' and ETags are ignored, "
                    f"no need to re-upload"
                )
                return key, date_uploaded, remote_size

            local_etag = calculate_etag(file_path)
            if local_etag == remote_etag:
                current_app.logger.info(
                    f"'{file_path}' is the same as '{key}', no need to re-upload"
                )
                return key, date_uploaded, remote_size

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
                current_app.logger.warning(e)
                current_app.logger.warning(
                    f"'{file_path}' Retrying upload, "
                    f"this is retry {MAX_RETRY_COUNT - retry} out of {MAX_RETRY_COUNT}"
                )

            else:
                move_to_rejects(file_path, "upload error")
                current_app.logger.error(e)
                raise

        except:
            move_to_rejects(file_path, "upload error")
            raise

        else:
            current_app.logger.info(f"Uploaded '{file_path}' to AWS")
            return key, datetime.now(timezone.utc), os.path.getsize(file_path)

    current_app.logger.error(
        f"Tried to upload '{file_path}' {str(MAX_RETRY_COUNT)} times but couldn't!"
    )
    move_to_rejects(file_path, "upload error")

    # Raise instead of falling off the end returning None: every caller unpacks
    # the return value as a (key, date_uploaded, filesize_bytes) tuple

    raise RuntimeError(
        f"Unable to upload '{file_path}' to AWS after {MAX_RETRY_COUNT} attempts"
    )


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
            previous_percent = None
            for chunk in iter(lambda: f.read(EIGHT_MEGABYTES), b""):
                # Concatenate all of the MD5 hashes together
                md5_digests.append(hashlib.md5(chunk).digest())
                percent = int((f.tell() / file_size) * 100)
                if previous_percent != percent:
                    current_app.logger.info(
                        f"'{basename}' Calculating ETag: {percent}%"
                    )
                    previous_percent = percent
                if job:
                    job.meta["description"] = f"'{basename}' — Calculating ETag"
                    job.meta["progress"] = percent
                    job.save_meta()

        # Get an MD5 hash of the concatenated hashes, and append the number of parts
        # e.g. "c7c2300fd47954c421d5fe0bc7910ca3-64"
        # c7c2300fd47954c421d5fe0bc7910ca3 is the hash of the concatenated MD5 hashes,
        # and there were 64 parts/individual MD5 hashes for the uploaded file

        return (
            hashlib.md5(b"".join(md5_digests)).hexdigest() + "-" + str(len(md5_digests))
        )


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


# Wikidata models Criterion spine numbers as property P12279, TMDb movie ids
# as P4947, and publication dates as P577; the earliest publication year is
# taken since a film carries one date per release

CRITERION_SPARQL_QUERY = """
SELECT ?spine ?tmdbId ?filmLabel
       (MIN(YEAR(?date)) AS ?year)
       (SAMPLE(?criterionId) AS ?criterionId) WHERE {
  ?film wdt:P12279 ?spine .
  OPTIONAL { ?film wdt:P4947 ?tmdbId . }
  OPTIONAL { ?film wdt:P9584 ?criterionId . }
  OPTIONAL { ?film wdt:P577 ?date . }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
GROUP BY ?spine ?film ?filmLabel ?tmdbId
"""

# Box sets carry the spine number on the set's own Wikidata item, with the
# member films linked via P527 ("has part"): map each member to its set's
# spine and title

CRITERION_SETS_SPARQL_QUERY = """
SELECT ?spine ?setLabel ?tmdbId ?filmLabel
       (MIN(YEAR(?date)) AS ?year)
       (SAMPLE(?criterionId) AS ?criterionId) WHERE {
  ?set wdt:P12279 ?spine .
  ?set wdt:P527 ?film .
  OPTIONAL { ?film wdt:P4947 ?tmdbId . }
  OPTIONAL { ?film wdt:P9584 ?criterionId . }
  OPTIONAL { ?film wdt:P577 ?date . }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
GROUP BY ?spine ?set ?setLabel ?film ?filmLabel ?tmdbId
"""

CRITERION_CACHE_KEY = "fitzflix:criterion:releases"
CRITERION_CACHE_SECONDS = 7 * 86400


def _wikidata_sparql(url, query):
    """Run one SPARQL query against Wikidata, per its access guidelines."""

    contact = current_app.config["SERVER_EMAIL"] or "fitzflix"
    r = requests.get(
        url,
        params={"query": query},
        headers={
            "User-Agent": f"FitzflixBot/1.0 (mailto:{contact})",
            "Accept": "application/sparql-results+json",
            "Accept-Encoding": "gzip,deflate",
        },
        timeout=60,
    )
    r.raise_for_status()
    return r.json().get("results", {}).get("bindings", [])


def _parse_criterion_binding(binding):
    """A SPARQL result row as a release dict, or None if the spine is bad."""

    spine = binding.get("spine", {}).get("value", "")
    if not spine.isdigit():
        return None

    tmdb_id = binding.get("tmdbId", {}).get("value", "")
    title = (binding.get("filmLabel", {}).get("value") or "").strip()
    year = binding.get("year", {}).get("value", "")

    return {
        "spine_number": int(spine),
        "tmdb_id": int(tmdb_id) if tmdb_id.isdigit() else None,
        "title": title.upper(),
        "year": int(year) if year.isdigit() else None,
        "criterion_film_id": binding.get("criterionId", {}).get("value") or None,
        "set_title": None,
    }


def get_criterion_collection_from_wikidata(force_refresh=False):
    """Fetch Criterion Collection spine numbers from Wikidata.

    Access follows Wikidata's data-access guidelines: a descriptive
    User-Agent with a contact address, a single narrowly-scoped SPARQL
    query, and results cached in Redis for a week so per-import lookups
    never re-query the endpoint. The monthly scheduled refresh forces a
    fresh fetch.
    """

    url = current_app.config["WIKIDATA_SPARQL_URL"]
    if not url:
        return []

    if not force_refresh:
        cached = current_app.redis.get(CRITERION_CACHE_KEY)
        if cached:
            return json.loads(cached)

    criterion_collection = []
    for binding in _wikidata_sparql(url, CRITERION_SPARQL_QUERY):
        release = _parse_criterion_binding(binding)
        if release:
            criterion_collection.append(release)

    # Standalone releases come first, so a film that has both its own
    # release and a set membership keeps its own spine — the matching
    # lookups keep the first entry per film

    for binding in _wikidata_sparql(url, CRITERION_SETS_SPARQL_QUERY):
        release = _parse_criterion_binding(binding)
        if release:
            release["set_title"] = (
                binding.get("setLabel", {}).get("value") or ""
            ).strip() or None
            criterion_collection.append(release)

    current_app.redis.set(
        CRITERION_CACHE_KEY,
        json.dumps(criterion_collection),
        ex=CRITERION_CACHE_SECONDS,
    )
    current_app.logger.info(
        f"Fetched {len(criterion_collection)} Criterion Collection releases "
        f"from Wikidata"
    )
    return criterion_collection


def criterion_release_lookups(criterion_collection):
    """Index Criterion releases by TMDb id and by (title, year)."""

    by_tmdb_id = {}
    by_title_year = {}
    for release in criterion_collection:
        if release.get("tmdb_id"):
            by_tmdb_id.setdefault(release["tmdb_id"], release)
        if release.get("title") and release.get("year"):
            by_title_year.setdefault((release["title"], release["year"]), release)
    return by_tmdb_id, by_title_year


def assign_criterion_release(movie, by_tmdb_id, by_title_year):
    """Record a movie's Criterion spine number if a release matches.

    TMDb id matches are exact; title and year are the fallback for movies
    that haven't been matched to TMDb yet. Box-set members get their set's
    spine and title. Wikidata doesn't model in-print status, so existing
    values are kept and new matches get optimistic defaults; hand-curated
    set titles are never overwritten.
    """

    release = by_tmdb_id.get(movie.tmdb_id) if movie.tmdb_id else None
    if release is None and movie.title and movie.year:
        release = by_title_year.get((movie.title.upper(), movie.year))
    if release is None:
        return False

    movie.criterion_spine_number = release["spine_number"]
    if release.get("criterion_film_id"):
        movie.criterion_film_id = release["criterion_film_id"]
    if release.get("set_title") and movie.criterion_set_title == None:
        movie.criterion_set_title = release["set_title"]
    if movie.criterion_in_print == None:
        movie.criterion_in_print = True
    if movie.criterion_disc_owned == None:
        movie.criterion_disc_owned = False

    current_app.logger.info(
        f"{movie} Assigning Criterion Collection "
        f"spine #{movie.criterion_spine_number}"
    )
    return True


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

    s3 = aws_s3_client()
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


def refresh_criterion_collection_info(movie_id=None):
    """Refresh Criterion Collection information from Wikidata.

    Runs monthly on the 18th — Criterion announces each month's new titles
    around the 15th, and a few days leaves time for Wikidata to catch up.
    A full refresh forces a fresh fetch; single-movie refreshes use the
    week-long cache.
    """

    with app.app_context():
        try:

            # If the user specified a particular movie to be updated, update the
            # Criterion Collection info for just that one movie. Otherwise, update all.

            if movie_id:
                movies = Movie.query.filter_by(id=movie_id).all()

            else:
                movies = Movie.query.all()

            criterion_collection = get_criterion_collection_from_wikidata(
                force_refresh=movie_id is None
            )
            by_tmdb_id, by_title_year = criterion_release_lookups(criterion_collection)

            matched = 0
            for movie in movies:
                if assign_criterion_release(movie, by_tmdb_id, by_title_year):
                    matched += 1

            db.session.commit()
            current_app.logger.info(
                f"Matched {matched} of {len(movies)} movie(s) against "
                f"{len(criterion_collection)} Criterion Collection releases"
            )

        except Exception:
            current_app.logger.error(traceback.format_exc())
            db.session.rollback()

        else:
            return True


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
                        current_app.sql_scheduler.enqueue_in(
                            timedelta(minutes=sleep_duration),
                            "app.videos.apply_tmdb_refresh",
                            library=library,
                            id=id,
                            tmdb_id=tmdb_id,
                            tmdb_payload=tmdb_payload,
                            notify_if_missing=notify_if_missing,
                            timeout=current_app.config["SQL_TASK_TIMEOUT"],
                            job_id=safe_job_id(
                                f"retry:apply_tmdb_refresh:{library}:{id}"
                            ),
                            job_result_ttl=86400,
                            job_description=(
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


def lossless_to_flac(file_path, file_id=None):
    """Convert any lossless tracks to FLAC if the file isn't from physical media."""

    # If the file was from physical media, it should already have a FLAC
    # version of any lossless tracks included because we would have ripped it
    # using the FLAC Plus Original Audio.mmcp.xml MakeMKV profile. (We kept
    # the original format around, just in case.)

    with app.app_context():
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
                    current_app.logger.info(f"'{basename}' Duration: {file_duration}s")

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
                    previous_percent = None
                    for line in flac_track_process.stdout:
                        progress_match = re.search(
                            r"time\=(?P<hour>\d{2})\:(?P<minute>\d{2}):(?P<seconds>\d{2})",
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
                        if previous_percent != progress:
                            current_app.logger.info(
                                f"'{basename}' Converting lossless tracks to FLAC: {progress}%"
                            )
                            previous_percent = progress
                        if job:
                            job.meta["description"] = (
                                f"'{basename}' — Converting lossless tracks to FLAC"
                            )
                            job.meta["progress"] = progress
                            job.save_meta()

                    wait_for_subprocess(flac_track_process)

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
                    current_app.logger.warning(
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
