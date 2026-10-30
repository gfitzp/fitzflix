"""Hold the registry of the rq tasks that Fitzflix enqueues by name.

rq stores the job name of each queued job in Redis as a string, for
example "app.videos.localization_task". The key of TASKS is that
string. Its TaskSpec gives the default queue, timeout and description
of the task. enqueue_task fills each one from the spec. Thus, a task
gets the same queue and the same timeout from every producer. An
explicit argument at a call site is an override. Such a site says why
in a comment.

The queue of a spec is a key of the roster of queues. The timeout is
a number of seconds or the name of a config setting, for example
"SQL_TASK_TIMEOUT". The setting is read at enqueue time. The command
of a spec is the CLI command that does the same work, when there is
one. The System page shows it next to the scheduled task.
"""

import importlib
import random
import uuid

from collections import namedtuple
from datetime import timedelta

from flask import current_app

from app import path_tag, safe_job_id
from app.queues import QUEUE_NAME

TaskSpec = namedtuple(
    "TaskSpec", "queue timeout description command", defaults=(None, None)
)

TASKS = {
    # The recurring maintenance jobs of cron_table. Each one runs on the
    # maintenance queue.
    "app.maintenance.rotate_logs": TaskSpec(
        "maintenance", 54000, "Rotating application logs"
    ),
    "app.log_digest.log_digest_task": TaskSpec(
        "maintenance", 600, "Digesting repeated log warnings"
    ),
    "app.maintenance.backup_database": TaskSpec(
        "maintenance", 3600, "Backing up the database"
    ),
    "app.videos.manual_import_task": TaskSpec(
        "maintenance",
        3600,
        "Scanning import directory for files",
        command="flask scan",
    ),
    "app.maintenance.restore_drill": TaskSpec(
        "maintenance", 3600, "Verifying the offsite database backup restores"
    ),
    "app.videos.refresh_criterion_collection_info": TaskSpec(
        "maintenance",
        3600,
        "Refreshing Criterion Collection information",
        command="flask refresh criterion",
    ),
    "app.criterion_catalog.refresh_criterion_releases": TaskSpec(
        "maintenance",
        900,
        "Fetching the Criterion Collection releases from Wikidata",
    ),
    "app.maintenance.cleanup_orphaned_files": TaskSpec(
        "maintenance", 3600, "Cleaning up orphaned partial files"
    ),
    "app.videos.sync_aws_s3_storage_task": TaskSpec(
        "maintenance",
        86400,
        "Syncing files with AWS S3 storage",
        command="flask sync",
    ),
    "app.maintenance.health_probe": TaskSpec(
        "maintenance", 600, "Probing system health"
    ),
    "app.letterboxd.sync_letterboxd_feeds": TaskSpec(
        "maintenance", 900, "Syncing Letterboxd diaries"
    ),
    "app.provider_catalog.refresh_provider_catalogs": TaskSpec(
        "maintenance",
        3600,
        "Discovering provider-catalog films for recommendations",
    ),
    "app.missing_files.missing_best_files_task": TaskSpec(
        "maintenance", 1800, "Scanning for best files that have no local copy"
    ),
    "app.recommendations.recompute_recommendations": TaskSpec(
        "maintenance",
        3600,
        "Recomputing film recommendations",
        command="flask recs recompute",
    ),
    "app.criterion_now.heartbeat_criterion_now": TaskSpec(
        "maintenance", 300, "Checking the Criterion 24/7 poller's pulse"
    ),
    "app.leaving_criterion.refresh_leaving_criterion": TaskSpec(
        "maintenance",
        3600,
        "Refreshing the leaving-Criterion film set",
        command="flask recs leaving",
    ),
    "app.newly_added.refresh_newly_added": TaskSpec(
        "maintenance",
        1800,
        "Refreshing the newly-added streaming feeds",
        command="flask recs newly-added",
    ),
    "app.tmdb_refresh.sync_certification_list": TaskSpec(
        "maintenance", 600, "Syncing the TMDB certification list"
    ),
    "app.awards.refresh_awards": TaskSpec(
        "maintenance",
        7200,
        "Refreshing film awards from Wikidata",
        command="flask recs awards",
    ),
    "app.streaming_rail.recompute_streaming_rail": TaskSpec(
        "maintenance",
        3600,
        "Recomputing the streaming rail",
        command="flask recs streaming",
    ),
    "app.estimate_warm.warm_estimates": TaskSpec(
        "maintenance", 3600, "Pre-warming estimate payloads"
    ),
    "app.frames.refresh_frame_pool_task": TaskSpec(
        "maintenance",
        3600,
        "Refreshing the Name That Frame pool",
        command="flask frames refresh",
    ),
    "app.tmdb_changes.refresh_changed_records": TaskSpec(
        "maintenance", 1800, "Refreshing TMDB-changed records"
    ),
    "app.tmdb_refresh.refresh_in_production_tv": TaskSpec(
        "maintenance", 3600, "Refreshing in-production TV series"
    ),
    "app.streaming.refresh_availability": TaskSpec(
        "maintenance", 3600, "Refreshing streaming availability"
    ),
    "app.availability_alerts.notify_watchlist_availability": TaskSpec(
        "maintenance",
        1800,
        "Checking watchlisted films for new availability",
        command="flask alerts availability",
    ),
    "app.videos.sqs_retrieve_task": TaskSpec(
        "maintenance",
        7200,
        "Polling AWS SQS for files to download",
        command="flask sqs",
    ),
    "app.videos.plex_history_poll": TaskSpec(
        "maintenance", 900, "Polling Plex for watch history"
    ),
    "app.plex_titles.sync_plex_episode_titles": TaskSpec(
        "maintenance", 1800, "Syncing episode titles into Plex"
    ),
    "app.plex_library.refresh_plex_libraries": TaskSpec(
        "maintenance", 600, "Refreshing Plex libraries"
    ),
    "app.plex_collections.sync_criterion_collection": TaskSpec(
        "maintenance", 600, "Syncing the Criterion collections into Plex"
    ),
    "app.plex_watchlist.sync_plex_watchlist": TaskSpec(
        "maintenance", 900, "Syncing the Plex watchlist"
    ),
    "app.radarr_push.reconcile_radarr_paths": TaskSpec(
        "maintenance", 900, "Checking the Radarr movie paths"
    ),
    "app.series_rename.reconcile_sonarr_paths": TaskSpec(
        "maintenance", 900, "Checking the Sonarr series paths"
    ),
    "app.series_rename.reconcile_sonarr_series": TaskSpec(
        "maintenance", 900, "Checking the Sonarr path of a series"
    ),
    "app.dvr.build_channel_lineups": TaskSpec(
        "maintenance", 3600, "Building virtual DVR channel lineups"
    ),
    # Other maintenance work that the pages and the tasks queue.
    "app.streaming.warm_title_availability": TaskSpec("maintenance", 1800),
    "app.criterion_now.poll_criterion_now": TaskSpec("maintenance", 300),
    "app.plex_library.analyze_plex_media": TaskSpec("maintenance", 1800),
    # The import pipeline.
    "app.videos.localization_task": TaskSpec("import", "LOCALIZATION_TASK_TIMEOUT"),
    "app.videos.mkvmerge_task": TaskSpec("import", "LOCALIZATION_TASK_TIMEOUT"),
    "app.atmos.atmos_supplement_task": TaskSpec("import", "TRANSCODE_TASK_TIMEOUT"),
    "app.videos.move_localized_file": TaskSpec("handoff", "MOVE_TASK_TIMEOUT"),
    "app.videos.aws_delete": TaskSpec("handoff", "FILE_TASK_TIMEOUT"),
    "app.atmos.atmos_check_task": TaskSpec("handoff", 600),
    "app.atmos.atmos_finish_task": TaskSpec("handoff", "TRANSCODE_TASK_TIMEOUT"),
    "app.videos.convert_localized_file": TaskSpec(
        "transcode", "TRANSCODE_TASK_TIMEOUT"
    ),
    "app.videos.remux_audio_plan_task": TaskSpec("transcode", "TRANSCODE_TASK_TIMEOUT"),
    "app.triage.generate_triage_snapshots": TaskSpec("transcode", 7200),
    "app.triage.generate_audio_comparison": TaskSpec("transcode", 7200),
    "app.frames.extract_frame_task": TaskSpec("transcode", 600),
    "app.frames.replace_frame_task": TaskSpec("transcode", 600),
    # The file operations.
    "app.videos.upload_task": TaskSpec("file", "UPLOAD_TASK_TIMEOUT"),
    # A move is 1 server-side copy (#369). For 100 GiB, s3transfer sends
    # 6,400 UploadPartCopy requests of 16 MiB, 10 at a time. The estimate
    # at 50 MB/s for each request is 4 minutes. FILE_TASK_TIMEOUT
    # (10 minutes) leaves too small a margin for a slow region. Thus, the
    # move has the budget of an upload.
    "app.aws_storage.move_archive_object": TaskSpec(
        "file", "UPLOAD_TASK_TIMEOUT", "Moving an archive to its new key"
    ),
    "app.videos.archive_held_task": TaskSpec("file", "UPLOAD_TASK_TIMEOUT"),
    "app.aws_storage.move_archive_to_modified_task": TaskSpec(
        "file", "UPLOAD_TASK_TIMEOUT"
    ),
    "app.videos.download_task": TaskSpec("file", "TRANSCODE_TASK_TIMEOUT"),
    "app.aws_storage.place_restored_import_task": TaskSpec("file", "FILE_TASK_TIMEOUT"),
    "app.videos.mkvpropedit_task": TaskSpec("file", "MKVPROPEDIT_TASK_TIMEOUT"),
    "app.videos.track_metadata_scan_task": TaskSpec("file", "MKVPROPEDIT_TASK_TIMEOUT"),
    "app.series_rename.rename_tv_series_task": TaskSpec("file", "SQL_TASK_TIMEOUT"),
    "app.manual_imports.manual_import_task": TaskSpec(
        "file", "LOCALIZATION_TASK_TIMEOUT"
    ),
    # The database writes.
    "app.videos.finalize_localization": TaskSpec("sql", "SQL_TASK_TIMEOUT"),
    "app.videos.apply_tmdb_refresh": TaskSpec("sql", "SQL_TASK_TIMEOUT"),
    "app.videos.apply_plex_watch": TaskSpec("sql", "SQL_TASK_TIMEOUT"),
    "app.videos.apply_letterboxd_import": TaskSpec("sql", "SQL_TASK_TIMEOUT"),
    "app.videos.save_track_metadata": TaskSpec("sql", "SQL_TASK_TIMEOUT"),
    "app.videos.track_metadata_scan_library": TaskSpec("sql", "SQL_TASK_TIMEOUT"),
    "app.videos.review_task": TaskSpec("sql", "SQL_TASK_TIMEOUT"),
    # The work that a user or an outside service asks for.
    "app.videos.refresh_tmdb_info": TaskSpec("request", "SQL_TASK_TIMEOUT"),
    "app.videos.aws_restore": TaskSpec("request", "SQL_TASK_TIMEOUT"),
    "app.videos.letterboxd_import_task": TaskSpec(
        "request", "LOCALIZATION_TASK_TIMEOUT"
    ),
    "app.infuse_player.pair_task": TaskSpec("request", 240),
    "app.radarr_push.adopt_download_task": TaskSpec("request", 300),
    "app.radarr_push.follow_import_move_task": TaskSpec("request", 300),
    "app.radarr_push.follow_rename_task": TaskSpec("request", 300),
    "app.sonarr_push.adopt_episode_task": TaskSpec("request", 300),
}


