"""The jobs of the import chain that wait or run, and the lock of a title."""

import os
import re
import time
from datetime import timedelta
from rq.exceptions import NoSuchJobError
from rq.job import Job
from rq.registry import FinishedJobRegistry
from flask import current_app
from app.tasks import LIVE_JOB_STATES, delete_job, enqueue_task, live_jobs, requeue
from app import safe_job_id
from app.archive.aws_clients import sanitize_s3_key
from app.models import File, title_lock_key
from app.media.importing import filenames

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


# The tasks of the import pipeline, with the queue of each. Each one
# names the file of the import as its source. The source stays in the
# import directory until the cataloging removes it. A conversion
# keeps it there for hours. The Sonarr adopt task comes before the
# localization. It waits for the Sonarr rename of the file and
# then queues the localization. Its third argument is the path of the
# download.

ADOPT_EPISODE_TASK = "app.arr.sonarr_push.adopt_episode_task"

LATER_STAGES = {
    "app.media.sdr_conversion.convert_localized_file": "transcode_queue",
    "app.media.importing.library_move.move_localized_file": "handoff_queue",
    "app.media.importing.cataloging.finalize_localization": "sql_queue",
}

PIPELINE_TASKS = {
    "app.media.importing.localization.localization_task": "import_queue",
    ADOPT_EPISODE_TASK: "request_queue",
    **LATER_STAGES,
}


def _source_of(job):
    """Return the source path that a pipeline job names, or None."""

    if job.func_name == ADOPT_EPISODE_TASK:
        source = job.args[2] if len(job.args) > 2 else None
    elif job.args:
        source = job.args[0]
    else:
        source = job.kwargs.get("file_path") or job.kwargs.get("source_path")
    return source if isinstance(source, str) else None


def pipeline_jobs(names=PIPELINE_TASKS):
    """Yield (job, source path) for each live job of the import pipeline.

    names holds the tasks that count. A job counts when tasks.live_jobs
    yields it. The match is by the source argument, not by the shape of
    the job id. Thus, a retry, a lock wait and a time-tagged id all
    count the same."""

    queues = {getattr(current_app, PIPELINE_TASKS[name]) for name in names}
    for job in live_jobs(queues):
        if job.func_name in names:
            yield job, _source_of(job)


def _same_path(first, second):
    """Return True if 2 path spellings name the same file."""

    return bool(first and second) and os.path.abspath(first) == os.path.abspath(second)


def localization_pending(path):
    """Return True if a localization of this file waits or runs."""

    return any(
        _same_path(source, path)
        for _, source in pipeline_jobs(
            {"app.media.importing.localization.localization_task"}
        )
    )


def later_stage_holds(path):
    """Return True if a conversion, move, or cataloging job still holds this source."""

    return any(_same_path(source, path) for _, source in pipeline_jobs(LATER_STAGES))


def paths_held_by_later_stages():
    """Return each path argument of the conversion, move and cataloging jobs.

    The weekly orphan sweep must not delete the input of such a job. A
    conversion can wait in the transcode queue for more than 7 days."""

    held = set()
    for job, _ in pipeline_jobs(LATER_STAGES):
        for value in list(job.args) + list((job.kwargs or {}).values()):
            if isinstance(value, str):
                held.add(os.path.abspath(value))
    return held


def keys_held_by_pipeline():
    """Return each archive key that a live import pipeline job can upload or hold.

    The import gives its key to its File row only at the cataloging. A
    wait for space has no limit, and a retry can wait for more than 8
    days. The weekly prune must not delete such an upload. A job holds
    the untouched/ key of the name of its source, and the aws_key of
    its file_details argument. The latter is a run-scoped key below
    pending-untouched/ for a same-name re-rip."""

    held = set()
    for job, source in pipeline_jobs():
        if source is not None:
            held.add(
                os.path.join(
                    current_app.config["AWS_UNTOUCHED_PREFIX"],
                    sanitize_s3_key(os.path.basename(source)),
                )
            )
        for value in list(job.args) + list((job.kwargs or {}).values()):
            if isinstance(value, dict) and isinstance(value.get("aws_key"), str):
                held.add(value["aws_key"])
    return held


