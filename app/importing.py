"""The import pipeline (the strangler split from app.videos).

This is the chain that a file follows from the import directory to the
library. evaluate_filename parses and prices the name.
localization_task archives the untouched original and removes the
non-native tracks. inspect, move, and finalize carry the localized
output to its library home and catalog it. A conversion to SDR runs
between them (app.sdr_conversion). manual_import_task is the hourly
sweep. The rejects door, the language functions, and the
filename sanitizers are also here.

app.videos re-exports every name in this module. Thus, the stored rq
job strings ("app.videos.localization_task") and the import sites
continue to resolve. The shared lock, retry, and copy functions stay in
app.videos. This module imports them lazily inside functions. Thus, the
module import direction stays one-way. This module imports the track
and S3 layers directly. They never import back.
"""

import errno
import json
import os
import re
import shutil
import signal
import subprocess
import time
import traceback
import uuid

from datetime import datetime, timedelta, timezone
from functools import partial

from pathvalidate import sanitize_filename
from pymediainfo import MediaInfo
from rq import get_current_job
from rq.exceptions import NoSuchJobError
from rq.job import Job
from rq.timeouts import BaseTimeoutException
from rq.registry import (
    FinishedJobRegistry,
    ScheduledJobRegistry,
    StartedJobRegistry,
)
from unidecode import unidecode

from flask import current_app, render_template
from werkzeug.local import LocalProxy

from app.custom_artwork import remove_episode_thumb_of
from app.tasks import (
    delete_job,
    enqueue_task,
    report_progress,
    requeue,
    requeue_job_id,
)
from app import (
    db,
    get_app,
    importable_basename,
    makedirs_on_mounted_volume,
    path_tag,
    retry_job_id,
    safe_job_id,
)
from app.aws_storage import (
    clear_archive_target,
    discard_pending_archive,
    ensure_pending_upload,
    is_pending_archive,
    pending_archive_key,
    pending_archive_of,
    request_archive_move,
    untouched_key_still_claimed,
    upload_untouched,
)
from app.settings import get_setting
from app.criterion_catalog import (
    assign_criterion_release,
    criterion_release_lookups,
    stored_criterion_releases,
)
from app.email import admin_email, notify_admin
from app.email import send_email
from app.naming import hidden, library_name, with_extension
from app.models import (
    MOVIES,
    TV_SHOWS,
    ArchivedOriginal,
    File,
    HeldImport,
    Movie,
    RefFeatureType,
    RefQuality,
    TVSeries,
    title_lock_key,
)
from app.tmdb_apply import tmdb_json
from app.pipeline import migrate_trail, record_task_stage
from app.sdr_conversion import (
    archived_original_from_file,
    converted_file_details,
    held_decision,
    hold_import,
    needs_placebo,
    plan_conversion,
)
from app.file_ranking import find_better_files, find_worse_files, real_rips_blocking
from app.tracks import (
    _extract_media_details,
    file_language_spelling,
    same_language,
    replace_track_rows,
    get_audio_tracks_from_file,
    get_subtitle_tracks_from_file,
    record_filesize,
    remove_empty_subtitle_tracks,
    stop_process,
    supplement_lossless_tracks,
    watch_mkvmerge_progress,
)

# An import waits for free space on the library volume. It is never
# rejected for space (#262). The check asks for the size of the source
# times SPACE_FACTOR, plus SPACE_RESERVE. The output can be larger than
# the source, because lossless tracks get FLAC twins. The final copy
# asks for the size of the output plus SPACE_RESERVE. A waiting file
# tries again every SPACE_WAIT, with no limit.

SPACE_FACTOR = 1.2
SPACE_RESERVE = 2 * 1024**3
SPACE_WAIT = timedelta(minutes=30)

# The errors of a full volume or a full quota. A copy that stops with
# one of them waits for space. It is not an import failure.

SPACE_ERRNOS = {errno.ENOSPC, errno.EDQUOT}

# A file that waits for space has a marker for the length of 1 wait,
# plus a wide margin. The hourly sweep and the watcher skip a marked
# file. Without it, each sweep started 1 more wait chain for the same
# file. If a chain dies, its marker expires and the sweep takes the file
# up. The marker also protects the pending upload of the import from
# the S3 prune (#455). A retry that waits on a busy queue must not let
# it lapse. Thus, the margin is hours, and each retry renews it.

SPACE_WAIT_KEY = "fitzflix:import:space-wait:{}"
SPACE_WAIT_MARK = max(SPACE_WAIT + timedelta(minutes=30), timedelta(hours=3))

# Refresh the title lock while a copy waits. The token must match. A
# lock that expired is taken again. A lock that another task holds is
# left alone. Returns 1, 2, or 0 for those 3 cases.

EXTEND_LOCK_SCRIPT = """
local held = redis.call('GET', KEYS[1])
if held == ARGV[1] then
    redis.call('PEXPIRE', KEYS[1], ARGV[2])
    return 1
elseif not held then
    redis.call('SET', KEYS[1], ARGV[1], 'PX', ARGV[2])
    return 2
end
return 0
"""


def mark_space_wait(basename, aws_key=None):
    """Mark a source file as waiting for space, for 1 wait and a margin.

    aws_key is the run-scoped upload of the import (#430), if it has one.
    No record claims that key until finalize. The weekly S3 sync reads
    the key from the marker and keeps the upload (#455)."""

    current_app.redis.set(
        SPACE_WAIT_KEY.format(basename),
        aws_key or "1",
        ex=int(SPACE_WAIT_MARK.total_seconds()),
    )


def renew_space_wait(basename):
    """Renew the space-wait marker of a source file, if it has one (#475).

    A wait chain can also defer for the title lock or for a dead volume.
    Each of those retries renews the marker. Thus, a chain that waits
    longer than SPACE_WAIT_MARK on those paths keeps its marker."""

    current_app.redis.expire(
        SPACE_WAIT_KEY.format(basename), int(SPACE_WAIT_MARK.total_seconds())
    )


def clear_space_wait(basename):
    """Remove the space-wait marker of a source file."""

    current_app.redis.delete(SPACE_WAIT_KEY.format(basename))


def space_wait_pending(redis, basename):
    """Return True if a wait chain for space holds this source file."""

    return bool(redis.exists(SPACE_WAIT_KEY.format(basename)))


def localization_pending(queue, path):
    """Return True if a localization of this file is waiting or running (#287).

    The fixed job id of the file can be in the queue list or in the
    StartedJobRegistry. It can also be in the intermediate queue, which
    is the gap between the list and the worker. A retry of the file has
    its own id. It can be scheduled, queued, or running. The fixed id
    with a time tag counts as the fixed id (#371)."""

    basename = os.path.basename(path)
    job_id = safe_job_id(basename)

    # A retry id from requeue starts with the name of the task and the
    # digest of the path. The counters follow.

    retry_id = requeue_job_id("app.videos.localization_task", path)

    # A wait for the title lock has its own id (schedule_retry in
    # app.videos). It has the basename and no counters.

    lock_retry_id = safe_job_id(f"retry:localization_task:'{basename}'")

    # The retries that were scheduled before requeue have the basename
    # and the counters. With no counters, retry_job_id gives the prefix
    # of each of them.

    legacy_prefix = retry_job_id("localization_task", f"'{basename}'")
    ids = set(queue.job_ids)
    ids.update(queue.intermediate_queue.get_job_ids())
    ids.update(StartedJobRegistry(queue=queue).get_job_ids())
    ids.update(ScheduledJobRegistry(queue=queue).get_job_ids())
    tagged = re.compile(re.escape(job_id) + r"_\d{10}")
    return any(
        i in (job_id, lock_retry_id, retry_id)
        or i.startswith(retry_id + "_")
        or i.startswith(legacy_prefix)
        or tagged.fullmatch(i)
        for i in ids
    )


# The stages after the localization. Each one has the source path as its
# first argument. The source stays in the import directory until the
# cataloging removes it. A conversion (#292) keeps it there for hours.

LATER_STAGES = {
    "app.videos.convert_localized_file": "transcode_queue",
    "app.videos.move_localized_file": "handoff_queue",
    "app.videos.finalize_localization": "sql_queue",
}


def _later_stage_jobs():
    """Yield each conversion, move and cataloging job.

    A job counts when it is queued, running, or scheduled for a retry.
    A job in the dequeue gap (the intermediate queue of rq) counts too
    (#523)."""

    for queue_attr in set(LATER_STAGES.values()):
        queue = getattr(current_app, queue_attr)
        ids = set(queue.job_ids)
        ids.update(queue.intermediate_queue.get_job_ids())
        ids.update(StartedJobRegistry(queue=queue).get_job_ids())
        ids.update(ScheduledJobRegistry(queue=queue).get_job_ids())
        for job in Job.fetch_many(list(ids), connection=current_app.redis):
            if job is None or job.func_name not in LATER_STAGES:
                continue
            yield job


def later_stage_holds(path):
    """Return True if a conversion, move, or cataloging job still holds this source (#296).

    The job can be queued, running, or scheduled for a retry."""

    for job in _later_stage_jobs():
        source = job.args[0] if job.args else job.kwargs.get("source_path")
        if source == path:
            return True
    return False


def paths_held_by_later_stages():
    """Return each path argument of the conversion, move and cataloging jobs (#436).

    A job counts when it is queued, running, or scheduled for a retry.
    The weekly orphan sweep must not delete the input of such a job. A
    conversion can wait in the transcode queue for more than 7 days."""

    held = set()
    for job in _later_stage_jobs():
        for value in list(job.args) + list((job.kwargs or {}).values()):
            if isinstance(value, str):
                held.add(os.path.abspath(value))
    return held


def keys_held_by_later_stages():
    """Return the archive key of each conversion, move and cataloging job (#493).

    The key is the aws_key of the file_details argument. The import
    gives the key to its File row only at the cataloging. A conversion
    or a move that waits for a retry can wait for more than 8 days. The
    weekly prune must not delete its upload."""

    held = set()
    for job in _later_stage_jobs():
        for value in list(job.args) + list((job.kwargs or {}).values()):
            if isinstance(value, dict) and isinstance(value.get("aws_key"), str):
                held.add(value["aws_key"])
    return held


def _series_title_of_name(basename):
    """Return the title of the series record that an episode name names, or None.

    This is the title rule of the parse, with no TMDB call. An id tag is
    removed. A series record that owns the id of the tag gives its title,
    as in _parse_tv (#523). Thus, "Doctor Who {tmdb-57243}" gives "Doctor
    Who (2005)". A name that is not an episode name gives None."""

    if not TV_NAME_SEARCH_RE.search(basename):
        return None
    tv = TV_NAME_RE.match(basename)
    if not tv:
        return None
    title = tv.group("title")
    tagged = re.fullmatch(rf"(?P<base>.+?)(?P<tags>(?: {ID_TAG_BLOCK})+)", title)
    if tagged:
        title = tagged.group("base")
        tag = preferred_id_tag(parse_name_tags(tagged.group("tags"))[0])
        owner = _series_record_of_tag(tag) if tag else None
        if owner is not None:
            title = owner.title
    return _canonical_series_title(title)


# The Sonarr adopt task comes before the localization (#289). It waits
# for the Sonarr rename of the file and then queues the localization.
# Its third argument is the path of the download (#373).

ADOPT_EPISODE_TASK = "app.sonarr_push.adopt_episode_task"


def series_import_in_flight(titles):
    """Return the source path of an import of a series that is not done, or None.

    titles holds the series titles that count. The localization can be
    queued, running, or scheduled for a retry. A later stage counts too,
    and the Sonarr adopt task before the localization (#373). A series
    rename waits for such an import (#370). The import attaches the
    episode to the series by its title at the end."""

    names = {
        "app.videos.localization_task": "import_queue",
        ADOPT_EPISODE_TASK: "request_queue",
        **LATER_STAGES,
    }
    for queue_attr in set(names.values()):
        queue = getattr(current_app, queue_attr)
        ids = set(queue.job_ids)
        ids.update(queue.intermediate_queue.get_job_ids())
        ids.update(StartedJobRegistry(queue=queue).get_job_ids())
        ids.update(ScheduledJobRegistry(queue=queue).get_job_ids())
        for job in Job.fetch_many(list(ids), connection=current_app.redis):
            if job is None or job.func_name not in names:
                continue
            if job.func_name == ADOPT_EPISODE_TASK:
                source = job.args[2] if len(job.args) > 2 else None
            else:
                source = job.args[0] if job.args else job.kwargs.get("source_path")
            if not isinstance(source, str):
                continue
            title = _series_title_of_name(os.path.basename(source))
            if title is not None and title in titles:
                return source
    return None


def enqueue_localization(path, at_front=False, quiet=False):
    """Queue the localization of a file once. Return the job, or None (#287).

    The function does nothing if a localization of the same file waits or
    runs, or if a space wait holds it. Otherwise it deletes the finished
    job of the fixed job id. A removal from its registry is not enough.
    The job hash keeps its expiry, and the new job under the id takes it
    and can expire in the queue (#371). A failed job stays in the failed
    jobs for the sweep. Then the new job gets the id with a time tag.
    With quiet, a skip writes no log line. The import watcher uses it,
    because it sees many events for 1 file."""

    queue = current_app.import_queue
    basename = os.path.basename(path)
    if (
        localization_pending(queue, path)
        or space_wait_pending(current_app.redis, basename)
        or later_stage_holds(path)
    ):
        if not quiet:
            current_app.logger.info(f"'{basename}' Already waiting for import")
        return None

    job_id = safe_job_id(basename)
    try:
        old = Job.fetch(job_id, connection=queue.connection)
    except NoSuchJobError:
        old = None
    if old is None:
        FinishedJobRegistry(queue=queue).remove(job_id)
    elif old.is_failed:
        job_id = f"{job_id}_{int(time.time())}"
    else:
        delete_job(old)
    return enqueue_task(
        "app.videos.localization_task",
        path,
        description=f"'{basename}'",
        at_front=at_front,
        job_id=job_id,
    )


def already_cataloged(file_path):
    """Return True if this file is already the library file at its path (#287).

    An in-place import (Radarr, Sonarr) reads the file at its library
    path. A File row at the same path with the same size is that file
    after its import. A second job or a late retry must not import it
    again."""

    relative = os.path.relpath(file_path, current_app.config["LIBRARY_DIR"])
    if relative.startswith(".."):
        return False
    file = File.query.filter_by(file_path=relative).first()
    return file is not None and file.filesize_bytes == os.path.getsize(file_path)


def lock_key_for_details(file_details):
    """Return the title lock key of the parsed details of an import file.

    This is the filename side of title_lock_key. File.file_identifier is
    the database side. tests/test_locks.py pins their agreement."""

    return title_lock_key(
        file_details.get("media_library"),
        file_details.get("title"),
        year=file_details.get("year"),
        feature_type=file_details.get("feature_type_name"),
        plex_title=file_details.get("plex_title"),
        season=file_details.get("season"),
        episode=file_details.get("episode"),
        edition=file_details.get("edition"),
    )


def _hold_title_lock(lock, basename):
    """Keep the title lock of a waiting copy alive for 1 more lock TTL.

    A copy can wait for space for longer than the TTL of 1 day. Without
    this, the lock expired, and a second import of the same title could
    start and write to the same staging file.

    Return False if another task holds the lock (#339). The caller must
    then stop and try again later. It must not write the files or the
    records of the title. Return True if this task holds the lock, or if
    the task has no lock."""

    if not lock:
        return True
    resource, token = lock[1], lock[2]
    ttl_ms = current_app.config["LOCALIZATION_TASK_TIMEOUT"] * 1000
    result = current_app.redis.eval(EXTEND_LOCK_SCRIPT, 1, resource, token, ttl_ms)
    if result == 2:
        current_app.logger.warning(
            f"'{basename}' The title lock had expired during the wait. Took it again"
        )
    elif result == 0:
        current_app.logger.warning(
            f"'{basename}' Another task holds the title lock during the wait"
        )
        return False
    return True


# The time that a stage waits when another task holds its title lock (#339)

TITLE_LOCK_WAIT = timedelta(minutes=30)


def defer_for_title_lock(func, args, counters, source_path, basename):
    """Run a stage again after TITLE_LOCK_WAIT, with the same lock (#339).

    The lock of the stage expired, and another task took it. The retry
    keeps the old lock token. When the other task releases the lock, the
    extend script of _hold_title_lock takes it again with that token.
    counters holds every attempt counter of the stage. lock_waits counts
    the waits. The queue and the timeout come from the task registry."""

    counters = {**counters, "lock_waits": int(counters.get("lock_waits") or 0) + 1}
    requeue(
        func,
        key=source_path,
        delay=TITLE_LOCK_WAIT,
        reason=f"Another task holds the title lock (wait {counters['lock_waits']})",
        counters=counters,
        args=args,
        description=f"'{basename}'",
    )


def _existing_directory(path):
    """Return path, or its nearest parent directory that exists."""

    while not os.path.isdir(path) and path != os.path.dirname(path):
        path = os.path.dirname(path)
    return path


def _free_bytes(path):
    """Return the free bytes of the volume that holds path."""

    return shutil.disk_usage(_existing_directory(path)).free


def _crosses_volumes(file_path, directory):
    """Return True if file_path and directory are on different volumes.

    The directory can be a new folder that does not exist yet. Then its
    nearest existing parent gives the volume."""

    return (
        os.stat(os.path.dirname(file_path)).st_dev
        != os.stat(_existing_directory(directory)).st_dev
    )