def report_progress(job, name, activity, percent=-1):
    """Show the activity of a running job on the queue page.

    The description is the name in single quotes, a dash, and the
    activity. With no name, it is only the activity. A percent of None
    keeps the progress that the job has. A call with no job does
    nothing. Thus, a task can call it outside a worker."""

    if not job:
        return
    job.meta["description"] = activity if name is None else f"'{name}' — {activity}"
    if percent is not None:
        job.meta["progress"] = percent
    job.save_meta()


def task_timeout(spec, config=None):
    """Return the timeout of a spec in seconds.

    A config-setting name is read from config, or from the config of
    the current app."""

    if isinstance(spec.timeout, str):
        return (config if config is not None else current_app.config)[spec.timeout]
    return spec.timeout


def resolve_queue(queue):
    """Return the queue object of a roster key. A queue object passes through."""

    if isinstance(queue, str):
        if queue not in QUEUE_NAME:
            raise KeyError(f"unknown queue {queue!r}")
        return getattr(current_app, f"{queue}_queue")
    return queue


def enqueue_task(
    name,
    *args,
    delay=None,
    at_front=False,
    queue=None,
    timeout=None,
    description=None,
    job_id=None,
    meta=None,
    result_ttl=None,
    **kwargs,
):
    """Queue the task called name with its default queue, timeout and description.

    The positional and the other keyword arguments go to the task. delay
    is a timedelta or a number of seconds. A job with a delay goes to
    the ScheduledJobRegistry. queue is a roster key or a queue object.
    Each explicit argument overrides the spec. The return value is the
    rq job."""

    return _submit(
        name,
        args,
        kwargs,
        delay=delay,
        at_front=at_front,
        queue=queue,
        timeout=timeout,
        description=description,
        job_id=job_id,
        meta=meta,
        result_ttl=result_ttl,
    )


