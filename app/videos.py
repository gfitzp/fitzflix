"""The shared task plumbing — and the compatibility shim.

What physically remains here: the cross-cutting machinery every
pipeline module leans on — file-copy with progress and transient-error
retries, the title lock acquire-or-defer dance, subprocess supervision,
and the dead-volume probe.

Everything else re-exports from the modules the strangler split carved
out (aws_storage, criterion_catalog, diary, tracks, importing,
tmdb_refresh): rq job names are strings stored in Redis, cron tables,
and the pipeline trail registry, so "app.videos.X" must keep resolving
forever. New code should import from the real homes; the shim exists
for the strings and the history.
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

# The AWS storage layer moved to app.aws_storage; these re-exports
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
# diary writers to app.diary; same re-export contract as above

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

    Returns the lock on success, or None after scheduling the retry.
    The retry lands in the queue's native ScheduledJobRegistry;
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


# This process's app instance, resolved lazily so importing this module from
# a process that already has an application doesn't build a second one

app = LocalProxy(get_app)