def library_space_shortfall(directory, needed_bytes):
    """Return the bytes that the volume of directory lacks, or 0.

    The directory can be a new folder that does not exist yet. Then its
    nearest existing parent gives the volume. A volume that cannot be
    read gives 0. Then the copy itself finds the problem."""

    try:
        free = _free_bytes(directory)
    except OSError:
        return 0
    return max(0, needed_bytes - free)


# Staging reservations (#294). Up to 5 workers localize at the same
# time. Each staged import records its peak need on staging and the
# name stems of its staging files. A new import counts what the others
# still need: their reservation minus the bytes that their files already
# use. A reservation with no files on staging expires after the grace
# period. Thus, a finished or failed import releases its space.

STAGING_RESERVED_KEY = "fitzflix:staging:reserved"
STAGING_RESERVE_LOCK = "fitzflix:staging:reserve-lock"
STAGING_FACTOR = 2.5
CONVERSION_STAGING_FACTOR = 1.8
STAGING_MARGIN = 20 * 1024**3
STAGING_GRACE_SECONDS = 600


def _name_stem(name):
    """Return a file name without its extension. A staging file name starts with it."""

    return os.path.splitext(name)[0]


def staged_output_path(staging_dir, source_basename, library_basename):
    """Return the staging path of the localized output of an import.

    The name has the stem of the source file. The import directory holds
    each source name only once. The extension is the extension of the
    library file."""

    extension = os.path.splitext(library_basename or "")[1] or ".mkv"
    return hidden(
        os.path.join(staging_dir, _name_stem(source_basename)), f".localized{extension}"
    )


def _staging_bytes(staging_dir, stems):
    """Return the bytes of the staging files whose names start with one of the stems."""

    total = 0
    try:
        entries = list(os.scandir(staging_dir))
    except OSError:
        return 0
    for entry in entries:
        name = entry.name.lstrip(".")
        if any(name.startswith(stem) for stem in stems):
            try:
                total += entry.stat().st_size
            except OSError:
                pass
    return total


def staging_outstanding(staging_dir, exclude=None):
    """Return the bytes that the other staging reservations still need.

    This also removes the reservations that have had no files on
    staging for longer than STAGING_GRACE_SECONDS."""

    redis = current_app.redis
    total = 0
    for field, raw in redis.hgetall(STAGING_RESERVED_KEY).items():
        name = field.decode() if isinstance(field, bytes) else field
        if name == exclude:
            continue
        try:
            entry = json.loads(raw)
        except ValueError:
            redis.hdel(STAGING_RESERVED_KEY, name)
            continue
        used = _staging_bytes(staging_dir, entry.get("stems", []))
        if used == 0 and time.time() - entry.get("at", 0) > STAGING_GRACE_SECONDS:
            redis.hdel(STAGING_RESERVED_KEY, name)
            continue
        total += max(0, entry.get("bytes", 0) - used)
    return total


def set_staging_reservation(basename, needed_bytes, names):
    """Record or change the staging reservation of an import (#294)."""

    current_app.redis.hset(
        STAGING_RESERVED_KEY,
        basename,
        json.dumps(
            {
                "bytes": int(needed_bytes),
                "stems": sorted({_name_stem(name) for name in names}),
                "at": time.time(),
            }
        ),
    )


def release_staging_reservation(basename):
    """Remove the staging reservation of an import."""

    current_app.redis.hdel(STAGING_RESERVED_KEY, basename)


def _staging_usage(staging_dir):
    """Return (total, free) bytes of the staging volume."""

    usage = shutil.disk_usage(staging_dir)
    return usage.total, usage.free


# The delay before an import tries the staging reservation lock again.

STAGING_LOCK_RETRY = timedelta(minutes=1)


def reserve_staging(basename, needed_bytes, names, staging_dir):
    """Reserve staging space for an import. Return "ok", "wait", "lock", or "too big".

    "lock" means that the reservation lock stayed busy. Nothing about
    the space is known then.

    "too big" means that the file cannot fit on the staging volume even
    with nothing else there. A short Redis lock makes the check and the
    reservation one step. Thus, 2 imports cannot take the same space."""

    try:
        total, free = _staging_usage(staging_dir)
    except OSError:
        return "too big"
    if needed_bytes > total - STAGING_MARGIN:
        return "too big"

    lock = None
    for _ in range(20):
        lock = current_app.lock_manager.lock(STAGING_RESERVE_LOCK, 30000)
        if lock:
            break
        time.sleep(0.5)

    # Without the lock, the check and the reservation are not 1 step.
    # Then 2 imports can take the same space (#478). The import tries
    # again soon. It is not a wait for space.

    if not lock:
        return "lock"
    try:
        total, free = _staging_usage(staging_dir)
        available = (
            free - staging_outstanding(staging_dir, exclude=basename) - STAGING_MARGIN
        )
        if needed_bytes > available:
            return "wait"
        set_staging_reservation(basename, needed_bytes, names)
        return "ok"
    finally:
        current_app.lock_manager.unlock(lock)


def _gigabytes(size):
    """Return a byte count as a short GB text."""

    return f"{size / 1024**3:.1f} GB"


def _log_space_wait(basename, directory, needed, shortfall, waits):
    """Log a wait for space. The first wait is a warning, the others are info."""

    message = (
        f"'{basename}' Needs {_gigabytes(needed)} on the volume of "
        f"'{directory}', which lacks {_gigabytes(shortfall)}. Trying again "
        f"in {int(SPACE_WAIT.total_seconds() // 60)} minutes"
    )
    if waits == 0:
        current_app.logger.warning(message + " (waiting for free space)")
    else:
        current_app.logger.info(message + f" (wait {waits + 1})")


def convert_to_matroska(file_path, output_file, job, name):
    """Remux a non-Matroska file into a Matroska container.

    Return True on success. A failure means a format that mkvmerge cannot
    carry. On failure, this function removes the partial output and
    returns False. Then the caller can import the file as it is.
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


# The import completeness gate. The first value is the number of seconds
# since the last modification. After that time, Fitzflix trusts a file
# (if the probe cannot read its container). The second value is the
# number of 1-minute checks a file gets before Fitzflix imports it in all
# cases.

COMPLETENESS_QUIET_SECONDS = 120


MAX_COMPLETENESS_RETRIES = 30


# The number of seconds between the 2 size readings of an incoming file.
# A file that grows in this time is still being copied. The test suite
# sets a shorter time.

GROWTH_CHECK_SECONDS = 5


# Containers that declare their own length. Thus, MediaInfo can prove
# that a stalled partial copy is truncated, not complete.

SELF_SIZING_FORMATS = {"Matroska", "MPEG-4"}


def probe_file_completeness(file_path):
    """Ask the container if the file is structurally complete.

    A Matroska file declares its segment size. An MP4 file indexes itself
    in a trailing moov atom. Thus, MediaInfo reports truncation for a
    partial copy of either. The time that the copy has stalled is not
    important. Return True if such a container looks complete. Return
    False if it reports truncation. Return None for a file that the probe
    cannot read (an unidentifiable file, or a format with no declared
    length).
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


class _NotedStrays(list):
    """The work files of a run. Each new one goes into the note of the run."""

    def __init__(self, run):
        super().__init__()
        self.run = run

    def append(self, path):
        """Add a work file, and note the run again."""

        super().append(path)
        self.run.note()


class _LocalizationRun:
    """Hold the state that the phases of 1 localization run share.

    localization_task owns the one exception handler of the run. That
    handler needs the title lock and the work files of the run. Thus,
    those 2 values live here. A phase that changes them says so."""

    def __init__(self, source_path, counters):
        self.source_path = source_path
        self.basename = os.path.basename(source_path)
        self.counters = counters
        self.job = None
        self.lock = None
        self.strays = _NotedStrays(self)
        self.pending = None
        # An in-place import rewrites the source itself (#424). A stop
        # signal during that rewrite must not leave it for a re-import.
        self.in_place = False

    def note(self):
        """Keep the lock, the pending upload and the strays in Redis (#498).

        A worker that dies takes them with it. The worker that reclaims
        the dead job reads them here. This is best effort."""

        from app.worker_recovery import note_job_state

        job_id = getattr(self.job, "id", None)
        if job_id is None:
            return
        try:
            note_job_state(
                job_id,
                current_app.config["LOCALIZATION_TASK_TIMEOUT"],
                lock=self.lock,
                pending=self.pending,
                strays=list(self.strays),
            )
        except Exception:
            current_app.logger.warning(traceback.format_exc())

    def retry(self, delay, reason, level="warning", **changes):
        """Book the task again after delay. Every retry carries all 4 counters.

        Each retry changes only its own counter. Return "retry"."""

        requeue(
            "app.videos.localization_task",
            key=self.source_path,
            delay=delay,
            reason=reason,
            counters={**self.counters, **changes},
            kwargs={"file_path": self.source_path},
            description=f"'{self.basename}'",
            level=level,
        )
        return "retry"

    def release_lock(self):
        """Release the title lock of the run and log it."""

        current_app.lock_manager.unlock(self.lock)
        current_app.logger.info(f"Removed lock {self.lock}")


def localization_task(
    file_path,
    transient_retries=0,
    completeness_retries=0,
    space_waits=0,
    tmdb_retries=0,
):
    """Archive an untouched file and remove the unnecessary language tracks.

    - This task uploads the untouched file to AWS S3 storage as a backup.
    - It localizes the file. It keeps all native-language audio and subtitle
      tracks. It also keeps the first audio track if that track is not in
      the native language.
    - It passes the localized file to a separate process. That process adds
      the file to the database.
    - It waits while the library volume has too little free space.
      space_waits counts the waits.

    The phases run in this order: _admission_gate, _identify,
    _take_title_lock, _quality_and_space_gate, _stage_source,
    _archive_original, _localize, and _hand_off. A phase that stops the
    run returns a verdict. A failure in any phase before the hand-off
    goes to the one handler here, which calls abandon_import.
    """

    with app.app_context():
        run = _LocalizationRun(
            file_path,
            {
                "transient_retries": transient_retries,
                "completeness_retries": completeness_retries,
                "space_waits": space_waits,
                "tmdb_retries": tmdb_retries,
            },
        )

        try:
            run.job = get_current_job()
            file_details = None

            verdict = _admission_gate(file_path, run)
            if verdict != "ok":
                return verdict == "retry"

            identified = _identify(file_path, run)
            if isinstance(identified, str):
                return identified == "retry"
            file_details, conversion = identified

            run.lock = _take_title_lock(file_details, run)
            if not run.lock:
                return False

            verdict = _quality_and_space_gate(file_path, file_details, conversion, run)
            if verdict != "ok":
                return verdict == "wait"

            # Save the untouched filename. Fitzflix can need it to recreate
            # the file.

            file_details["untouched_basename"] = run.basename

            staging = _stage_source(file_path, run)
            if staging is None:
                return True
            work_path, staged = staging

            _archive_original(work_path, file_details)
            run.pending = pending_archive_of(file_details)
            run.note()

            # The untouched upload does not change the source. Only the
            # localization of an in-place import rewrites it (#497).
            run.in_place = not staged
            work_path, hidden_output_file = _localize(
                file_path, work_path, file_details, staged, run.job, run.strays
            )

        except BaseException as error:
            # Remove the staged copies. Fitzflix rejects the original source.
            # Nothing has changed a staged source. A file too large for the
            # staging volume is the exception (#424). It is processed in
            # place, and the track statistics, the FLAC twins, or the MP4
            # metadata removal can rewrite it before a failure. The
            # untouched archive goes up first, so the original bytes stay
            # in S3. A stop signal keeps the source (#497).

            abandon_failed_stage(
                error,
                run.source_path,
                run.lock,
                may_keep=not run.in_place,
                strays=run.strays,
                pending_archive=pending_archive_of(file_details),
            )

        else:
            return _hand_off(
                run, work_path, file_details, hidden_output_file, staged, conversion
            )

        return True