def _submit(
    name,
    args,
    kwargs,
    delay=None,
    at_front=False,
    queue=None,
    timeout=None,
    description=None,
    job_id=None,
    meta=None,
    result_ttl=None,
):
    """Queue 1 job. The task arguments are a tuple and a dict here.

    Thus, a task argument can have the name of an option."""

    spec = TASKS[name]
    target = resolve_queue(queue if queue is not None else spec.queue)
    options = {
        "args": tuple(args),
        "kwargs": dict(kwargs),
        "job_timeout": timeout if timeout is not None else task_timeout(spec),
    }
    if description is None:
        description = spec.description
    if description is not None:
        options["description"] = description
    if job_id is not None:
        options["job_id"] = job_id
    if meta is not None:
        options["meta"] = meta
    if result_ttl is not None:
        options["result_ttl"] = result_ttl
    # rq keeps the result stream of a failure with no expiry unless the
    # job has a failure_ttl (#478)
    options["failure_ttl"] = FAILURE_TTL
    if at_front:
        options["at_front"] = True
    if delay is None:
        return target.enqueue(name, **options)
    if not isinstance(delay, timedelta):
        delay = timedelta(seconds=delay)
    return target.enqueue_in(delay, name, **options)


# A failed job and its result stream expire after 30 days (#478). Without
# a failure_ttl, rq 2 keeps the stream of each failure forever.

