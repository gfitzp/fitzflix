"""The shared task plumbing and the compatibility shim.

This module keeps the machinery that every pipeline module uses. That
is the file copy with progress and retries after transient errors, the
acquire-or-defer sequence for the title lock, the subprocess
supervision, and the dead-volume probe.

All the other names are re-exports from the modules that the strangler
split created (aws_storage, criterion_catalog, diary, tracks,
importing, tmdb_refresh). The rq job names are strings stored in Redis,
in cron tables, and in the pipeline trail registry. Thus,
"app.videos.X" must resolve forever. New code must import from the
real homes. The shim exists for the strings and the history.
"""

import errno
import os
import random
import subprocess
import time

from datetime import timedelta

from flask import current_app
from werkzeug.local import LocalProxy

from app import get_app, safe_job_id
from app.maintenance import VOLUMES_ROOT, volume_alive

# The AWS storage layer moved to app.aws_storage. These re-exports keep
# every stored rq job string and import site resolving through
# app.videos. The callers of this module continue to work unchanged.

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
    rearchive_untouched_object,
    rename_untouched_object,
    sanitize_s3_key,
    sqs_retrieve_task,
    sync_aws_s3_storage_task,
    untouched_key_still_claimed,
    upload_task,
)

# The Criterion spine catalog moved to app.criterion_catalog. The diary
# writers moved to app.diary. The re-export contract is the same as above.

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
    _normalize_title,
    _pick_tmdb_match,
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
from app.importing import (
    COMPLETENESS_QUIET_SECONDS,
    MAX_COMPLETENESS_RETRIES,
    SELF_SIZING_FORMATS,
    convert_to_matroska,
    evaluate_filename,
    finalize_localization,
    finalize_transcoding,
    inspect_localized_file,
    iso_639_3_native_language,
    localization_task,
    manual_import_task,
    move_localized_file,
    move_to_rejects,
    probe_file_completeness,
    reconstruct_filename,
    sanitize_string,
    transcode_task,
)
from app.tmdb_refresh import (
    apply_tmdb_refresh,
    find_or_create_tmdb_movie,
    refresh_tmdb_info,
)
from app.tracks import (
    _extract_media_details,
    build_supplement_args,
    extract_track_metadata,
    flag_possibly_forced_subtitles,
    get_audio_tracks_from_file,
    get_subtitle_tracks_from_file,
    iso_639_2_languages,
    language_names,
    library_language_choices,
    mkvmerge_task,
    mkvmerge_unlocked,
    mkvpropedit_task,
    mkvpropedit_unlocked,
    parse_dolby_vision_profile,
    plan_audio_supplements,
    record_filesize,
    remove_empty_subtitle_tracks,
    remux_audio_plan_task,
    resolve_language_code,
    save_track_metadata,
    supplement_lossless_tracks,
    track_metadata_scan,
    track_metadata_scan_library,
    track_metadata_scan_task,
    watch_mkvmerge_progress,
)

# __all__ marks the re-exports as public on purpose. The rq job strings
# and the import sites resolve them through app.videos. It also tells
# pyflakes that they are used.

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
    "rearchive_untouched_object",
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
    "_normalize_title",
    "_pick_tmdb_match",
    "apply_letterboxd_import",
    "apply_plex_watch",
    "clear_not_interested",
    "clear_watchlist",
    "letterboxd_import_task",
    "parse_letterboxd_export",
    "plex_history_poll",
    "review_task",
    "star_rating_fields",
    "_extract_media_details",
    "build_supplement_args",
    "extract_track_metadata",
    "flag_possibly_forced_subtitles",
    "get_audio_tracks_from_file",
    "get_subtitle_tracks_from_file",
    "iso_639_2_languages",
    "language_names",
    "library_language_choices",
    "mkvmerge_task",
    "mkvmerge_unlocked",
    "mkvpropedit_task",
    "mkvpropedit_unlocked",
    "parse_dolby_vision_profile",
    "plan_audio_supplements",
    "record_filesize",
    "remove_empty_subtitle_tracks",
    "remux_audio_plan_task",
    "resolve_language_code",
    "save_track_metadata",
    "supplement_lossless_tracks",
    "track_metadata_scan",
    "track_metadata_scan_library",
    "track_metadata_scan_task",
    "watch_mkvmerge_progress",
    "COMPLETENESS_QUIET_SECONDS",
    "MAX_COMPLETENESS_RETRIES",
    "SELF_SIZING_FORMATS",
    "convert_to_matroska",
    "evaluate_filename",
    "finalize_localization",
    "finalize_transcoding",
    "inspect_localized_file",
    "iso_639_3_native_language",
    "localization_task",
    "manual_import_task",
    "move_localized_file",
    "move_to_rejects",
    "probe_file_completeness",
    "reconstruct_filename",
    "sanitize_string",
    "transcode_task",
    "apply_tmdb_refresh",
    "find_or_create_tmdb_movie",
    "refresh_tmdb_info",
]


# These OSError numbers show a dropped or unstable network mount, not a
# problem with the file itself. For example, macOS smbfs revokes the open
# file handles of a copy with EBADF when their SMB session resets during
# the operation. These errors get a retry, never an immediate reject.

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


def copy_with_progress(src, dst, job, name, activity="Copying to library"):
    """Copy a file in chunks and report the progress like the external tools."""

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

    # At this point, the copy has read and written every byte. Thus, a
    # source that fails its own close() tells nothing more about the copy.
    # An SMB server that lost its handle for a file answers close() with
    # EBADF every time. At the same time, it serves reads correctly. If
    # Fitzflix discards a byte-complete copy for that error, it repeats the
    # full transfer while the share stays in that state.

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
    """Rename a file with retries.

    A busy SMB volume can return false errors for a short time. This
    includes ENOENT for names that exist. Thus, a transient failure gets a
    new try before the error propagates.
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
    """Return the /Volumes mount roots in paths that do not respond."""

    mounts = set()
    for path in paths:
        if path and path.startswith(VOLUMES_ROOT + os.sep):
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

    This function returns the lock on success. It returns None after it
    schedules the retry. The retry goes into the native
    ScheduledJobRegistry of the queue. The mover in scheduler.py enqueues
    the retry when it is due.
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

    # The deterministic id makes a repeat deferral replace the pending
    # retry. It does not add a new one. The result ttl of 1 day keeps the
    # record of a finished retry alive. Thus, a retry that it scheduled
    # itself can still find that record.

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
    """Wait for an external tool to stop, and raise if it exited with an error.

    This function accepts a subprocess.Popen or a subprocess.CompletedProcess.
    The mkvtoolnix tools (mkvmerge, mkvpropedit) exit with 1 for warnings and
    with 2 for errors. Thus, the callers for those tools must pass
    ok_returncodes=(0, 1).
    """

    if hasattr(process, "wait"):
        process.wait()
    if process.returncode not in ok_returncodes:
        raise subprocess.CalledProcessError(process.returncode, process.args)


# The app instance of this process. Fitzflix resolves it lazily. Thus, a
# process that already has an application can import this module without
# a second application.

app = LocalProxy(get_app)