def _series_title_of_name(basename):
    """Return the title of the series record that an episode name names, or None.

    This is the title rule of the parse, with no TMDB call. An id tag is
    removed. A series record that owns the id of the tag gives its title,
    as in _parse_tv. Thus, "Doctor Who {tmdb-57243}" gives "Doctor
    Who (2005)". A name that is not an episode name gives None."""

    if not filenames.TV_NAME_SEARCH_RE.search(basename):
        return None
    tv = filenames.TV_NAME_RE.match(basename)
    if not tv:
        return None
    title = tv.group("title")
    tagged = re.fullmatch(
        rf"(?P<base>.+?)(?P<tags>(?: {filenames.ID_TAG_BLOCK})+)", title
    )
    if tagged:
        title = tagged.group("base")
        tag = filenames.preferred_id_tag(
            filenames.parse_name_tags(tagged.group("tags"))[0]
        )
        owner = filenames._series_record_of_tag(tag) if tag else None
        if owner is not None:
            title = owner.title
    return filenames._canonical_series_title(title)


def series_import_in_flight(titles):
    """Return the source path of an import of a series that is not done, or None.

    titles holds the series titles that count. Each live pipeline job
    counts, from the Sonarr adopt task to the cataloging. A
    series rename waits for such an import. The import attaches
    the episode to the series by its title at the end."""

    for _, source in pipeline_jobs():
        if source is None:
            continue
        title = _series_title_of_name(os.path.basename(source))
        if title is not None and title in titles:
            return source
    return None


def enqueue_localization(path, at_front=False, quiet=False):
    """Queue the localization of a file once. Return the job, or None.

    The function does nothing if a live pipeline job names the file as
    its source. A wait for space is such a job. Otherwise it
    deletes the finished job of the fixed job id. A removal from its
    registry is not enough. The job hash keeps its expiry, and the new
    job under the id takes it and can expire in the queue. A
    failed job stays in the failed jobs for the sweep. A live job of a
    file of the same name at another path keeps the id too. Then the new
    job gets the id with a time tag.
    With quiet, a skip writes no log line. The import watcher uses it,
    because it sees many events for 1 file."""

    queue = current_app.import_queue
    basename = os.path.basename(path)
    if localization_pending(path) or later_stage_holds(path):
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
    elif old.is_failed or old.get_status(refresh=False) in LIVE_JOB_STATES:
        # A live job under the id imports a file of the same name at a
        # different path.
        job_id = f"{job_id}_{int(time.time())}"
    else:
        delete_job(old)
    return enqueue_task(
        "app.media.importing.localization.localization_task",
        path,
        description=f"'{basename}'",
        at_front=at_front,
        job_id=job_id,
    )


def already_cataloged(file_path):
    """Return True if this file is already the library file at its path.

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

    Return False if another task holds the lock. The caller must
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


# The time that a stage waits when another task holds its title lock

TITLE_LOCK_WAIT = timedelta(minutes=30)


def defer_for_title_lock(func, args, counters, source_path, basename, timeout=None):
    """Run a stage again after TITLE_LOCK_WAIT, with the same lock.

    The lock of the stage expired, and another task took it. The retry
    keeps the old lock token. When the other task releases the lock, the
    extend script of _hold_title_lock takes it again with that token.
    counters holds every attempt counter of the stage. lock_waits counts
    the waits. The queue comes from the task registry. The timeout comes
    from the task registry too, unless the caller gives one."""

    counters = {**counters, "lock_waits": int(counters.get("lock_waits") or 0) + 1}
    requeue(
        func,
        key=source_path,
        delay=TITLE_LOCK_WAIT,
        reason=f"Another task holds the title lock (wait {counters['lock_waits']})",
        counters=counters,
        args=args,
        description=f"'{basename}'",
        timeout=timeout,
    )