FAILURE_TTL = 30 * 86400


def delete_job(job):
    """Delete an rq job together with its result stream (#478).

    Job.delete removes the job hash, but not the rq:results stream of
    the job. A stream with no job grows the database forever."""

    from rq.results import Result

    Result.delete_all(job)
    job.delete()


# The states of a job that still waits or runs.

LIVE_JOB_STATES = ("queued", "started", "scheduled", "deferred")


def task_func_names(name):
    """Return each func_name that rq can give a job of the task called name (#456).

    A job that enqueue_task queues has the name of the registry. The
    scheduler queues the function object. Then rq names the job after
    the module that defines the function. For a function that app.videos
    exports from another module, that is a different name."""

    names = {name}
    module_name, attribute = name.rsplit(".", 1)
    try:
        func = getattr(importlib.import_module(module_name), attribute)
    except (ImportError, AttributeError):
        return names
    names.add(f"{func.__module__}.{func.__qualname__}")
    return names


def live_job_ids(queue):
    """Return the ids of the jobs of a queue that wait or run (#616).

    This is the one definition of a live job. A live job is in the queue
    list, the started jobs or the scheduled jobs. It can also be in the
    intermediate queue of rq, the gap between the list and the worker
    (#523). A retry or a defer waits in the scheduled jobs. queue is a
    roster key or a queue object."""

    from rq.registry import ScheduledJobRegistry, StartedJobRegistry

    queue = resolve_queue(queue)
    ids = list(queue.job_ids)
    ids.extend(queue.intermediate_queue.get_job_ids())
    ids.extend(StartedJobRegistry(queue=queue).get_job_ids())
    ids.extend(ScheduledJobRegistry(queue=queue).get_job_ids())
    return list(dict.fromkeys(ids))