def _admission_gate(file_path, run):
    """Decide if an incoming file can start its import. Return "ok", "retry", or "stop".

    "retry" means that the task is booked again (a dead volume, a file
    that grows, or a file that is not complete yet). "stop" means that
    the task ends with nothing to do, or after a reject."""

    from app.videos import _dead_volumes

    basename = run.basename
    completeness_retries = run.counters["completeness_retries"]

    # Do not start if a necessary volume is dead. A mount that fails
    # during the task strands partial files. Thus, defer to a retry.

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
        return run.retry(
            timedelta(minutes=5), f"Volumes unavailable ({', '.join(dead)})"
        )

    # If the incoming file does not exist, there is nothing to do.

    if not os.path.exists(file_path):
        return "stop"

    # A duplicate job or a late retry can reach a file that an earlier
    # job already imported in place (#287).

    if already_cataloged(file_path):
        current_app.logger.info(
            f"'{basename}' Is already in the library; nothing to import"
        )
        return "stop"

    # The work files add up to 22 bytes to the name ("." and
    # ".localized", ".remux.mkv", and others). A name system allows 255
    # bytes. A longer name made mkvmerge fail with no clear reason.

    if len(basename.encode("utf-8")) > MAX_NAME_BYTES:
        current_app.logger.error(
            f"'{basename}' has a name of more than {MAX_NAME_BYTES} bytes. "
            f"Rename it shorter"
        )
        move_to_rejects(file_path, "name too long")
        return "stop"

    # If the file name contains "temp-1234.", ignore the file.

    if re.search(r"\-temp\-\d+\.", basename):
        return "stop"

    # Do not process a file that a copy is still writing. If the file
    # grows, check again in 1 minute.

    size_before = os.path.getsize(file_path)
    time.sleep(GROWTH_CHECK_SECONDS)
    if os.path.getsize(file_path) != size_before:
        return run.retry(timedelta(minutes=1), "is still being copied", level="info")

    # A stalled network copy can fool the size check above. Its size
    # stays constant while the file is still incomplete. Let the
    # container prove completeness where it can. A partial Matroska or
    # MP4 file reports truncation. The time that the copy has stalled is
    # not important. Give a format that the probe cannot read a
    # modification-time quiet period instead. Fitzflix imports a file
    # that never proves itself within the budget in all cases, because
    # Fitzflix imports a corrupt-but-complete file as it is by design.

    verdict = probe_file_completeness(file_path)
    if verdict is False or (
        verdict is None
        and time.time() - os.path.getmtime(file_path) < COMPLETENESS_QUIET_SECONDS
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
            return run.retry(
                timedelta(minutes=1),
                f"{reason} (check {completeness_retries + 1} of "
                f"{MAX_COMPLETENESS_RETRIES})",
                level="info",
                completeness_retries=completeness_retries + 1,
            )

    return "ok"


def _identify(file_path, run):
    """Parse the name of an import and plan its conversion.

    Return (file_details, conversion), where conversion is None for a
    file that needs no conversion. Return "retry" after a TMDB outage,
    or "stop" after a reject."""

    basename = run.basename
    tmdb_retries = run.counters["tmdb_retries"]

    file_details = evaluate_filename(file_path)
    current_app.logger.info(f"'{basename}' File details: {file_details}")
    if not file_details:
        reason = getattr(file_details, "reason", None)

        # The TMDB tries have their own count. The transient count limits
        # the retries of an I/O error in the staging copy.

        if reason == TMDB_UNREACHABLE and tmdb_retries < TMDB_RETRIES:
            return run.retry(
                timedelta(minutes=30),
                "TMDB is unreachable",
                tmdb_retries=tmdb_retries + 1,
            )
        current_app.logger.error(
            f"'{basename}' rejected: {reason}!"
            if reason
            else f"'{basename}' doesn't match expected naming formats!"
        )
        move_to_rejects(file_path, reason or "incorrect filename")
        return "stop"

    # An HDR or BT.2020 import becomes a 1080p SDR conversion (#292). The
    # plan renames it to its 1080p quality now. Thus, the quality checks
    # compare the conversion, not the original.

    conversion = None
    if get_setting("CONVERT_HDR_TO_SDR"):
        conversion = _plan_conversion_or_reject(file_path, file_details)
        if conversion is False:
            return "stop"
        if conversion:
            file_details = converted_file_details(file_details, conversion)
            current_app.logger.info(
                f"'{basename}' {conversion['source_format']} "
                f"{conversion['source_quality']}. Importing as a "
                f"{conversion['target_quality']} SDR conversion"
            )
    return file_details, conversion


def _take_title_lock(file_details, run):
    """Take the title lock of an import. Return the lock, or None after a deferral.

    Fitzflix must not process a different version of this video at the
    same time. Thus, the lock key comes from specific movie or TV show
    fields. A different file with the same key waits until the
    processing of the first file is complete. Without the lock, the task
    returns to the localization queue. The queue tries it again when the
    lock becomes available."""

    from app.videos import acquire_lock_or_defer

    file_identifier = lock_key_for_details(file_details)
    current_app.logger.debug(f"'{run.basename}' Lock identifier: {file_identifier}")

    # The note of the lock follows the take in the same call (#498). A
    # worker that dies after this still leaves its lock token behind.

    lock = acquire_lock_or_defer(
        file_identifier,
        current_app.config["LOCALIZATION_TASK_TIMEOUT"] * 1000,
        current_app.import_queue,
        "app.videos.localization_task",
        minutes=(45, 75),
        timeout=current_app.config["LOCALIZATION_TASK_TIMEOUT"],
        description=f"'{run.basename}'",
        kwargs={"file_path": run.source_path, **run.counters},
    )
    if lock:
        run.lock = lock
        run.note()
    return lock


def _quality_and_space_gate(file_path, file_details, conversion, run):
    """Decide if a locked import goes on. Return "ok", "hold", "reject", or "wait".

    "hold" waits for an admin, "reject" sends an inferior file to
    rejects, and "wait" books the task again for library space. Each of
    the 3 releases the title lock. "wait" also clears run.lock."""

    basename = run.basename
    space_waits = run.counters["space_waits"]

    # A conversion waits for an admin when the library has a real rip of
    # the title at its quality or better. "Import" sends the file back
    # here with replace_existing.

    if conversion:
        if held_decision(file_path):
            file_details["replace_existing"] = True
        else:
            blockers = real_rips_blocking(file_details)
            if blockers:
                held = hold_import(file_path, file_details, conversion, blockers)
                # A held file no longer waits for space. A stale mark
                # would block the "Import" choice of the Held page (#434).
                clear_space_wait(basename)
                run.release_lock()
                return "hold" if held is not None else "reject"

    # Look for better-quality versions of this file that already exist.

    better_versions = find_better_files(file_details)
    if better_versions:
        current_app.logger.info(
            f"Better versions of '{basename}' exist; skipping import"
        )
        for better in better_versions:
            current_app.logger.debug(vars(better))

        move_to_rejects(file_path, "inferior quality")
        clear_space_wait(basename)
        run.release_lock()
        notify_admin(
            "Received an inferior-quality file",
            "inferior_warning",
            basename=basename,
            better_versions=better_versions,
            rejects_directory=current_app.config["REJECTS_DIR"],
        )
        return "reject"

    # Wait for free space on the library volume before the heavy work.
    # Without the space, the final copy would fail and discard the work.
    # The wait releases the title lock (#262).

    output_directory = os.path.join(
        current_app.config["LIBRARY_DIR"], file_details.get("dirname")
    )
    needed = int(os.path.getsize(file_path) * SPACE_FACTOR) + SPACE_RESERVE
    shortfall = library_space_shortfall(output_directory, needed)
    if shortfall:
        _log_space_wait(basename, output_directory, needed, shortfall, space_waits)
        mark_space_wait(basename)
        run.release_lock()
        run.lock = None
        run.retry(
            SPACE_WAIT, "waits for space", level=None, space_waits=space_waits + 1
        )
        return "wait"
    if space_waits:
        clear_space_wait(basename)
        current_app.logger.info(
            f"'{basename}' Continuing the import after {space_waits} "
            f"wait(s) for space"
        )
    return "ok"


def _stage_source(file_path, run):
    """Copy the source to local staging. Return (work_path, staged), or None.

    The archive upload and the localization tools then do their heavy
    I/O on the local disk. A network failure costs a retry, not a
    stranded partial file. None means that the task is booked again.
    The staged copy goes in run.strays.

    The space check counts what the other imports on staging still need
    (#294). Without enough space, the import waits and releases the
    title lock. Only a file that cannot fit on the staging volume at all
    is processed on its source volume. Then staged is False."""

    from app.videos import (
        MAX_TRANSIENT_RETRIES,
        TRANSIENT_COPY_ERRNOS,
        copy_with_progress,
    )

    basename = run.basename
    space_waits = run.counters["space_waits"]
    transient_retries = run.counters["transient_retries"]
    staging_dir = current_app.config["STAGING_DIR"]
    staging_needed = int(os.path.getsize(file_path) * STAGING_FACTOR)
    staging = reserve_staging(basename, staging_needed, [basename], staging_dir)
    if staging == "lock":
        # The space was not checked. Thus, this is no space wait, and the
        # space-wait counter and marker stay as they are (#478).
        current_app.logger.info(
            f"'{basename}' The staging lock is busy. Trying again in "
            f"{int(STAGING_LOCK_RETRY.total_seconds() // 60)} minute"
        )
        run.release_lock()
        run.lock = None
        run.retry(STAGING_LOCK_RETRY, "waits for the staging lock", level=None)
        return None
    if staging == "wait":
        current_app.logger.info(
            f"'{basename}' Needs {_gigabytes(staging_needed)} on staging. "
            f"The other imports still need "
            f"{_gigabytes(staging_outstanding(staging_dir, exclude=basename))}. "
            f"Trying again in {int(SPACE_WAIT.total_seconds() // 60)} minutes"
        )
        mark_space_wait(basename)
        run.release_lock()
        run.lock = None
        run.retry(
            SPACE_WAIT,
            "waits for staging space",
            level=None,
            space_waits=space_waits + 1,
        )
        return None

    if staging != "ok":
        current_app.logger.warning(
            f"'{basename}' Does not fit on the staging volume, "
            f"processing on the source volume instead"
        )
        return file_path, False

    staged_path = os.path.join(staging_dir, basename)
    run.strays.append(staged_path)
    record_task_stage("Copying to staging", "started")
    try:
        copy_with_progress(
            file_path, staged_path, run.job, basename, "Copying to local staging"
        )
    except OSError as e:
        record_task_stage("Copying to staging", "failed")
        if (
            e.errno not in TRANSIENT_COPY_ERRNOS
            or transient_retries >= MAX_TRANSIENT_RETRIES
        ):
            raise

        # An unstable mount revoked the file handles of the copy. The
        # source is not damaged. Thus, clean up and retry after the mount
        # settles. Do not reject a good file.

        try:
            os.remove(staged_path)
        except OSError:
            pass
        run.release_lock()
        run.retry(
            timedelta(minutes=5),
            f"Staging copy failed with a transient I/O error ({e}) "
            f"(attempt {transient_retries + 1} of {MAX_TRANSIENT_RETRIES})",
            transient_retries=transient_retries + 1,
        )
        return None

    record_task_stage("Copying to staging", "done")
    return staged_path, True


def _archive_original(work_path, file_details):
    """Upload the untouched file to AWS S3 storage as a backup, if the setting is on.

    A surviving record can claim the untouched key for different bytes.
    Then the upload goes to a run-scoped key below pending-untouched/
    (#430). The record of the import claims that key. After the commit,
    an archive move takes it to its final key. A failed import deletes
    it. Thus, a failure never replaces the archive of a good rip."""

    if get_setting("ARCHIVE_ORIGINAL_MEDIA"):
        run_id = uuid.uuid4().hex[:12]
        (
            file_details["aws_key"],
            file_details["aws_date_uploaded"],
            file_details["aws_filesize_bytes"],
        ) = upload_untouched(work_path, lambda key: pending_archive_key(key, run_id))


def _localize(source_path, work_path, file_details, staged, job, strays):
    """Write the localized output of an import. Return (work_path, hidden_output_file).

    A non-Matroska file first becomes Matroska where mkvmerge can carry
    it. Then work_path is the converted file. A Matroska file loses its
    non-native tracks. Any other file is copied as it is. Each work file
    that this step makes goes in strays."""

    basename = os.path.basename(source_path)
    staging_dir = current_app.config["STAGING_DIR"]

    current_app.logger.info(f"'{basename}' Starting localization process")

    output_directory = os.path.join(
        current_app.config["LIBRARY_DIR"], file_details.get("dirname")
    )

    # Parse the incoming file and get its details with MediaInfo.

    current_app.logger.info(f"'{basename}' Parsing with MediaInfo")
    media_info = MediaInfo.parse(work_path)
    current_app.logger.debug(f"'{basename}' -> {media_info.to_json()}")

    for track in media_info.tracks:
        if track.track_type == "General" and track.format:
            current_app.logger.info(f"'{basename}' File container {track.format}")
            file_details["container"] = track.format

    # Fitzflix first remuxes a non-Matroska file into a Matroska container.
    # Thus, every importable format gets the same localization. Fitzflix
    # imports a format that mkvmerge cannot carry as it is.

    if file_details.get("container") != "Matroska":
        scratch_dir = staging_dir if staged else output_directory
        makedirs_on_mounted_volume(scratch_dir)
        converted_file = hidden(os.path.join(scratch_dir, basename), ".convert.mkv")
        strays.append(converted_file)

        if convert_to_matroska(work_path, converted_file, job, basename):
            # Use the converted file. Rename the final output to agree with
            # its new container.

            if work_path != source_path:
                try:
                    os.remove(work_path)
                except OSError:
                    pass
            work_path = converted_file
            file_details["container"] = "Matroska"
            file_details["basename"] = with_extension(file_details["basename"], "mkv")
            file_details["extension"] = "mkv"
            file_details["file_path"] = os.path.join(
                file_details["dirname"], file_details["basename"]
            )

        else:
            current_app.logger.warning(
                f"'{basename}' Can't be converted to Matroska, importing as-is"
            )

    # If staging is on, Fitzflix writes the localized output next to the
    # staged copy. Thus, only the complete file goes to the library.

    # On staging, the name comes from the source. The library name of an
    # extra is only the extra, for example "Trailer.mkv". The extras of 2
    # films would then write the same staging file.

    # The partial output of a failed step is a stray in both places
    # (#389). Without staging, it is a hidden file in the library folder.

    if staged:
        hidden_output_file = staged_output_path(
            staging_dir, basename, file_details.get("basename")
        )
    else:
        hidden_output_file = hidden(
            os.path.join(output_directory, file_details.get("basename"))
        )
    strays.append(hidden_output_file)

    # Export a localized version of the incoming file.

    if file_details.get("container") == "Matroska":
        _localize_matroska(
            work_path, hidden_output_file, output_directory, basename, job
        )
    else:
        _copy_as_is(
            work_path,
            hidden_output_file,
            output_directory,
            basename,
            file_details.get("container"),
            job,
        )
    return work_path, hidden_output_file


def _localize_matroska(work_path, hidden_output_file, output_directory, basename, job):
    """Write the localized Matroska output of work_path to hidden_output_file."""

    from app.videos import wait_for_subprocess

    current_app.logger.info(f"'{basename}' Localizing as a Matroska file")

    # Give each lossless track that is not FLAC or PCM a FLAC twin
    # immediately before it. An Apple TV client can play FLAC natively.
    # Always keep the original for direct play and for future
    # passthrough. A file whose twins already exist (a MakeMKV "FLAC Plus
    # Original Audio" rip, or a re-download of a supplemented upload)
    # passes through as it is. CREATE_FLAC_TWINS turns this step off for
    # passthrough players.

    if not get_setting("CREATE_FLAC_TWINS"):
        current_app.logger.info(f"'{basename}' CREATE_FLAC_TWINS is off. No FLAC twins")
    else:
        supplement_lossless_tracks(work_path)

    # Sometimes the input mkv file has no track details, for example the
    # number of subtitle elements in a subtitle track. Fitzflix needs
    # those details to tell if there is possibly a forced subtitle track.
    # This command adds the details to the file if they are missing.

    current_app.logger.info(f"'{basename}' Adding track statistics tags")

    statistics_tags_process = subprocess.Popen(
        [
            current_app.config["MKVPROPEDIT_BIN"],
            "--add-track-statistics-tags",
            work_path,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
        bufsize=1,
    )
    watch_mkvmerge_progress(
        statistics_tags_process, job, basename, "Adding track statistics tags"
    )
    wait_for_subprocess(statistics_tags_process, ok_returncodes=(0, 1))

    # Parse the file again, because it now has the track statistics tags.

    current_app.logger.info(f"'{basename}' Parsing added statistics with MediaInfo")
    media_info = MediaInfo.parse(work_path)
    current_app.logger.debug(f"'{basename}' -> {media_info.to_json()}")
    audio_tracks = get_audio_tracks_from_file(work_path)
    subtitle_tracks = get_subtitle_tracks_from_file(work_path)

    # ISO 639-2 gives 20 languages 2 codes. MediaInfo reports the
    # terminological code for each file with a LanguageIETF element,
    # whatever wrote it ("deu" for a track written as "ger"). Thus, the
    # comparisons use same_language, and the mkvmerge filters use the
    # spelling of the file (#423).

    native_language = file_language_spelling(
        current_app.config["NATIVE_LANGUAGE"], audio_tracks
    )
    native_subtitle_language = file_language_spelling(
        current_app.config["NATIVE_LANGUAGE"], subtitle_tracks
    )

    # Find the audio tracks to export.

    # If there are no audio tracks, mkvmerge could use the --no-audio
    # flag. But the default of the first audio track is sufficient,
    # because no track will exist.

    if len(audio_tracks) == 0:
        first_audio_track_language = "1"

    elif audio_tracks[0].get("language"):
        first_audio_track_language = audio_tracks[0].get("language")

    else:
        first_audio_track_language = 1

    # If the first audio track is in the native language, remove all other
    # languages.

    if len(audio_tracks) >= 1 and same_language(
        first_audio_track_language, native_language
    ):
        current_app.logger.info(
            f"'{basename}' First audio track matches native language "
            f"'{native_language}'"
        )
        output_audio_langs = native_language

    # The first audio track can be in a language that is not the native
    # language, while the native language is present. Then export the
    # tracks in the first language and all native-language audio. The
    # file is probably a dub, or it has native-language commentary tracks.

    elif any(
        same_language(track["language"], native_language) for track in audio_tracks
    ):
        current_app.logger.info(
            f"'{basename}' First audio track is foreign, "
            f"but '{native_language}' audio is present"
        )
        output_audio_langs = f"{first_audio_track_language},{native_language}"

    # If there is no native-language track, export only the tracks in the
    # first language. The file is probably a subtitled movie with no
    # commentary track.

    else:
        current_app.logger.info(f"'{basename}' No '{native_language}' audio track")
        output_audio_langs = first_audio_track_language

    # Find the tracks to export and make the output file.

    makedirs_on_mounted_volume(output_directory)

    first_audio_foreign = len(audio_tracks) >= 1 and not same_language(
        first_audio_track_language, native_language
    )
    native_subtitles = any(
        same_language(track["language"], native_language) for track in subtitle_tracks
    )
    if first_audio_foreign and native_subtitles:
        current_app.logger.info(
            f"'{basename}' Non-native audio, "
            f"but '{native_language}' subtitles are present"
        )
    elif native_subtitles:
        current_app.logger.info(f"'{basename}' '{native_language}' audio and subtitles")
    elif subtitle_tracks:
        current_app.logger.info(f"'{basename}' No '{native_language}' subtitles")
    else:
        current_app.logger.info(f"'{basename}' No subtitles whatsoever")

    mkvmerge_process = subprocess.Popen(
        build_localize_command(
            current_app.config["MKVMERGE_BIN"],
            work_path,
            hidden_output_file,
            output_audio_langs,
            native_subtitle_language,
            subtitle_tracks,
            first_audio_foreign,
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
        bufsize=1,
    )
    watch_mkvmerge_progress(mkvmerge_process, job, basename, "Localizing")
    wait_for_subprocess(mkvmerge_process, ok_returncodes=(0, 1))


def build_localize_command(
    mkvmerge,
    source,
    output,
    audio_langs,
    native_language,
    subtitle_tracks,
    first_audio_foreign,
):
    """Return the mkvmerge command that localizes source into output.

    audio_langs is the value of -a. With native-language subtitles, the
    command keeps only those subtitles. Then, if the first audio track is
    foreign, the first native-language subtitle track becomes the default
    and the others do not. If the audio is native, no native-language
    subtitle track is a default. Without native-language subtitles, the
    command keeps no subtitles. The output has no title and no track
    names. This function reads nothing and runs nothing."""

    native_tracks = [
        track
        for track in subtitle_tracks
        if same_language(track["language"], native_language)
    ]
    if native_tracks:
        flags = []
        for index, track in enumerate(native_tracks):
            on = first_audio_foreign and index == 0
            flags.extend(
                ["--default-track-flag", f"{track['streamorder']}:{1 if on else 0}"]
            )
        spellings = list(dict.fromkeys(track["language"] for track in native_tracks))
        subtitles = ["-s", ",".join(spellings)] + flags
    else:
        subtitles = ["--no-subtitles"]
    return (
        [mkvmerge, "-o", output, "-a", audio_langs]
        + subtitles
        + ["--title", "", "--track-name", "-1:", source]
    )


def _copy_as_is(
    work_path, hidden_output_file, output_directory, basename, container, job
):
    """Copy a file that is not Matroska to its hidden output.

    An MPEG-4 file first loses its metadata through AtomicParsley."""

    from app.videos import wait_for_subprocess

    if container == "MPEG-4":
        current_app.logger.info(f"'{basename}' Removing MPEG-4 metadata")
        atomicparsley_process = subprocess.Popen(
            [
                current_app.config["ATOMICPARSLEY_BIN"],
                work_path,
                "--metaEnema",
                "--overWrite",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            bufsize=1,
        )
        try:
            for line in atomicparsley_process.stdout:
                line = line.replace("\n", "")
                current_app.logger.info(f"'{os.path.basename(work_path)}' {line}")
                report_progress(
                    job, os.path.basename(work_path), "Removing MPEG-4 metadata"
                )
        except BaseException:
            # AtomicParsley rewrites the work file in place. It must stop
            # before the cleanup (#499).
            stop_process(atomicparsley_process)
            raise

        wait_for_subprocess(atomicparsley_process)

        current_app.logger.info(f"'{basename}' Removed MPEG-4 metadata")

    else:
        current_app.logger.info(f"'{basename}' Not Matroska or MPEG-4, importing as-is")

    current_app.logger.info(f"'{basename}' Copying to '{hidden_output_file}'")
    makedirs_on_mounted_volume(output_directory)
    report_progress(job, basename, "Copying to destination")
    shutil.copy(work_path, hidden_output_file)


def _hand_off(run, work_path, file_details, hidden_output_file, staged, conversion):
    """Give the localized output to the next stage. Return True, or False on a failure.

    The working copy (the staged source or the conversion temp file) is
    no longer necessary. The file-operation queue carries the localized
    output to the library. Then it gives the quick database work to the
    sql queue. A conversion first goes to the transcode queue."""

    source_path, basename, lock = run.source_path, run.basename, run.lock

    if work_path != source_path:
        try:
            os.remove(work_path)
        except OSError:
            pass

    # The parse can have renamed the file (the title made canonical
    # against an existing series, or the container changed to .mkv). The
    # key of the trail is the basename. Thus, merge the trail under the
    # new name before the move job stamps "queued" on it.

    if file_details.get("basename") != basename:
        migrate_trail(current_app.redis, basename, file_details.get("basename"))

    # The localized file stays on staging until its move, and a
    # conversion writes more next to it. Shrink the reservation to what
    # remains (#294).

    # A failure here (Redis away at the enqueue) must not keep the title
    # lock for its full TTL (#340). The source stays in the import
    # directory for the hourly sweep.

    try:
        if staged:
            output_size = os.path.getsize(hidden_output_file)
            set_staging_reservation(
                basename,
                output_size * (CONVERSION_STAGING_FACTOR if conversion else 1),
                [basename],
            )

        # A conversion first goes to the transcode queue. That task gives
        # the file to move_localized_file after the encode.

        if conversion:
            enqueue_task(
                "app.videos.convert_localized_file",
                source_path,
                file_details,
                lock,
                hidden_output_file,
                conversion,
                description=f"'{file_details.get('basename')}'",
            )
        else:
            enqueue_task(
                "app.videos.move_localized_file",
                source_path,
                file_details,
                lock,
                hidden_output_file,
                description=f"'{basename}'",
            )
    except Exception:
        abandon_handoff(
            source_path,
            lock,
            hidden_output_file,
            pending_archive=pending_archive_of(file_details),
        )
        return False
    return True


# A supervisor stop, a restart, or a shutdown sends these signals. The
# file is good. Thus, a stage that they stop keeps its source (#497).

INTERRUPT_SIGNALS = frozenset({signal.SIGTERM, signal.SIGINT, signal.SIGHUP})


def interrupted(error):
    """Return True if a stop signal ended the task or its child process (#497).

    A child that a signal stops has a negative return code. A second
    SIGTERM to an rq worker raises SystemExit inside the job."""

    if isinstance(error, (SystemExit, KeyboardInterrupt)):
        return True
    code = getattr(error, "returncode", None)
    return (
        isinstance(error, subprocess.CalledProcessError)
        and isinstance(code, int)
        and -code in INTERRUPT_SIGNALS
    )


def abandon_failed_stage(error, source_path, lock, may_keep=True, **kwargs):
    """Clean up a stage after error. Re-raise an error that is not an Exception.

    A stop signal keeps the source in the import directory for the
    hourly sweep (#497). Any other error rejects the source. With
    may_keep False, a stop signal rejects the source too."""

    stop = may_keep and interrupted(error)
    if stop:
        current_app.logger.warning(
            f"'{os.path.basename(source_path)}' A stop signal interrupted "
            f"the import ({error!r}). The source stays for the next sweep"
        )
        kwargs["keep_source"] = True
    else:
        current_app.logger.error(traceback.format_exc())
    abandon_import(source_path, lock, **kwargs)
    if not isinstance(error, Exception):
        raise error


def abandon_import(
    source_path,
    lock,
    strays=(),
    reservation=None,
    reason="exception",
    keep_source=False,
    pending_archive=None,
):
    """Clean up an import stage that failed. Each step is best effort.

    Release the title lock, remove the stray work files, release the
    staging reservation, and move the source to rejects with reason.
    With keep_source, the source stays in the import directory. A failed
    step must not stop the next step. A lock that stays held blocks the
    title until the TTL of the lock expires. The lock goes first (#497).
    The S3 and file steps can take longer than the stop wait of
    supervisor. reservation is the name of the reservation. The default
    is the basename of the source. pending_archive is the run-scoped S3
    upload of the import (#430). It is deleted."""

    if lock:
        try:
            current_app.lock_manager.unlock(lock)
            current_app.logger.info(f"Removed lock {lock}")
        except Exception:
            current_app.logger.warning(traceback.format_exc())
    if pending_archive:
        try:
            discard_pending_archive(pending_archive)
        except Exception:
            current_app.logger.warning(traceback.format_exc())

    for stray in strays:
        if not stray:
            continue
        try:
            os.remove(stray)
        except FileNotFoundError:
            pass
        except OSError:
            current_app.logger.warning(traceback.format_exc())
    try:
        release_staging_reservation(reservation or os.path.basename(source_path))
    except Exception:
        current_app.logger.warning(traceback.format_exc())
    if not keep_source:
        try:
            move_to_rejects(source_path, reason)
        except Exception:
            current_app.logger.error(traceback.format_exc())


class HandoffFailed(Exception):
    """A stage could not queue its retry or its next stage (#521).

    The source is good. Only Redis failed. Thus, the handler of the
    stage keeps the source for the hourly sweep through abandon_handoff.
    It does not reject the source."""


def handoff(submit, *args, **kwargs):
    """Call an enqueue or a requeue. Raise HandoffFailed if it fails (#521)."""

    try:
        return submit(*args, **kwargs)
    except Exception as e:
        raise HandoffFailed(f"{type(e).__name__}: {e}") from e


def abandon_handoff(source_path, lock, staged_output, pending_archive=None):
    """Clean up after a stage could not queue the next stage (#340).

    Remove the localized output, release the staging reservation and the
    title lock, and leave the source in place. The hourly sweep of the
    import directory queues the source again. Without staging, the
    output is a hidden file in the library folder (#389). It goes too."""

    current_app.logger.error(
        f"'{os.path.basename(source_path)}' The next stage could not be "
        f"queued. The source stays for the next sweep: " + traceback.format_exc()
    )
    abandon_import(
        source_path,
        lock,
        strays=[staged_output],
        keep_source=True,
        pending_archive=pending_archive,
    )


def _plan_conversion_or_reject(file_path, file_details):
    """Return the conversion plan of an import, None, or False after a reject (#292).

    A conversion needs ARCHIVE_ORIGINAL_MEDIA. The library gets only the
    conversion. Thus, without the archive, Fitzflix would lose the only
    copy of the original. A Dolby Vision Profile 5 file needs
    FFMPEG_PLACEBO_BIN. Each of these rejects the file and tells the admin."""

    basename = os.path.basename(file_path)
    try:
        plan = plan_conversion(file_path, file_details)
    except ValueError as e:
        reason = str(e)
        plan = None
    else:
        if not plan:
            return None
        if not get_setting("ARCHIVE_ORIGINAL_MEDIA"):
            reason = (
                "A conversion needs ARCHIVE_ORIGINAL_MEDIA. Without the archive, "
                "the original would be lost"
            )
        elif needs_placebo(plan) and not current_app.config["FFMPEG_PLACEBO_BIN"]:
            reason = "A Dolby Vision Profile 5 conversion needs FFMPEG_PLACEBO_BIN"
        else:
            return plan

    # The notification is the alert. An ERROR record would mail the admin
    # a second time (#453).
    current_app.logger.warning(f"'{basename}' rejected: {reason}")
    move_to_rejects(file_path, "conversion not possible")
    notify_admin(
        "A conversion is not possible",
        "conversion_rejected",
        basename=basename,
        reason=reason,
        rejects_directory=current_app.config["REJECTS_DIR"],
    )
    return False


def inspect_localized_file(file_path, container, job=None):
    """Apply the final track flags to a localized file and report its details.

    This function runs where the file is: on local staging, before the
    library copy. Thus, the flag edits and the parse never occur on the
    sql queue. The first audio track becomes the only default. The first
    subtitle track becomes the default if the audio is foreign. This
    function removes the empty subtitle tracks. It returns the media
    details that finalize_localization needs.
    """

    from app.videos import wait_for_subprocess

    name = os.path.basename(file_path)

    if container == "Matroska":
        audio_tracks = get_audio_tracks_from_file(file_path)
        subtitle_tracks = get_subtitle_tracks_from_file(file_path)

        # Set the first audio track as the only default audio track.

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

            # Clear the default flag from every other audio track. Then a
            # player does not select unpredictably between multiple defaults.

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
            try:
                for line in mkvpropedit_process.stdout:
                    current_app.logger.info(f"'{name}' {line.rstrip()}")
            except BaseException:
                stop_process(mkvpropedit_process)
                raise

            wait_for_subprocess(mkvpropedit_process, ok_returncodes=(0, 1))

        # The 2 spellings of a dual-code language are the same language
        # (#423).

        native_language = current_app.config["NATIVE_LANGUAGE"]

        # Set the first subtitle track as the default if the first audio is
        # foreign and if there is no default subtitle track.

        existing_default_subtitle_track = any(
            track["default"] == True for track in subtitle_tracks
        )

        if (
            len(subtitle_tracks) >= 1
            and len(audio_tracks) >= 1
            and not same_language(audio_tracks[0].get("language"), native_language)
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

        # Remove the subtitle tracks that have 0 elements.

        remove_empty_subtitle_tracks(file_path)

    return _extract_media_details(file_path)


def move_localized_file(
    source_path,
    file_details,
    lock,
    hidden_output_file,
    transient_retries=0,
    space_waits=0,
    vanished_retries=0,
    lock_waits=0,
):
    """Carry the localized output to a hidden name at its library destination.

    This is the long file copy. It is split out of finalize_localization.
    Thus, it runs on the file-operation queue. Several copies can run in
    parallel. The single-worker sql queue only sees the quick database
    work and an immediate same-volume rename. The title lock passes
    through to finalize.

    A library volume without space for the copy makes the task wait. The
    output stays on staging, and the task keeps the title lock (#262).

    After a copy to another volume, the output stays on staging until
    finalize_localization renames the copy into place. An unstable NFS
    mount can lose the just-written copy (the Movies share, Oct 2026).
    Then finalize gives the copy back to this task. vanished_retries
    counts those returns.

    If another task holds the title lock, the copy waits (#339). Then
    neither this task nor finalize writes the files of the title.
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

        # Every retry of the copy carries all 4 counters (#351). Each
        # retry changes only its own counter.

        move_args = (source_path, file_details, lock, hidden_output_file)
        counters = {
            "transient_retries": transient_retries,
            "space_waits": space_waits,
            "vanished_retries": vanished_retries,
            "lock_waits": lock_waits,
        }

        def retry(delay, reason, level="warning", **changes):
            renew_space_wait(os.path.basename(source_path))
            handoff(
                requeue,
                "app.videos.move_localized_file",
                key=source_path,
                delay=delay,
                reason=reason,
                counters={**counters, **changes},
                args=move_args,
                description=f"'{basename}'",
                level=level,
            )
            return False

        def defer():
            renew_space_wait(os.path.basename(source_path))
            handoff(
                defer_for_title_lock,
                "app.videos.move_localized_file",
                move_args,
                counters,
                source_path,
                basename,
            )
            return False

        if not _hold_title_lock(lock, basename):
            return defer()

        # Defer if a necessary volume is dead. The task keeps the title lock
        # for the retry.

        dead = _dead_volumes(
            [
                output_directory,
                os.path.dirname(hidden_output_file),
                os.path.dirname(source_path),
            ]
        )
        if dead:
            return retry(
                timedelta(minutes=5),
                f"Volumes unavailable ({', '.join(dead)}) for the library copy",
            )

        destination_hidden = hidden(os.path.join(output_directory, basename))

        def wait_for_space(needed, shortfall, directory=None):
            """Book the copy again after SPACE_WAIT. Keep the title lock.

            If another task holds the title lock, the copy waits for the
            lock instead (#353)."""

            if not _hold_title_lock(lock, basename):
                return defer()
            _log_space_wait(
                basename, directory or output_directory, needed, shortfall, space_waits
            )
            mark_space_wait(
                os.path.basename(source_path), pending_archive_of(file_details)
            )

            # The weekly sweep deletes a hidden staging file that is 1 week
            # old. A wait for space has no limit. Thus, each wait touches
            # the localized file, and the sweep keeps it.

            try:
                os.utime(hidden_output_file)
            except OSError:
                pass
            return retry(
                SPACE_WAIT, "waits for space", level=None, space_waits=space_waits + 1
            )

        # A copy to another volume needs space there. Check before the
        # inspection and the copy. Thus, a wait repeats neither.

        # A stat error skips the check. Then the copy meets the same
        # error, and the existing error handling applies.

        try:
            crosses_volumes = hidden_output_file != destination_hidden and (
                _crosses_volumes(hidden_output_file, output_directory)
            )
            needed = os.path.getsize(hidden_output_file) + SPACE_RESERVE
        except OSError:
            crosses_volumes = False
        if crosses_volumes:
            shortfall = library_space_shortfall(output_directory, needed)
            if shortfall:
                return wait_for_space(needed, shortfall)
        if space_waits:
            clear_space_wait(os.path.basename(source_path))
            current_app.logger.info(
                f"'{basename}' The space is available again after "
                f"{space_waits} wait(s). Copying"
            )

        staged_output = None
        try:
            job = get_current_job()

            # The final flag edits and the metadata extraction occur here,
            # while the file is still on local staging. Thus, the sql queue
            # never opens the file.

            # The removal of an empty subtitle track remuxes the output in
            # place. That needs a second copy on staging, outside the
            # reservation. An import must never go to rejects for space
            # (#262). Thus, a failure with too little free space for that
            # copy waits like a copy into a full library. The failed remux
            # removes its partial file before this check.

            try:
                inspection = inspect_localized_file(
                    hidden_output_file, file_details.get("container"), job
                )
            except (subprocess.CalledProcessError, OSError):
                work_dir = os.path.dirname(hidden_output_file)
                needed = os.path.getsize(hidden_output_file) + SPACE_RESERVE
                free = shutil.disk_usage(work_dir).free
                if free < needed:
                    return wait_for_space(needed, needed - free, directory=work_dir)
                raise

            try:
                makedirs_on_mounted_volume(output_directory)

                if hidden_output_file == destination_hidden:
                    # A file too big for staging was localized at its
                    # destination name already.
                    pass

                elif (
                    os.stat(os.path.dirname(hidden_output_file)).st_dev
                    == os.stat(output_directory).st_dev
                ):
                    _rename_with_retries(hidden_output_file, destination_hidden)

                else:
                    # Keep the output on staging. finalize_localization
                    # removes it after the rename into place. If the mount
                    # loses the copy first, finalize copies it again from
                    # here.

                    copy_with_progress(
                        hidden_output_file, destination_hidden, job, basename
                    )
                    staged_output = hidden_output_file

            except OSError as e:
                if e.errno in SPACE_ERRNOS:
                    # The volume filled during the copy, for example from
                    # another import. The output is intact on staging.
                    # Remove the partial copy and wait. Never reject the
                    # file for space.

                    try:
                        os.remove(destination_hidden)
                    except OSError:
                        pass
                    needed = os.path.getsize(hidden_output_file) + SPACE_RESERVE
                    return wait_for_space(
                        needed,
                        library_space_shortfall(output_directory, needed) or needed,
                    )

                if (
                    e.errno not in TRANSIENT_COPY_ERRNOS
                    or transient_retries >= MAX_TRANSIENT_RETRIES
                ):
                    raise

                # An unstable mount interrupted the library copy. But the
                # localized output is still intact on staging. Remove the
                # partial destination and retry only this copy. Do not reject
                # the file and do the whole import again. The task keeps the
                # title lock for the retry.

                try:
                    os.remove(destination_hidden)
                except OSError:
                    pass
                return retry(
                    timedelta(minutes=5),
                    f"Library copy failed with a transient I/O error ({e}) "
                    f"(attempt {transient_retries + 1} of {MAX_TRANSIENT_RETRIES})",
                    transient_retries=transient_retries + 1,
                )

        except HandoffFailed:
            # A retry could not be booked (#521). The source is good. It
            # stays for the hourly sweep, as after a failed enqueue below.

            _remove_staged_output(destination_hidden)
            abandon_handoff(
                source_path,
                lock,
                hidden_output_file,
                pending_archive=pending_archive_of(file_details),
            )
            return False

        except BaseException as error:
            # Remove both hidden copies. The original source is unchanged.
            # Fitzflix rejects the source. A stop signal keeps it (#497).

            abandon_failed_stage(
                error,
                source_path,
                lock,
                strays=(hidden_output_file, destination_hidden),
                pending_archive=pending_archive_of(file_details),
            )

        else:
            # A failed enqueue must not keep the title lock for its full
            # TTL, or leave the copies behind (#437). The source stays in
            # the import directory for the hourly sweep.
            try:
                enqueue_task(
                    "app.videos.finalize_localization",
                    source_path,
                    file_details,
                    lock,
                    destination_hidden,
                    inspection,
                    staged_output=staged_output,
                    vanished_retries=vanished_retries,
                    description=f"'{basename}'",
                )
            except Exception:
                _remove_staged_output(destination_hidden)
                abandon_handoff(
                    source_path,
                    lock,
                    hidden_output_file,
                    pending_archive=pending_archive_of(file_details),
                )
                return False

        return True


def _report_import_move_to_radarr(source_directory, output_directory):
    """Tell Radarr when an import left its folder for a different one.

    The source must be below a Radarr root folder, and the output must
    be a different folder. Then Radarr gets the new path and a rescan,
    and the empty folder of the download goes away. The Radarr update
    runs as its own job on the request queue. Thus, the sql queue never
    waits on Radarr (#268). A Radarr failure is logged. It does not
    fail the import. The file is already in place."""

    from app.maintenance import clear_leftover_directory
    from app.radarr_push import enqueue_radarr_push, radarr_configured

    source = os.path.realpath(source_directory)
    output = os.path.realpath(output_directory)
    if source == output or not radarr_configured():
        return
    roots = [os.path.realpath(r) for r in current_app.config["RADARR_ROOT_FOLDERS"]]
    if not any(os.path.commonpath([source, root]) == root for root in roots):
        return
    enqueue_radarr_push(
        "app.radarr_push.follow_import_move_task",
        source,
        os.path.basename(output),
        description=f"Reporting an import move to Radarr ({os.path.basename(output)})",
    )
    try:
        clear_leftover_directory(source)
    except Exception:
        current_app.logger.warning(traceback.format_exc())


# A just-written library copy that the mount loses is copied again from
# staging at most this many times. Then the import fails as before.

MAX_VANISHED_RETRIES = 2

# The wait of a cataloging for Sonarr, and the limit of the waits (#372).
# 12 waits of 5 minutes are 1 hour.

SONARR_WAIT = timedelta(minutes=5)
MAX_SONARR_WAITS = 12


def _copy_vanished(hidden_output_file, staged_output, vanished_retries):
    """Return True if the hidden library copy is gone and staging can replace it."""

    return (
        bool(staged_output)
        and vanished_retries < MAX_VANISHED_RETRIES
        and os.path.exists(staged_output)
        and not os.path.exists(hidden_output_file)
    )


def _remove_staged_output(staged_output):
    """Remove the staging copy of an output after the import is complete."""

    if staged_output:
        try:
            os.remove(staged_output)
        except OSError:
            pass


def finalize_localization(
    file_path,
    file_details,
    lock,
    hidden_output_file,
    inspection,
    staged_output=None,
    vanished_retries=0,
    sonarr_waits=0,
    lock_waits=0,
):
    """Add a localized file to the database and move it into position.

    The task runs in 3 phases. _build_records writes the File, Movie or
    TVSeries, quality, and track rows to the session, and finds the
    worse files. _place_file renames the hidden library copy into place,
    or gives the copy back to move_localized_file. Then the task commits.
    _after_commit does the follow-up work. Each follow-up is best effort.
    A failure after the commit never moves the file, and never stops the
    next follow-up.

    hidden_output_file is the hidden library copy that move_localized_file
    made. inspection is the media details that it read from the file.

    staged_output is the same file on staging, when the library copy
    crossed volumes. The Movies NFS mount can drop at the end of a large
    write. Then macOS remounts it, and the just-written hidden copy is
    gone (Fort Apache, 2026-10-15). If the rename finds no hidden copy,
    the task gives the copy back to move_localized_file, which copies
    again from staged_output. MAX_VANISHED_RETRIES limits the returns.
    The task removes staged_output after the rename and the commit.

    An episode finds its series first, before any record (#372). When
    Sonarr does not answer and a Sonarr rename can explain a new title,
    the task waits SONARR_WAIT, up to MAX_SONARR_WAITS times. Then it
    makes the new series.

    Each run first extends its title lock, as the move does (#520). If
    another task took the lock meanwhile, the task waits for it. It
    writes no row and moves no file of the title until then.
    """

    from app.videos import _dead_volumes

    with app.app_context():
        # The queue page lists this job with a banner and a row. Name the
        # stage there, as the other stages of the pipeline do.

        basename = file_details.get("basename")
        report_progress(get_current_job(), basename, "Cataloging")

        output_directory = os.path.join(
            current_app.config["LIBRARY_DIR"], file_details.get("dirname")
        )
        counters = {
            "vanished_retries": vanished_retries,
            "sonarr_waits": sonarr_waits,
            "lock_waits": lock_waits,
        }

        # A dead volume, or a Sonarr wait, can requeue this task for
        # longer than the lock TTL. Then a new import of the same title
        # can take the lock. This check comes first. Thus, each retry
        # extends the lock, or waits for the other task (#520).

        if not _hold_title_lock(lock, basename):
            try:
                defer_for_title_lock(
                    "app.videos.finalize_localization",
                    (
                        file_path,
                        file_details,
                        lock,
                        hidden_output_file,
                        inspection,
                        staged_output,
                    ),
                    counters,
                    file_path,
                    basename,
                )
            except Exception:
                # The source stays for the hourly sweep (#521).
                _remove_staged_output(hidden_output_file)
                abandon_handoff(
                    file_path,
                    lock,
                    staged_output,
                    pending_archive=pending_archive_of(file_details),
                )
            return False

        # Defer if a necessary volume is dead. Do this before the records.
        # Then the task keeps the title lock for the retry and does not
        # release it.

        dead = _dead_volumes([output_directory, os.path.dirname(file_path)])
        if dead:
            try:
                requeue(
                    "app.videos.finalize_localization",
                    key=file_path,
                    delay=timedelta(minutes=5),
                    reason=f"Volumes unavailable ({', '.join(dead)}) for the cataloging",
                    # The Sonarr wait count goes with the retry. Otherwise each
                    # volume drop starts the limit of the waits again (#402).
                    counters=counters,
                    args=(file_path, file_details, lock, hidden_output_file),
                    kwargs={"inspection": inspection, "staged_output": staged_output},
                    description=f"'{file_details.get('basename')}'",
                )
            except Exception:
                # The retry could not be booked (#437). The source stays
                # for the hourly sweep, and the lock and the copies go.
                _remove_staged_output(hidden_output_file)
                abandon_handoff(
                    file_path,
                    lock,
                    staged_output,
                    pending_archive=pending_archive_of(file_details),
                )
            return False

        # A failure before the commit must not leave the new file in the
        # library with no record (#316). The handler needs to know if the
        # rename happened, and if it replaced a file at that path. None
        # means that the rename did not happen.

        placement = None
        try:
            output_file = os.path.join(
                current_app.config["LIBRARY_DIR"], file_details.get("file_path")
            )
            series_id = None
            if file_details.get("media_library") == TV_SHOWS:
                verdict, series_id = resolve_import_series(file_details)
                # End the read snapshot before the records. The Sonarr call
                # stays out of the write transaction.
                db.session.rollback()
                if verdict == "wait" and sonarr_waits < MAX_SONARR_WAITS:
                    handoff(
                        requeue,
                        "app.videos.finalize_localization",
                        key=file_path,
                        delay=SONARR_WAIT,
                        reason="waits for Sonarr, because a Sonarr rename can "
                        "own this series",
                        counters={**counters, "sonarr_waits": sonarr_waits + 1},
                        args=(file_path, file_details, lock, hidden_output_file),
                        kwargs={
                            "inspection": inspection,
                            "staged_output": staged_output,
                        },
                        description=f"'{file_details.get('basename')}'",
                    )
                    return False
            records = _build_records(
                file_details,
                inspection,
                os.path.basename(hidden_output_file),
                series_id,
            )
            placement = _place_file(
                file_path,
                file_details,
                lock,
                hidden_output_file,
                output_file,
                output_directory,
                staged_output,
                vanished_retries,
            )

            # A copy that goes back to move_localized_file keeps the title
            # lock for the retried chain.

            if placement == "handed_off":
                return False

            file, worse_local_copies, worse_aws_keys, kept_archives, emails = records
            _commit_records(file, kept_archives, file_path)

        except HandoffFailed:
            # A retry or the hand-off to the move could not be queued
            # (#521). The source is good. It stays for the hourly sweep,
            # and the lock and the copies go.

            db.session.rollback()
            _remove_staged_output(hidden_output_file)
            abandon_handoff(
                file_path,
                lock,
                staged_output,
                pending_archive=pending_archive_of(file_details),
            )
            return False

        except BaseException as error:
            db.session.rollback()

            # The source goes to rejects. Remove the hidden library copy,
            # or the renamed file when no record keeps it. A rename over an
            # existing file already replaced that file, and its record
            # still points to it. For an in-place import the renamed file
            # IS file_path, and it goes to rejects. A stop signal keeps
            # the source (#497).

            stray = None
            if placement is None:
                stray = hidden_output_file
            elif placement == "renamed" and file_path != output_file:
                stray = output_file
            _release_title_lock(lock)
            abandon_failed_stage(
                error,
                file_path,
                None,
                strays=(stray, staged_output),
                pending_archive=pending_archive_of(file_details),
            )
            return None

        # Each follow-up guards itself. This guard is for the code between
        # them. Nothing after the commit moves the file.

        try:
            _after_commit(
                file,
                file_path,
                file_details,
                output_file,
                output_directory,
                staged_output,
                worse_local_copies,
                worse_aws_keys,
                emails,
                inspection["audio_tracks"],
            )
        except Exception:
            current_app.logger.error(traceback.format_exc())
            db.session.rollback()
            current_app.logger.warning(
                f"'{output_file}' Was imported, but a follow-up step failed"
            )
        else:
            current_app.logger.info(f"'{file_path}' processed as '{output_file}'")
        finally:
            _release_title_lock(lock)

            # The import is done. Its staging reservation goes now, not
            # at the grace sweep of a later reservation (#461).

            try:
                release_staging_reservation(os.path.basename(file_path))
            except Exception:
                current_app.logger.warning(traceback.format_exc())
        return None


def _release_title_lock(lock):
    """Release the title lock of a finished cataloging and log it."""

    current_app.lock_manager.unlock(lock)
    current_app.logger.info(f"Removed lock {lock}")


def _build_records(file_details, inspection, output_name, series_id=None):
    """Write the records of a localized file to the session. Commit nothing.

    Return (file, worse_local_copies, worse_aws_keys, kept_archives,
    pending_emails). worse_local_copies holds (path, file id) of each
    worse library file. worse_aws_keys holds the archive keys to delete
    after the commit. kept_archives holds the archive-only originals to
    add with the commit. pending_emails holds the sends that wait for the
    commit. This function reads no file. output_name is for the log.
    series_id is the series of an episode that the cataloging found
    before the records."""

    # Look for this File record in the database. If it does not exist,
    # make a new one. If it exists, update that record.

    file = File.query.filter_by(file_path=file_details.get("file_path")).first()

    # A real rip that replaces a conversion keeps the original of the
    # conversion in S3, as an archive-only original (#292).

    kept_archives = []
    if not file:
        file = File.from_details(file_details)
        current_app.logger.debug(vars(file))
        current_app.logger.info(f"{file} Creating File record")
        db.session.add(file)

    else:
        current_app.logger.info(f"{file} Existing File record found")
        if (
            file.converted_from_quality
            and not file_details.get("converted_from_quality")
            and file.aws_key
            and file.aws_date_uploaded
        ):
            kept_archives.append(
                archived_original_from_file(file, "replaced by a real rip")
            )

        # The track rows of the existing record go in the
        # replace_track_rows call below.

        file.date_updated = datetime.now(timezone.utc)

    if file.media_library == MOVIES:
        _attach_movie(file, file_details)
    elif file.media_library == TV_SHOWS:
        _attach_tv_series(file, file_details, series_id)

    # Set the file quality details.

    quality = RefQuality.query.filter_by(
        quality_title=file_details.get("quality_title")
    ).first()
    file.quality = quality
    current_app.logger.info(f"{file} Setting file_quality {quality}")

    # The conversion fields (#292). A real rip clears them on a reused
    # record.

    file.converted_from_quality = file_details.get("converted_from_quality")
    file.converted_from_format = file_details.get("converted_from_format")

    # A file that is not a conversion has no transcoded/ copy. The weekly
    # S3 sync prunes the copy of a reused record (#299).

    if not file.converted_from_quality:
        file.aws_transcoded_key = None
        file.aws_transcoded_date_uploaded = None
        file.aws_transcoded_filesize_bytes = None

    # A reused record gets the untouched name of the new source. A
    # conversion that reuses the record of a real rip must not keep the
    # name of the rip (#292).

    if file_details.get("untouched_basename"):
        file.untouched_basename = file_details["untouched_basename"]

    # The media details arrive precomputed from move_localized_file. Set
    # the file video track info.

    for field, value in inspection["video"].items():
        setattr(file, field, value)

    # Write the track rows. The flag pass inside marks the forced state of
    # a suspicious subtitle track as unknown. The candidates query decides
    # later if the file needs triage. Imported content is NEW evidence. A
    # re-imported file first deletes an earlier reviewed verdict and the
    # stale aids. A replacement can have a forced track that the original
    # did not have.

    from app.triage import reset_triage_state

    reset_triage_state(file)
    replace_track_rows(file, inspection["audio_tracks"], inspection["subtitle_tracks"])

    # Set the localized date.

    file.date_localized = datetime.now(timezone.utc)

    # Set the AWS archived fields if the task uploaded the file to AWS S3
    # storage. A reused record can have the archive of its old file under
    # a different key. No record claims that key after the commit. A later
    # restore of that key would import the old file under the old name
    # (Sapphire, 2026-10-20). Thus, the key goes with the archives of the
    # worse files.

    replaced_key = None
    if (
        file.aws_date_uploaded
        and file.aws_key
        and file.aws_key != file_details.get("aws_key")
    ):
        replaced_key = file.aws_key

    file.aws_key = file_details.get("aws_key")
    file.aws_date_uploaded = file_details.get("aws_date_uploaded")
    file.aws_filesize_bytes = file_details.get("aws_filesize_bytes")

    # A pending move of the old archive does not apply to the new one
    # (#369). A reused conversion can keep a transcoded/ copy under the
    # old name. Its move is requested here, and it goes to the queue at
    # the commit.

    if file.aws_key_target:
        clear_archive_target(file)
    if file.converted_from_quality and file.aws_transcoded_key:
        request_archive_move(file)

    record_filesize(file, inspection["filesize_bytes"])
    current_app.logger.info(f"'{output_name}' {file.filesize_bytes} bytes")

    # replace_existing is the "Import" choice of an admin for a held
    # conversion.

    worse_local_copies, worse_aws_keys, pending_emails = _dispose_of_worse_files(
        file, kept_archives, file_details.get("replace_existing", False)
    )
    if replaced_key:
        worse_aws_keys.insert(0, replaced_key)
    return file, worse_local_copies, worse_aws_keys, kept_archives, pending_emails


def _attach_movie(file, file_details):
    """Find or make the Movie of a film file and attach it, with its feature type."""

    current_app.logger.info(
        f"{file} Searching in Movies table using "
        f"title='{file_details.get('title')}', year='{file_details.get('year')}'"
    )
    movie = Movie.query.filter_by(
        title=file_details.get("title"), year=file_details.get("year")
    ).first()
    if not movie:
        movie = Movie(title=file_details.get("title"), year=file_details.get("year"))
        current_app.logger.info(f"{file} Creating {movie}")

        # Check the new movie against the stored Criterion list. The
        # import never queries Wikidata (#283). The monthly refresh
        # updates the movie later.

        try:
            criterion_collection = stored_criterion_releases()
        except Exception:
            current_app.logger.warning(traceback.format_exc())
            criterion_collection = []

        assign_criterion_release(
            movie, *criterion_release_lookups(criterion_collection)
        )

        db.session.add(movie)

    file.movie = movie
    current_app.logger.info(f"{file} Associating with {movie}")

    # Set the special feature type if the file is a special feature.

    if file_details.get("feature_type_name"):
        feature_type = RefFeatureType.query.filter_by(
            feature_type=file_details.get("feature_type_name")
        ).first()
        file.feature_type = feature_type
        current_app.logger.info(f"{file} Marking as {feature_type}")


def _existing_series(file_details):
    """Return the TVSeries that the parsed details of an episode name, or None.

    The TMDB id of an id tag comes first. Then the parsed title. Then
    the title of the record that the parsed title names now. A series
    rename can change the title after the parse (#370)."""

    title = file_details.get("title")
    if file_details.get("tmdb_id"):
        found = TVSeries.query.filter_by(tmdb_id=file_details["tmdb_id"]).all()
        if len(found) == 1:
            return found[0]
    tv_series = TVSeries.query.filter_by(title=title).first()
    if tv_series is None and title:
        tv_series = TVSeries.query.filter_by(
            title=_canonical_series_title(title)
        ).first()
    return tv_series


def _show_folder(file_details):
    """Return the show folder of the dirname of an episode, or None."""

    parts = (file_details.get("dirname") or "").split("/")
    return parts[1] if len(parts) > 1 and parts[0] == TV_SHOWS else None


def _rename_plausible(title, folder):
    """Return True if a Sonarr rename can explain a new series title.

    The title has a year, a series with the bare title exists, and no
    file of Fitzflix is in the show folder yet."""

    year_form = re.fullmatch(r"(?P<base>.+) \((?P<year>\d{4})\)", title or "")
    if not year_form or not folder:
        return False
    if TVSeries.query.filter_by(title=year_form.group("base")).first() is None:
        return False
    prefix = f"{TV_SHOWS}/{folder}"
    return (
        File.query.filter(
            db.or_(File.dirname == prefix, File.dirname.like(f"{prefix}/%"))
        ).first()
        is None
    )


def resolve_import_series(file_details):
    """Return (verdict, series id) for the series of an episode import (#372).

    The cataloging calls this function before it writes any record.
    Thus, the Sonarr call does not hold the write transaction. The
    verdict is "ok" with the id of the series, or with None for a new
    series. A series that the name does not give can be the series of a
    rename to the title that waits. It can be the series that Sonarr
    keeps in the show folder of the file. The verdict is "wait" when
    Sonarr did not answer and a Sonarr rename is possible. A new series
    then would split the show."""

    from app.series_rename import series_for_import

    found = _existing_series(file_details)
    title = file_details.get("title")
    if found is not None or not title:
        return "ok", found.id if found is not None else None
    folder = _show_folder(file_details)
    found, ok = series_for_import(title, folder)
    if found is not None:
        current_app.logger.info(
            f"'{file_details.get('basename')}' The show folder {folder!r} "
            f"belongs to {found}"
        )
        return "ok", found.id
    if not ok and _rename_plausible(title, folder):
        return "wait", None
    return "ok", None


def _attach_tv_series(file, file_details, series_id=None):
    """Find or make the TVSeries of an episode file and attach it.

    series_id is the series that resolve_import_series found before the
    records. Else the details name the series (_existing_series). Only
    after that does the import make a new series. Else a Sonarr rename
    that adds a year splits the show (#372)."""

    title = file_details.get("title")
    current_app.logger.info(f"{file} Searching in TVSeries table using title='{title}")
    tv_series = db.session.get(TVSeries, series_id) if series_id else None
    if tv_series is None:
        tv_series = _existing_series(file_details)
    if not tv_series:
        tv_series = TVSeries(title=file_details.get("title"))
        current_app.logger.info(f"{file} Creating {tv_series}")
        db.session.add(tv_series)

    file.tv_series = tv_series
    current_app.logger.info(f"{file} Associating with {tv_series}")


def _dispose_of_worse_files(file, kept_archives, replace_existing=False):
    """Delete the rows of the worse files from the session. Return what waits for the commit.

    Return (worse_local_copies, worse_aws_keys, pending_emails). Their
    library copies are deleted only after the new file is in place and
    the commit succeeds. A rollback (an NFS hand-off, a lost copy, a
    failed rename) keeps their rows. Their files must still be there.
    The disposal emails also wait for the commit. An archive-only
    original of a worse conversion goes in kept_archives."""

    worse_files = find_worse_files(file, replace_existing)
    current_app.logger.info(f"{file} worse files: {worse_files}")

    worse_local_copies = []
    worse_aws_keys = []
    pending_emails = []
    for worse in worse_files:
        worse_local_copies.append(
            (os.path.join(current_app.config["LIBRARY_DIR"], worse.file_path), worse.id)
        )

        if (
            worse.converted_from_quality
            and not file.converted_from_quality
            and worse.aws_key
            and worse.aws_date_uploaded
        ):
            kept_archives.append(
                archived_original_from_file(worse, "replaced by a real rip")
            )

        # If the new file is from digital media, delete only the worse
        # digital-media files. Fitzflix always keeps the best
        # physical-media file.
        #
        # If the new file is from physical media, delete all worse files.
        # The media source is not important.

        if (
            worse.quality.physical_media == file.quality.physical_media
            or file.quality.physical_media == True
        ):
            if worse.aws_date_uploaded:
                # Record the key now. Delete it from AWS only after the
                # database commit succeeds. Then a failed commit cannot
                # cost the backup of a record that rolled back.
                worse_aws_keys.append(worse.aws_key)

            db.session.delete(worse)

        # The disc of a converted original stays on the shelf (#292).
        # Thus, Fitzflix sends no disposal email when a conversion
        # replaces a file or a file replaces a conversion.

        if (
            worse.quality.physical_media == True
            and file.quality.physical_media == True
            and not worse.converted_from_quality
            and not file.converted_from_quality
        ):
            pending_emails.extend(_disposal_emails(file, worse))
    return worse_local_copies, worse_aws_keys, pending_emails


def _disposal_emails(file, worse):
    """Return the sends that tell the admin and the to-do list about a replaced disc.

    The bodies render now, while the worse row is still in the session.
    The sends wait for the commit."""

    sends = []
    send = admin_email(
        "Replaced a physical media file",
        "replaced_physical_media",
        file=file,
        worse=worse,
    )
    if send is not None:
        sends.append(send)

    if current_app.config["TODO_EMAIL"]:
        todo_email = current_app.config["TODO_EMAIL"]
        sends.append(
            partial(
                send_email,
                f"Find and dispose of the media for '{worse.untouched_basename}'",
                sender=("Fitzflix", current_app.config["SERVER_EMAIL"]),
                recipients=[todo_email],
                text_body=render_template(
                    "email/replaced_physical_media.txt",
                    user=todo_email,
                    file=file,
                    worse=worse,
                ),
                html_body=render_template(
                    "email/replaced_physical_media.html",
                    user=todo_email,
                    file=file,
                    worse=worse,
                ),
            )
        )
    return sends


def _place_file(
    file_path,
    file_details,
    lock,
    hidden_output_file,
    output_file,
    output_directory,
    staged_output,
    vanished_retries,
):
    """Rename the hidden library copy into place. Return "renamed", "replaced", or "handed_off".

    move_localized_file already put the file on the destination volume.
    Thus, this is an immediate rename. "replaced" means that the rename
    replaced a file at that path. The file can be on another volume, or
    the mount can lose the copy. Then the session rolls back, and the
    copy goes back to the file-operation queue. That is "handed_off".
    Long file work never runs on the sql queue."""

    from app.videos import _rename_with_retries

    makedirs_on_mounted_volume(output_directory)
    if (
        os.stat(os.path.dirname(hidden_output_file)).st_dev
        != os.stat(output_directory).st_dev
    ):
        current_app.logger.warning(
            f"'{file_details.get('basename')}' isn't at the library "
            f"volume yet; re-queueing the library copy"
        )
        db.session.rollback()

        # The caller keeps the lock only after the enqueue returns. If
        # the enqueue fails, the caller releases the lock and keeps the
        # source (#340, #521).

        handoff(
            enqueue_task,
            "app.videos.move_localized_file",
            file_path,
            file_details,
            lock,
            hidden_output_file,
            description=f"'{file_details.get('basename')}'",
        )
        return "handed_off"

    replaced_existing = os.path.exists(output_file)
    try:
        _rename_with_retries(hidden_output_file, output_file)
    except OSError:
        if not _copy_vanished(hidden_output_file, staged_output, vanished_retries):
            raise

        # The mount lost the hidden copy, but the output is still on
        # staging. Copy it again. Keep the title lock. Wait 1 minute.
        # Thus, the mount is back before the copy starts.

        db.session.rollback()

        # The copy starts a new chain of the move. Its other counters
        # start at 0. The vanished count keeps the ids of the new chain
        # apart from the ids of the old one.

        handoff(
            requeue,
            "app.videos.move_localized_file",
            key=file_path,
            delay=timedelta(minutes=1),
            reason=(
                f"The library copy disappeared from "
                f"'{os.path.dirname(hidden_output_file)}'. The copy from "
                f"staging starts again (attempt {vanished_retries + 1} of "
                f"{MAX_VANISHED_RETRIES})"
            ),
            counters={
                "transient_retries": 0,
                "space_waits": 0,
                "vanished_retries": vanished_retries + 1,
                "lock_waits": 0,
            },
            args=(file_path, file_details, lock, staged_output),
            description=f"'{file_details.get('basename')}'",
        )
        return "handed_off"
    return "replaced" if replaced_existing else "renamed"


def _commit_records(file, kept_archives, file_path):
    """Commit the records of the import with its archive-only originals.

    The archive-only originals commit with the import. Their rows claim
    the keys. Thus, the delete of the worse keys skips them. An
    archive-only row for the key of the new file is no longer necessary.
    The held row of an "Import" choice is done."""

    for archived in kept_archives:
        db.session.add(archived)
    if file.aws_key:
        ArchivedOriginal.query.filter_by(aws_key=file.aws_key).delete()
    HeldImport.query.filter_by(held_path=file_path).delete()
    db.session.commit()


def _follow_up(output_file, name, step, *args, **kwargs):
    """Run 1 follow-up step of a committed import. Return its value, or None on a failure.

    The import itself succeeded. A failed step is logged, and the
    session rolls back. The file stays, and the next step runs. The
    timeout of rq and an interrupt are not caught (#376)."""

    try:
        return step(*args, **kwargs)
    except (BaseTimeoutException, KeyboardInterrupt):
        raise
    except Exception:
        current_app.logger.error(traceback.format_exc())
        db.session.rollback()
        current_app.logger.warning(
            f"'{output_file}' Was imported, but a follow-up step failed ({name})"
        )
        return None


def _after_commit(
    file,
    file_path,
    file_details,
    output_file,
    output_directory,
    staged_output,
    worse_local_copies,
    worse_aws_keys,
    pending_emails,
    audio_tracks,
):
    """Do the follow-up work of a committed import. Each step is best effort.

    The new file is in place, and its record is committed. The steps
    delete the worse library copies and send the emails. They give the
    staged copy to the Atmos job and delete the replaced archives. They
    remove the source and tell Radarr. Then they queue the TMDB refresh,
    the Atmos twin, the archive of a conversion, and the triage aids."""

    step = partial(_follow_up, output_file)

    step("worse copies", _delete_worse_copies, worse_local_copies, output_file)
    for send in pending_emails:
        step("email", send)

    # The staged copy can go to the Atmos job of this file. Then that job
    # does not read the file back from the NAS. The Atmos decision comes
    # later. Until then, keep the staged copy only if staging has space
    # for it. With CREATE_ATMOS_TWINS off, no Atmos job comes. Thus, keep
    # nothing. A failed step keeps nothing either.

    atmos_handoff = step("atmos handoff", _atmos_handoff, staged_output, output_file)
    if atmos_handoff is None:
        _remove_staged_output(staged_output)

    # The commit succeeded. Thus, delete the AWS archives of the replaced
    # files. Do it from the handoff queue. Then the sql worker does not
    # wait on the network. The delete is the last step of a file in the
    # pipeline. Thus, it does not wait in line behind long file work
    # (Glenn, 2026-10-15).

    # A run-scoped upload (#430) moves to its final key now. The target
    # claims that key first. Thus, the delete of a replaced archive with
    # the same key below skips it.

    # The move runs later, and the source is removed now. Thus, the
    # pending object must exist first (#494). A missing object gets a new
    # upload of the source. The check is its own step. The source goes
    # only if that step returned True. A failure in the step, or in the
    # move request after it, keeps the source.

    source_can_go = True
    if file_path != output_file and file.aws_key and is_pending_archive(file.aws_key):
        source_can_go = (
            step("pending upload", ensure_pending_upload, file, file_path) is True
        )
    step("archive key", _request_final_archive_key, file)

    for worse_key in worse_aws_keys:
        step("archive delete", _delete_replaced_archive, worse_key)

    # Remove the imported file, unless the localized file replaced it.
    # Fitzflix must not remove the file that it just made.

    # The archive move runs later. If the pending upload is gone and the
    # new upload failed, the source is the only copy of the rip (#494).
    # Then it goes to rejects/unarchived, out of the import sweep.

    if file_path != output_file and not source_can_go:
        step("source hold", move_to_rejects, file_path, "unarchived")
    elif file_path != output_file:
        step("source removal", _remove_source, file_path)

    step("radarr", _report_film_move, file, file_path, output_directory)

    step("tmdb refresh", _enqueue_tmdb_enrichment, file, file_details)

    atmos_queued = step(
        "atmos twin", _enqueue_atmos_twin, file, atmos_handoff, staged_output
    )
    if not atmos_queued:
        _remove_staged_output(staged_output)

    # A conversion archives its 1080p library file in transcoded/ (#299).
    # An Atmos job archives it when it finishes. Thus, the upload waits
    # for that job.

    step("transcoded archive", _enqueue_transcoded_archive, file, atmos_queued)

    possibly_foreign_language, lossy_lead = _audio_alerts(
        audio_tracks, current_app.config["NATIVE_LANGUAGE"]
    )
    if possibly_foreign_language and len(audio_tracks) > 1:
        step(
            "foreign audio email",
            notify_admin,
            "Foreign audio track added",
            "possibly_foreign_audio",
            file=file,
        )

    step("file id", lambda: current_app.logger.info(f"{file} File ID {file.id}"))

    step("triage snapshots", _enqueue_triage_snapshots, file)
    step("audio comparison", _enqueue_audio_comparison, file)

    if lossy_lead:
        step(
            "lossy audio email",
            notify_admin,
            "Added a file that has a lossless audio track",
            "lossy_audio",
            file=file,
        )


def _audio_alerts(audio_tracks, native_language):
    """Return (possibly_foreign_language, lossy_lead) for the audio tracks of an import.

    The first track is possibly foreign if its language is not native,
    "und", or "zxx". The lead is lossy if the first track is lossy and
    another track is lossless."""

    first_language = audio_tracks[0].get("language") if audio_tracks else None
    possibly_foreign_language = bool(audio_tracks) and not (
        same_language(first_language, native_language)
        or first_language in ("und", "zxx")
    )
    lossless = [
        track.get("compression_mode", "Lossy") == "Lossless" for track in audio_tracks
    ]
    first_audio_track_lossy = not (lossless and lossless[0])
    return possibly_foreign_language, first_audio_track_lossy and any(lossless)


def _report_film_move(file, file_path, output_directory):
    """Tell Radarr about the move of a film import. An episode has nothing to report."""

    if file.movie_id:
        _report_import_move_to_radarr(os.path.dirname(file_path), output_directory)


def _enqueue_transcoded_archive(file, atmos_queued):
    """Queue the transcoded/ archive of a conversion, unless an Atmos job does it."""

    if (
        file.converted_from_quality
        and get_setting("ARCHIVE_ORIGINAL_MEDIA")
        and not atmos_queued
    ):
        enqueue_task(
            "app.videos.upload_task", file.id, description=f"'{file.basename}'"
        )


def _delete_worse_copies(worse_local_copies, output_file):
    """Delete the library copies of the worse files and their triage snapshots.

    The new file never has the path of a worse file. The check is a
    guard."""

    from app.triage import remove_triage_snapshots

    for worse_path, worse_id in worse_local_copies:
        if worse_path == output_file:
            continue
        try:
            os.remove(worse_path)
            current_app.logger.info(f"Deleted local file '{worse_path}'")
        except FileNotFoundError:
            pass
        except OSError as e:
            # A share that remounts must not stop the rest of the
            # follow-up work. The orphan is visible on the share.
            current_app.logger.warning(
                f"'{worse_path}' Couldn't delete the replaced file: {e}"
            )
        remove_triage_snapshots(worse_id)

        # The episode thumb of a deleted TV file has no video left (#469)

        tv_library = os.path.abspath(current_app.config["TV_LIBRARY"])
        if os.path.abspath(worse_path).startswith(tv_library + os.sep):
            remove_episode_thumb_of(worse_path)


def _atmos_handoff(staged_output, output_file):
    """Return the staged copy for the Atmos job, or None with CREATE_ATMOS_TWINS off."""

    from app.atmos import staged_handoff

    if not get_setting("CREATE_ATMOS_TWINS"):
        return None
    return staged_handoff(staged_output, output_file)


def _request_final_archive_key(file):
    """Request the archive move of a run-scoped upload to its final key (#430).

    The records are committed. Thus, a replaced record no longer claims
    the key, and the move can take it. A record that still claims the
    key keeps it. Then the move gives this file the key with its id."""

    if file.aws_key and is_pending_archive(file.aws_key):
        request_archive_move(file)
        db.session.commit()


def _delete_replaced_archive(worse_key):
    """Queue the AWS delete of a replaced archive that no row claims.

    The new file can claim the same key that its predecessor held (a
    repointed key, or a re-import on the same basename). Never delete a
    key that a surviving row claims."""

    if untouched_key_still_claimed(worse_key):
        current_app.logger.info(
            f"Keeping '{worse_key}' in AWS — another file record still claims it"
        )
        return
    enqueue_task(
        "app.videos.aws_delete",
        worse_key,
        description=f"Deleting '{worse_key}' from AWS",
    )


def _remove_source(file_path):
    """Remove the source of an import. A source that is already gone is not an error."""

    try:
        os.remove(file_path)
    except FileNotFoundError:
        pass


def _enqueue_tmdb_enrichment(file, file_details):
    """Queue the TMDB refresh of a new film or series that has no match yet.

    TMDB enrichment runs as its own task after the commit. Thus, the
    cataloging never waits on the network. The enrichment sends an email
    if the movie still has no match. The fetch runs on the request
    queue. It gives its payload to the sql queue for the database writes.
    A filename id tag goes with the task. Then the enrichment fetches
    that exact title. It does not search by name (#155)."""

    movie = file.movie
    tv_series = file.tv_series
    if file.movie_id and movie.tmdb_id == None and not movie.tmdb_ignored:
        enqueue_task(
            "app.videos.refresh_tmdb_info",
            "Movies",
            movie.id,
            file_details.get("tmdb_id"),
            notify_if_missing=True,
            description=f"Refreshing TMDB data for '{movie.title} ({movie.year})'",
        )
    elif file.series_id and tv_series.tmdb_id == None and not tv_series.tmdb_ignored:
        enqueue_task(
            "app.videos.refresh_tmdb_info",
            "TV Shows",
            tv_series.id,
            file_details.get("tmdb_id"),
            description=f"Refreshing TMDB data for '{tv_series.title}'",
        )


def _enqueue_atmos_twin(file, atmos_handoff, staged_output):
    """Queue the E-AC-3 Atmos twin of a TrueHD Atmos track. Return True if a job is queued.

    A TrueHD Atmos track without its E-AC-3 Atmos twin gets the
    MediaConvert supplement (#55b). The job comes after the commit. Then
    it sees the complete records. It goes on the handoff queue. The file
    is already in the pipeline. Thus, its twin does not wait behind the
    new imports. CREATE_ATMOS_TWINS turns this step off for passthrough
    players."""

    from app.atmos import maybe_enqueue_atmos_supplement

    if not get_setting("CREATE_ATMOS_TWINS"):
        current_app.logger.info(
            f"'{file.basename}' CREATE_ATMOS_TWINS is off. No E-AC-3 Atmos twins"
        )
        return False
    return bool(
        maybe_enqueue_atmos_supplement(
            file.id, staged_source=atmos_handoff, queue=current_app.handoff_queue
        )
    )


def _enqueue_triage_snapshots(file):
    """Queue the triage snapshots of a new file, and tell the admin about a forced track.

    Make the inspection aids of the triage page now, while the file is
    new and certainly local. The gate is the SAME candidates query that
    the triage page uses. It is not the first-track-baseline heuristic.
    That heuristic misses a file whose suspicious track comes FIRST. The
    tracks of Baby Driver read [49, 3110, 4334] elements, and nothing was
    flagged."""

    from app.triage import maybe_enqueue_triage_snapshots

    if maybe_enqueue_triage_snapshots(file.id):
        notify_admin(
            "Possibly forced subtitle track",
            "possibly_forced_subtitle",
            file=file,
        )


def _enqueue_audio_comparison(file):
    """Queue the audio comparison clips of a new file.

    The candidates check inside maybe_enqueue prevents clips for the
    intentional E-AC-3 Atmos lead of the Atmos trio."""

    from app.triage import maybe_enqueue_audio_comparison

    maybe_enqueue_audio_comparison(file.id)


def manual_import_task():
    """Scan the Import directory and import the files that are not in the queue."""

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

            # A filename can contain more than one quality string. Record the
            # files already handled. Then each file is enqueued one time per
            # scan.

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
                            if enqueue_localization(
                                os.path.join(current_app.config["IMPORT_DIR"], file),
                                quiet=True,
                            ):
                                current_app.logger.info(
                                    f"'{os.path.basename(file)}' Found in import directory"
                                )

                            current_app.lock_manager.unlock(lock)

            _retry_failed_in_place_imports()

        except Exception:
            current_app.logger.error(traceback.format_exc())

        else:
            return True


# The retries of the failed in-place localization of 1 path. The window
# is a fixed 7 days from the first retry (follow-up #437).

IN_PLACE_RETRIES_KEY = "fitzflix:in-place-import-retries:{}"
IN_PLACE_RETRY_WINDOW = 7 * 86400
MAX_IN_PLACE_RETRIES = 3


def _retry_failed_in_place_imports():
    """Queue again the failed localizations of the files outside the import directory (#437).

    A Sonarr or Radarr import reads the file at its library path. A kill
    or a timeout of its localization leaves the file in the library with
    no record, and the arr event does not come again. The scan of the
    import directory above never sees such a file. A path gets at most
    MAX_IN_PLACE_RETRIES retries."""

    from rq.registry import FailedJobRegistry

    queue = current_app.import_queue
    import_dir = os.path.abspath(current_app.config["IMPORT_DIR"])
    redis = current_app.redis
    for job in Job.fetch_many(
        FailedJobRegistry(queue=queue).get_job_ids(), connection=redis
    ):
        if job is None or job.func_name != "app.videos.localization_task":
            continue
        path = job.args[0] if job.args else job.kwargs.get("file_path")
        if not path or os.path.abspath(os.path.dirname(path)) == import_dir:
            continue
        if not os.path.isfile(path) or already_cataloged(path):
            continue
        if localization_pending(queue, path):
            continue
        counter = IN_PLACE_RETRIES_KEY.format(path_tag(path))
        if int(redis.get(counter) or 0) >= MAX_IN_PLACE_RETRIES:
            continue
        if not enqueue_localization(path, quiet=True):
            continue
        # Only a queued retry counts. The first retry starts the window,
        # and the later retries do not move it.
        tries = redis.incr(counter)
        if tries == 1:
            redis.expire(counter, IN_PLACE_RETRY_WINDOW)
        current_app.logger.warning(
            f"'{path}' Its in-place localization failed. It is queued again "
            f"(retry {tries} of {MAX_IN_PLACE_RETRIES})"
        )


# Plex external-id tags (#155). A movie name or a show-folder name can
# have {tmdb-NNN}, {imdb-ttNNN}, or {tvdb-NNN} after the year, in either
# order with {edition-...}. The id names the exact title. Thus, if the id
# is present, it selects the metadata source. Fitzflix then does not
# search by title.

ID_TAG_BLOCK = r"\{(?:tmdb-\d+|tvdb-\d+|imdb-tt\d+)\}"
NAME_TAG_BLOCK = rf"(?:{ID_TAG_BLOCK}|\{{edition-[^{{}}]+\}})"

ID_TAG_RE = re.compile(
    r"\{(?:(?P<source>tmdb|tvdb)-(?P<value>\d+)|imdb-(?P<imdb_value>tt\d+))\}"
)
EDITION_TAG_RE = re.compile(r"\{edition-(?P<edition>[^{}]+)\}")


def parse_name_tags(tags_text):
    """Split the brace-tag region of a name into its external-id tags and
    its edition.

    Return ([(source, value, raw_text), ...], edition_or_None).
    """

    id_tags = []
    for match in ID_TAG_RE.finditer(tags_text or ""):
        source = match.group("source") or "imdb"
        value = match.group("value") or match.group("imdb_value")
        id_tags.append((source, value, match.group(0)))
    edition_match = EDITION_TAG_RE.search(tags_text or "")
    return id_tags, edition_match.group("edition") if edition_match else None


def preferred_id_tag(id_tags):
    """Return the preferred tag if a name has several. tmdb outranks imdb.
    imdb outranks tvdb."""

    order = {"tmdb": 0, "imdb": 1, "tvdb": 2}
    return min(id_tags, key=lambda tag: order[tag[0]], default=None)


def resolve_external_id_tag(source, value, kind, log=True):
    """Resolve an imdb/tvdb tag to a TMDB id through the TMDB /find endpoint.

    Thus, TMDB stays the single metadata source. kind ("movie" or "tv")
    selects the /find results bucket to read. Return the TMDB id. Return
    False if TMDB answered and knows nothing under that id. Then the
    caller must reject the file. It must not guess by title. Return None
    if TMDB was not reachable. The caller can accept that.
    """

    try:
        body = tmdb_json(
            f"/find/{value}", {"external_source": f"{source}_id"}, timeout=30
        )
        if log:
            current_app.logger.debug(body)
        results = body.get(f"{kind}_results")

    except Exception as e:
        response = getattr(e, "response", None)
        if response is not None and response.status_code == 404:
            # TMDB answered. There is no such external id.
            return False
        current_app.logger.warning(traceback.format_exc())
        return None

    if not results:
        return False

    return results[0].get("id") or False


# The rejection reason of a yearless name whose id tag needs TMDB while
# TMDB cannot be reached. The import tries again. It does not reject.

TMDB_UNREACHABLE = "tmdb unreachable"
TMDB_RETRIES = 48

# The longest source name that leaves room for the names of the work files.

MAX_NAME_BYTES = 230


class FilenameRejection:
    """A falsy evaluate_filename result that names the reason for the
    rejection.

    Thus, the reject is loud. The file goes into a labeled rejects
    subfolder, not into the generic "incorrect filename" folder. A
    reason of None is the generic reject. Then the rejects folder, the
    log line, and the text of the filename tester stay generic. detail
    names the rule that rejected the name, for the log and the tests."""

    def __init__(self, reason, detail=None):
        self.reason = reason
        self.detail = detail or reason

    def __bool__(self):
        return False

    def __repr__(self):
        return f"FilenameRejection({self.reason!r}, {self.detail!r})"


# The name formats. A movie name can have Plex id and edition tags between
# the year and the dash (#155). The yearless Plex form "Title {tmdb-NNN}"
# is also accepted, but only with an id tag. Thus, "Title -
# [Quality].ext" stays rejected.

# The title is lazy. The first "(YYYY)" is the year of the film. An extra
# can carry a year in its own name: "Star Wars (1977) - Featurettes -
# Star Wars (1977) - Original Trailer".

MOVIE_NAME_RE = re.compile(
    r"(?P<title>.+?) \((?P<year>\d{4})\)"
    rf"(?P<tags>(?: {NAME_TAG_BLOCK})*)"
    r" \-(?: (?P<version>.+) | )\[(?P<quality_title>.+)\]\.(?P<extension>.+)"
)
YEARLESS_MOVIE_NAME_RE = re.compile(
    r"(?P<title>.+?)"
    rf"(?P<tags>(?: {NAME_TAG_BLOCK})+)"
    r" \-(?: (?P<version>.+) | )\[(?P<quality_title>.+)\]\.(?P<extension>.+)"
)
TV_NAME_SEARCH_RE = re.compile(
    r"(.+) \- S(\d+)E(\d+)(?:\-E(\d+))? \-(?: (.+) | )\[(.+)\]\.(.+)"
)
TV_NAME_RE = re.compile(
    r"(?P<title>.+) \- S(?P<season>\d+)E(?P<episode>\d+)"
    r"(?:\-E(?P<last_episode>\d+))? \-(?: (?P<version>.+) | )"
    r"\[(?P<quality_title>.+)\]\.(?P<extension>.+)"
)


def evaluate_filename(file_path, tmdb_id=None, log=True):
    """Examine a file name and return the details of its movie or TV show.

    Return a FilenameRejection for a name that Fitzflix cannot import.
    Pass log=False for a preview of a filename (for example, the admin
    filename tester). Then the dry run does not fill the log as a real
    import does.
    """

    basename = os.path.basename(file_path)

    # Try the TV series match first. If not, a TV series with a year in the
    # name (for example, "Doctor Who (2005) - S01E01 - [DVD].mkv") matches
    # as movie: "Doctor Who", year: 2005, version: "S01E01".

    if TV_NAME_SEARCH_RE.search(basename):
        return _parse_tv(basename, log)
    if MOVIE_NAME_RE.match(basename) or YEARLESS_MOVIE_NAME_RE.match(basename):
        return _parse_movie(basename, tmdb_id, log)
    return FilenameRejection(None, "no name format matches")


def _clean(value):
    """Return value with each run of white space as 1 space, or None for an empty value."""

    return " ".join(value.split()).strip() if value else None


def _details(
    media_library,
    dirname,
    basename,
    plex_title,
    title,
    edition,
    quality_title,
    fullscreen,
    extension,
    **numbers,
):
    """Return the details dict of a parsed name, with its text fields cleaned.

    numbers go between title and edition, in their order: year, tmdb_id
    and feature_type_name for a film, or tmdb_id, season, episode and
    last_episode for an episode. The import log prints the dict."""

    return {
        "media_library": _clean(media_library),
        "file_path": _clean(os.path.join(dirname, basename)),
        "dirname": _clean(dirname),
        "basename": _clean(basename),
        "plex_title": _clean(plex_title),
        "title": _clean(title),
        **numbers,
        "edition": _clean(edition),
        "quality_title": _clean(quality_title),
        "fullscreen": True if fullscreen else None,
        "extension": _clean(extension),
    }


def _standard_full_screen(version):
    """Split a version text at " - ". Return (parts, fullscreen).

    Each part that reads as full screen, in any case and with or without
    a space, becomes "Full Screen". fullscreen is True if a part did."""

    parts = version.split(" - ")
    fullscreen = False
    for i, part in enumerate(parts):
        if part.upper().replace(" ", "") == "FULLSCREEN":
            fullscreen = True
            parts[i] = "Full Screen"
    return parts, fullscreen


def _put_full_screen_last(parts):
    """Move the first "Full Screen" part of a version to the end."""

    parts.pop(parts.index("Full Screen"))
    parts.append("Full Screen")


def _known_quality(quality_title):
    """Return True if quality_title is the title of a RefQuality row."""

    return RefQuality.query.filter_by(quality_title=quality_title).first() is not None


def _parse_tv(basename, log):
    """Return the details of an episode name, or a FilenameRejection."""

    tv = TV_NAME_RE.match(basename)
    media_library = "TV Shows"
    title = tv.group("title")

    # A Plex id tag is on the show-folder part of the name (#155). Remove
    # it from the series title, but keep it. The show folder keeps the
    # tag, because Plex reads it there.

    series_id_tag = None
    series_tmdb_id = None
    title_tags = re.fullmatch(rf"(?P<base>.+?)(?P<tags>(?: {ID_TAG_BLOCK})+)", title)
    if title_tags:
        title = title_tags.group("base")
        series_id_tag = preferred_id_tag(parse_name_tags(title_tags.group("tags"))[0])

    if series_id_tag:
        series_tmdb_id = _resolve_series_tag(series_id_tag, basename, log)
        if isinstance(series_tmdb_id, FilenameRejection):
            return series_tmdb_id

        # The id names the exact series. Use the title of the record that
        # already owns that id. Do not use the spelling of the filename.

        if series_tmdb_id:
            existing_series = TVSeries.query.filter_by(tmdb_id=series_tmdb_id).first()
            if existing_series:
                title = existing_series.title

    title = _canonical_series_title(title)

    # A tag names the series exactly. When no record has its id yet, the
    # title can belong to a different series. "The Office {tmdb-2316}"
    # must not join the UK "The Office". There, its episodes could also
    # delete the episodes of that series as worse. Series titles are
    # unique. Thus, the file waits in rejects for a name with the year of
    # the series.

    if series_tmdb_id:
        holder = TVSeries.query.filter_by(title=title).first()
        if holder is not None and holder.tmdb_id not in (None, series_tmdb_id):
            if log:
                current_app.logger.error(
                    f"'{basename}' names TMDB series {series_tmdb_id}, but "
                    f"'{title}' is TMDB series {holder.tmdb_id}. Rename the "
                    f"file with the year of its series"
                )
            return FilenameRejection("title of another series")

    season = int(tv.group("season"))
    episode = int(tv.group("episode"))
    last_episode = int(tv.group("last_episode") or tv.group("episode"))

    # A reversed span ("S01E05-E03") is a bad name (#482). The row, the
    # lock key, and the worse-file rule would all see a bad span.

    if last_episode < episode:
        if log:
            current_app.logger.error(
                f"'{basename}' has a reversed episode span, "
                f"E{episode:02d} to E{last_episode:02d}"
            )
        return FilenameRejection(None, "reversed episode span")

    # If the file quality name is not an expected name, reject the file.

    quality_title = tv.group("quality_title")
    if not _known_quality(quality_title):
        return FilenameRejection(None, "unknown quality")

    extension = tv.group("extension")

    # Remove spaces and periods from the end of the folder name, as Sonarr
    # does:
    # https://github.com/Sonarr/Sonarr/blob/phantom-develop/src/NzbDrone.Core/Organizer/FileNameBuilder.cs#L353

    folder_title = title
    while folder_title.endswith(" ") or folder_title.endswith("."):
        folder_title = folder_title.strip(" ")
        folder_title = folder_title.strip(".")

    # Tag in, tag out. The show folder keeps the id tag that came with the
    # name. After the tag resolves, it has the canonical tmdb form.

    if series_id_tag:
        folder_tag = (
            f"{{tmdb-{series_tmdb_id}}}" if series_tmdb_id else series_id_tag[2]
        )
        folder_title = f"{folder_title} {folder_tag}"

    if season == 0:
        dirname = os.path.join(media_library, folder_title, "Specials")

    else:
        dirname = os.path.join(
            media_library, folder_title, f"Season {tv.group('season').zfill(2)}"
        )

    fullscreen = False
    version = None
    if tv.group("version"):
        version_strings, fullscreen = _standard_full_screen(tv.group("version"))
        if fullscreen:
            _put_full_screen_last(version_strings)
        version = " - ".join(version_strings)

    # The episode code keeps the digits of the name.

    plex_title, basename = library_name(
        {
            "library": media_library,
            "title": title,
            "season": tv.group("season"),
            "episode": tv.group("episode"),
            "last_episode": tv.group("last_episode"),
            "version": version,
            "quality_title": quality_title,
            "extension": extension,
        }
    )

    return _details(
        media_library,
        dirname,
        basename,
        plex_title,
        title,
        version,
        quality_title,
        fullscreen,
        extension,
        tmdb_id=series_tmdb_id,
        season=season,
        episode=episode,
        last_episode=last_episode,
    )


def _series_record_of_tag(series_id_tag):
    """Return the matched series record that owns the id of a tag, or None.

    This makes no network call. An imdb or tvdb tag matches the stored
    external id of a record that has a TMDB id."""

    source, value, _ = series_id_tag
    if source == "tmdb":
        return TVSeries.query.filter_by(tmdb_id=int(value)).first()
    return (
        TVSeries.query.filter(
            (TVSeries.tvdb_id == int(value))
            if source == "tvdb"
            else (TVSeries.imdb_id == value)
        )
        .filter(TVSeries.tmdb_id != None)
        .first()
    )


def _resolve_series_tag(series_id_tag, basename, log):
    """Return the TMDB id that a series id tag names, None, or a FilenameRejection.

    None means that TMDB could not be reached. A matched series already
    stores its external ids. Thus, an imdb/tvdb tag usually resolves
    without the network."""

    source, value, _ = series_id_tag
    if source == "tmdb":
        return int(value)
    existing = _series_record_of_tag(series_id_tag)
    if existing:
        return existing.tmdb_id
    resolved = resolve_external_id_tag(source, value, "tv", log=log)
    if resolved is False:
        if log:
            current_app.logger.error(
                f"'{basename}' carries {{{source}-{value}}}, "
                f"but TMDB knows no series under that id"
            )
        return FilenameRejection("id not found")
    return resolved


def _canonical_series_title(title):
    """Return the title of the series record that a parsed series title names.

    This is the convention of the movie branch. Sonarr can name a file
    with or without the year of the series. Either form must go to the
    record that already owns the show. It must not split the show into a
    second series. A name WITH A YEAR attaches to the bare-titled record
    only if the year is the same as its TMDB first-air year. "Batman
    (1992)" never goes to the 1966 series. A BARE name attaches to a
    year-suffixed record only if exactly 1 such record exists.

    The result is always the title of the record, not the spelling of
    the file (#480). MariaDB compares titles with no case, no accents,
    and no trailing spaces. Thus, "MONK" finds the record "Monk". The
    dirname, the basename, and the lock key must use "Monk"."""

    existing = TVSeries.query.filter_by(title=title).first()
    if existing is not None:
        return existing.title
    year_form = re.fullmatch(r"(?P<base>.+) \((?P<year>\d{4})\)", title)

    if year_form:
        bare = TVSeries.query.filter_by(title=year_form.group("base")).first()
        if (
            bare is not None
            and bare.tmdb_first_air_date is not None
            and bare.tmdb_first_air_date.year == int(year_form.group("year"))
        ):
            return bare.title
        return title
    candidates = [
        series
        for series in TVSeries.query.filter(
            TVSeries.title.like(f"{title} (____)")
        ).all()
        if re.fullmatch(re.escape(title) + r" \(\d{4}\)", series.title)
    ]
    if len(candidates) == 1:
        return candidates[0].title
    return title


def _parse_movie(basename, tmdb_id, log):
    """Return the details of a film name, or a FilenameRejection.

    An explicit tmdb_id (the TMDB-refresh rename path) outranks an id
    tag in the name."""

    movie_match = MOVIE_NAME_RE.match(basename)
    movie = movie_match or YEARLESS_MOVIE_NAME_RE.match(basename)

    media_library = "Movies"
    title = movie.group("title")
    year = int(movie.group("year")) if movie_match else None

    id_tags, edition = parse_name_tags(movie.group("tags"))
    id_tag = preferred_id_tag(id_tags)

    # The yearless Plex form has meaning only with an id tag. An edition
    # tag alone does not identify the film.

    if year is None and id_tag is None:
        return FilenameRejection(None, "no year and no id tag")

    # If the file quality name is not an expected name, reject the file.

    quality_title = movie.group("quality_title")
    if not _known_quality(quality_title):
        return FilenameRejection(None, "unknown quality")

    identity = _resolve_movie_identity(basename, title, year, id_tag, tmdb_id, log)
    if isinstance(identity, FilenameRejection):
        return identity
    title, year, tmdb_id = identity

    if log:
        current_app.logger.info(f"File: {basename}")
        current_app.logger.info(f"Movie: {title} ({year})")

    # Tag in, tag out. A name that came with an id tag keeps one on its
    # library paths. After the tag resolves, it has the canonical tmdb
    # form, with {edition-...} after it.

    id_tag_text = None
    if id_tag:
        id_tag_text = f"{{tmdb-{tmdb_id}}}" if tmdb_id else id_tag[2]

    extension = movie.group("extension")
    layout = _movie_layout(
        {
            "library": media_library,
            "title": title,
            "year": year,
            "id_tag": id_tag_text,
            "edition": edition,
            "quality_title": quality_title,
            "extension": extension,
        },
        movie.group("version"),
    )
    if isinstance(layout, FilenameRejection):
        return layout
    dirname, plex_title, basename, version, feature_type, fullscreen = layout
    basename = sanitize_filename(unidecode(basename))

    return _details(
        media_library,
        dirname,
        basename,
        plex_title,
        title,
        version,
        quality_title,
        fullscreen,
        extension,
        year=year,
        tmdb_id=tmdb_id,
        feature_type_name=_clean(feature_type),
    )


def _movie_layout(parts, version_text):
    """Return (dirname, plex_title, basename, version, feature_type, fullscreen) of a film.

    parts holds the library_name parts of the film with no version. The
    basename is not sanitized yet. A special feature type in the version
    text puts the file in the folder of that type, with only its own
    name. "Full Screen" goes at the end of the version. A feature type
    with no name after it is a FilenameRejection (#522). It parsed as an
    extra with the edition of its type and the name of the film."""

    edition = parts["edition"]
    feature_type = None
    special_feature = None

    # The folder is the plex title of the film with no version.

    folder, _ = library_name(parts)
    dirname = os.path.join(parts["library"], sanitize_filename(unidecode(folder)))

    if not version_text:
        plex_title, basename = library_name(parts)
        return dirname, plex_title, basename, edition, feature_type, False

    version_strings, fullscreen = _standard_full_screen(version_text)

    # Get a list of the current possible special feature types.

    special_feature_types = db.session.query(RefFeatureType.feature_type).all()
    special_feature_types = [result[0] for result in special_feature_types]

    if fullscreen == True:
        # Move "Full Screen" in the version string. "Full Screen" must go at
        # the end of the version string if there is no special feature type:
        #
        # Fullscreen - Director's Cut
        # - must become -
        # Director's Cut - Full Screen
        #
        # This is because the file is a full screen version of the
        # Director's Cut. It is not a Director's Cut of the full screen
        # version.
        #
        # But do not put "Full Screen" after a special feature type if it is
        # not already there. If you do, you get:
        #
        # Clang Clang Boogie (2019) - Interviews - Full Screen - I Like Salad [Bluray-1080p].mkv
        # - becomes -
        # Clang Clang Boogie (2019)/Interviews/Full Screen - I Like Salad.mkv
        # - but the correct result is -
        # Clang Clang Boogie (2019)/Interviews/I Like Salad.mkv

        # Compare the uppercase versions of the special feature types. Then
        # the case is not important (for example, "Behind the Scenes" and
        # "Behind The Scenes" are the same).

        if not bool(
            set([v.upper() for v in version_strings]).intersection(
                [t.upper() for t in special_feature_types]
            )
        ):
            _put_full_screen_last(version_strings)

    for type in special_feature_types:
        # If the string has a special feature identifier, get all text after
        # the identifier. Use that text as the name of the special feature.

        if type.upper() in [string.upper() for string in version_strings]:
            type_position = [string.upper() for string in version_strings].index(
                type.upper()
            ) + 1
            special_feature = " - ".join(version_strings[type_position:])
            if not special_feature:
                return FilenameRejection(None, f"a {type} extra with no name")
            feature_type = type
            dirname = os.path.join(dirname, sanitize_filename(unidecode(feature_type)))
            break

    # The file name of a special feature is only the special feature. It
    # has no movie title, year, or version. The version string is now the
    # name.

    # An extra of an edition keeps the edition. Else the extras of 2
    # editions with the same name replace each other, and a TMDB rename
    # moves the extra out of its edition folder.

    if special_feature:
        plex_title, basename = library_name(
            dict(parts, feature_type=feature_type, version=special_feature)
        )
        return dirname, plex_title, basename, edition, feature_type, fullscreen

    # "Full Screen" is not part of the version of a film. The name puts
    # it before the quality tag. If the version string is only "Full
    # Screen", report the edition name as the edition, not the raw
    # version.

    if fullscreen:
        version_strings.remove("Full Screen")
    version = " - ".join(version_strings)
    plex_title, basename = library_name(
        dict(parts, version=version or None, fullscreen=fullscreen)
    )
    return dirname, plex_title, basename, version or edition, feature_type, fullscreen


def _resolve_movie_identity(basename, title, year, id_tag, tmdb_id, log):
    """Return the (title, year, tmdb_id) of a film, or a FilenameRejection.

    This is the only network part of the parse, and the only part that
    knows TMDB_UNREACHABLE. An id tag selects the metadata source (#155).
    A tmdb tag is the id itself. An imdb/tvdb tag resolves through the
    library first (a matched movie stores its imdb id), then through the
    TMDB /find endpoint. An id that TMDB definitely does not know is a
    loud reject. A fallback to a title search could attach the wrong
    film. An explicit tmdb_id argument (the TMDB-refresh rename path)
    outranks the tag."""

    tmdb_unreachable = False
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
                if resolved is None:
                    tmdb_unreachable = bool(current_app.config.get("TMDB_API_KEY"))
                tmdb_id = resolved

    # Name the film as TMDB names it, because a film can have alternate
    # titles or spellings. For example:
    # A Fistful of Dynamite == Duck, You Sucker
    # Fifth Avenue Girl == 5th Avenue Girl

    tmdb_result = None
    m = None
    if tmdb_id:
        # A record that already owns this id has the canonical title and
        # year. The network is not necessary.

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
        # If an id tag did not resolve (TMDB not reachable), never run the
        # title search. It could attach the wrong film.

        try:
            if tmdb_id:
                # This reads only the id, the title, and the release date.
                # Thus, it requests no appended blocks.
                params = {}
                url = "/movie/" + str(tmdb_id)
            else:
                params = {"query": title, "primary_release_year": year}
                url = "/search/movie"
            body = tmdb_json(url, params, timeout=30)
            current_app.logger.debug(body)

        except Exception as e:
            # A TMDB API problem must not prevent the import of the file.
            # The exception is a 404 on an id that the filename named. That
            # id does not exist, and a guess would be worse.

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

            # With no API key, a later try cannot do better.

            tmdb_unreachable = bool(current_app.config.get("TMDB_API_KEY"))

        else:
            tmdb_result = body

    if tmdb_result:
        if tmdb_id:
            # /movie/<id> returns the movie object itself, not a results
            # array.
            tmdb_results = [tmdb_result] if tmdb_result.get("id") else None
        else:
            tmdb_results = tmdb_result.get("results")
        if tmdb_results:
            current_app.logger.debug(f"TMDB results: {tmdb_results}")
            tmdb_film = tmdb_results[0]

            # Look for this tmdb_id in the database.

            m = (
                Movie.query.filter_by(tmdb_id=tmdb_film.get("id"))
                .order_by(Movie.date_created.asc())
                .first()
            )

            if log:
                current_app.logger.info(f"Existing movie with this TMDB id: {m}")

            # If it exists, use the existing film title and year. Do not use
            # the parsed values.

            if m:
                title = m.title
                year = m.year

            # If not, use the title and year from TMDB.

            else:
                title = tmdb_film.get("title", title)
                release_date = tmdb_film.get("release_date") or (
                    f"{year}-01-01" if year else None
                )
                if release_date:
                    release_date = datetime.strptime(release_date, "%Y-%m-%d")
                    year = release_date.year

    # The yearless form has no fallback. Without a library record or a
    # reachable TMDB, there is no year to file the movie under.

    # A TMDB outage is not a bad name. The import tries again later.

    if year is None and tmdb_unreachable:
        if log:
            current_app.logger.warning(
                f"'{basename}' has no year, and TMDB could not be reached "
                f"to resolve its id tag"
            )
        return FilenameRejection(TMDB_UNREACHABLE)

    if year is None:
        if log:
            current_app.logger.error(
                f"'{basename}' has no year, and its id tag couldn't be "
                f"resolved to one"
            )
        return FilenameRejection("id not resolvable")

    return title, year, tmdb_id


def iso_639_3_native_language():
    """Return the ISO-639-3 code for the native language.

    MakeMKV uses ISO-639-3 when it writes its MKV files. But the Matroska
    specification calls for ISO-639-*2* bibliographic language codes. The
    codes are the same in most cases, but some languages differ. For
    example, a French MKV from MakeMKV has the 639-3 code "fra" as its
    language code. But the mkvtoolnix tools do not know "fra". They expect
    "fre". If MakeMKV made the file, Fitzflix must convert the native
    language code of the user from 639-2 to 639-3. Then it can check if
    the language exists in the file.
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

    If a volume is not available, return False. Do not raise. A dead mount
    must not turn 1 failure into a cascade. The file stays where it is for
    a later re-import.

    A cross-volume move goes through a hidden name. This function promotes
    the file only after the copy is complete. Thus, a failure during the
    copy can never leave a partial file in the rejects directory under an
    importable name.
    """

    basename = os.path.basename(file_path)
    reject_directory = os.path.join(current_app.config["REJECTS_DIR"], reason)
    destination = os.path.join(reject_directory, basename)
    hidden_destination = hidden(os.path.join(reject_directory, basename), ".partial")

    try:
        # The rejects directory is on the external SSD. The SSD can drop
        # during a long task. Then the file stays in place (#341)

        makedirs_on_mounted_volume(reject_directory)
        try:
            os.rename(file_path, destination)
        except OSError:
            # The destination is a different volume, or the filesystem refused
            # the rename. Copy to the hidden name, promote it, then delete the
            # source. If a step fails, remove both destinations. Then the
            # state is exactly "the source stays where it is". The result is
            # complete or nothing.

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
    """Remove or replace the bad characters in a string and convert it to ASCII."""

    original_string = string

    # The default map of bad to good characters is based on the character
    # replacement of Sonarr:
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

    # Do the unidecode first, because it can add an unwanted character.

    string = unidecode(string)

    # Replace the bad characters with the good characters.

    for i, bad_char in enumerate(bad_characters):
        string = string.replace(bad_char, good_characters[i])

    # Make sure the string is correct for the filesystem.

    string = sanitize_filename(string)

    # Remove duplicate spaces.

    while "  " in string:
        string = string.replace("  ", " ")

    string = string.strip()

    # If the name starts with a period, remove the period. Then the file is
    # not invisible. Use startswith, not string[0]. Then an empty string
    # does not crash.
    if string.startswith("."):
        string = string[1:]

    # Fail loudly. An empty name must not go into the construction of a file
    # name or an S3 key. There it would make degenerate paths.

    if not string:
        raise ValueError(
            f"'{original_string}' sanitizes to an empty string, so it can't be "
            f"used in a file or key name"
        )

    return string


def reconstruct_filename(file_id, series_title=None):
    """Reconstruct the untouched filename from the current details.

    A film gets the current title and year of its movie record. An
    episode gets the current title of its series, or series_title (a
    series rename passes the new title before it commits it). The other
    parts of an episode name stay as the untouched name has them (#362)."""

    f = File.query.filter_by(id=file_id).first()
    if not f:
        return False
    if f.media_library != MOVIES:
        return _reconstruct_episode_name(f, series_title)

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

    # TMDB can send a title with an empty release date. Then the year of
    # the record stands in. The untouched file of a conversion is its
    # original (#292). Its name keeps the quality of the original, not
    # the 1080p quality of the library file. Else a rename moves the
    # archive to the wrong key.

    parts = {
        "library": MOVIES,
        "title": m.title,
        "year": m.year,
        "edition": f.edition,
        "quality_title": f.converted_from_quality or q.quality_title,
        "fullscreen": f.fullscreen == True,
        "extension": ext.lstrip("."),
    }
    if m.tmdb_title != None:
        parts["title"] = m.tmdb_title
        parts["year"] = m.tmdb_release_date.year if m.tmdb_release_date else m.year

    # Tag in, tag out (#155). An untouched name that had a Plex id tag
    # keeps one. If the current tmdb id of the record is known, the tag
    # uses it.

    id_tag = ID_TAG_RE.search(f.untouched_basename)
    if id_tag:
        parts["id_tag"] = f"{{tmdb-{m.tmdb_id}}}" if m.tmdb_id else id_tag.group(0)

    # The untouched name of an extra holds its type and its name as the
    # version. A full screen copy keeps only "Full Screen".

    if f.fullscreen != True and f.feature_type_id != None:
        parts["version"] = f"{ft.feature_type} - {f.plex_title}"

    _, reconstructed_filename = library_name(parts)
    reconstructed_filename = sanitize_filename(reconstructed_filename)
    return " ".join(reconstructed_filename.split()).strip()


def _reconstruct_episode_name(f, series_title=None):
    """Return the untouched name of an episode with the current title of its series.

    The season, the episode numbers, the version, the quality and the
    extension stay as the untouched name has them. An id tag on the
    series part stays. It gets the current TMDB id of the series. A name
    that does not parse stays as it is."""

    match = TV_NAME_RE.match(f.untouched_basename or "")
    series = f.tv_series
    if match is None or series is None:
        return f.untouched_basename

    title = series_title or series.title
    title_tags = re.fullmatch(
        rf"(?P<base>.+?)(?P<tags>(?: {ID_TAG_BLOCK})+)", match.group("title")
    )
    if title_tags:
        tags = f" {{tmdb-{series.tmdb_id}}}" if series.tmdb_id else title_tags["tags"]
        title = f"{title}{tags}"

    _, name = library_name(
        {
            "library": TV_SHOWS,
            "title": title,
            "season": match.group("season"),
            "episode": match.group("episode"),
            "last_episode": match.group("last_episode"),
            "version": match.group("version"),
            "quality_title": match.group("quality_title"),
            "extension": match.group("extension"),
        }
    )
    return sanitize_filename(name)


# The app instance of this process. Fitzflix resolves it lazily. Thus, an
# import of this module from a process that already has an application
# does not make a second one.

app = LocalProxy(get_app)