def live_jobs(queues):
    """Yield each job of the queues that waits or runs (#616).

    A job is live if and only if this yields it. The ids come from
    live_job_ids. The job hashes of each queue come in 1 round trip."""

    from rq.job import Job

    seen = set()
    for queue in queues:
        queue = resolve_queue(queue)
        if queue.name in seen:
            continue
        seen.add(queue.name)
        for job in Job.fetch_many(live_job_ids(queue), connection=queue.connection):
            if job is not None:
                yield job


def live_task_job(name):
    """Return a job of the task called name that waits or runs on its queue, or None.

    The Run now button of the System page asks this before it queues a
    scheduled task. A second copy of a maintenance task behind the first
    one does the same work 2 times (the import sweep, the S3 sync)."""

    names = task_func_names(name)
    for job in live_jobs([TASKS[name].queue]):
        if (job.meta or {}).get("func") == name or job.func_name in names:
            return job
    return None


def start_task_run(name):
    """Return True if a Run now of the task called name can queue it (#456).

    A short gate in Redis covers the time between the check and the
    enqueue. Thus, 2 posts at the same time to 2 web workers cannot both
    pass. The caller calls finish_task_run after the enqueue."""

    gate = _bulk_gate(f"run-now:{name}")
    if not current_app.redis.set(gate, "1", nx=True, ex=BULK_GATE_SECONDS):
        return False
    if live_task_job(name) is not None:
        current_app.redis.delete(gate)
        return False
    return True


def finish_task_run(name):
    """Release the Run now gate of the task called name after its enqueue."""

    current_app.redis.delete(_bulk_gate(f"run-now:{name}"))


def free_job_id(name, job_id):
    """Make the fixed id of a task free for a new job. Return False if a job under it is live.

    A finished, failed, stopped or canceled job under the id is deleted,
    with its registry entry. A new job under the same id must not stay
    in the FailedJobRegistry, and must not take the expiry of the old
    hash (#371, #438). A job that waits or runs keeps the id."""

    from rq.exceptions import NoSuchJobError
    from rq.job import Job

    queue = resolve_queue(TASKS[name].queue)
    try:
        old = Job.fetch(job_id, connection=queue.connection)
    except NoSuchJobError:
        return True
    if old.get_status(refresh=False) in LIVE_JOB_STATES:
        return False
    delete_job(old)
    return True


# A bulk post of a maintenance page takes this gate for a short time.
# Thus, a double click cannot pass the queue check two times (#439).

BULK_GATE_KEY = "fitzflix:bulk-gate:{}"
BULK_GATE_SECONDS = 60


def bulk_job_id(prefix):
    """Return a new job id for 1 job of a bulk run. Each id is used only 1 time."""

    return f"{prefix}{uuid.uuid4().hex[:12]}"


def bulk_run_pending(prefix, *names):
    """Return True if a job with an id that starts with prefix waits or runs.

    prefix is a string or a tuple of strings. names are the tasks of the
    run. The check reads the live job ids of the queue of each task."""

    queues = {TASKS[name].queue for name in names}
    return any(
        job_id.startswith(prefix) for queue in queues for job_id in live_job_ids(queue)
    )


def start_bulk_run(prefix, *names):
    """Return True if a new bulk run can start. Return False if one waits or runs (#439).

    A run is pending while a job of it waits or runs. The gate covers
    the time that a post takes to queue its jobs."""

    gate = _bulk_gate(prefix)
    if not current_app.redis.set(gate, "1", nx=True, ex=BULK_GATE_SECONDS):
        return False
    if bulk_run_pending(prefix, *names):
        current_app.redis.delete(gate)
        return False
    return True


def finish_bulk_run(prefix):
    """Release the gate of a bulk post after it queued its jobs (follow-up #439).

    The queued jobs now keep a second post out. A run that fails at once
    can then start again at once, not after BULK_GATE_SECONDS."""

    current_app.redis.delete(_bulk_gate(prefix))


def _bulk_gate(prefix):
    """Return the Redis key of the gate of a bulk run."""

    return BULK_GATE_KEY.format(prefix if isinstance(prefix, str) else prefix[0])


def requeue_job_id(name, key=None, counters=None, tag=None):
    """Return the id of a retry of the task called name.

    The id is "retry", the short task name, a digest of key, and each
    counter as name=value in name order. Each attempt of a chain thus
    gets an id of its own. The digest keeps apart 2 files with the same
    basename (#338). A retry with no key and no
    counters has a fixed id, and a repeat defer replaces it. tag goes
    after the key digest. A track edit gives the digest of its arguments
    there. Then 2 edits of 1 file retry apart (#595)."""

    parts = ["retry", name.rsplit(".", 1)[-1]]
    if key is not None:
        parts.append(path_tag(key))
    if tag is not None:
        parts.append(tag)
    parts.extend(
        f"{counter}={value}" for counter, value in sorted((counters or {}).items())
    )
    return safe_job_id(":".join(parts))


def _wait_text(delay):
    """Return a delay as words for the log, for example "5 minutes"."""

    seconds = int(delay.total_seconds())
    if seconds % 60:
        return f"{seconds} seconds"
    minutes = seconds // 60
    return "1 minute" if minutes == 1 else f"{minutes} minutes"


def requeue(
    name,
    *,
    key,
    delay,
    reason,
    counters=None,
    args=(),
    kwargs=None,
    description=None,
    queue=None,
    timeout=None,
    job_id=None,
    meta=None,
    level="warning",
):
    """Put a task back on its queue after a delay. Return the delay.

    This is the 1 retry path of the tasks. delay is a timedelta or a
    (low, high) range of minutes for a random delay. counters holds
    every attempt counter of the task. They go into the task kwargs and
    into the job id. Thus, a retry cannot drop a counter (#351). The
    queue, the timeout and the description come from TASKS unless the
    caller gives them. job_id replaces the id from requeue_job_id. The
    result ttl of 1 day keeps the record of a finished retry alive. The
    function writes 1 log line at the given level. A caller that wrote a
    more detailed line itself gives level=None."""

    if isinstance(delay, tuple):
        delay = timedelta(minutes=random.randint(*delay))
    task_kwargs = {**(kwargs or {}), **(counters or {})}
    if job_id is None:
        job_id = requeue_job_id(name, key, counters)
    if level:
        label = description or TASKS[name].description or name.rsplit(".", 1)[-1]
        getattr(current_app.logger, level)(
            f"{label} {reason}, returning to queue after {_wait_text(delay)}"
        )
    _submit(
        name,
        args,
        task_kwargs,
        delay=delay,
        queue=queue,
        timeout=timeout,
        description=description,
        job_id=job_id,
        meta=meta,
        result_ttl=86400,
    )
    return delay
