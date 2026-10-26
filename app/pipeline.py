"""Record a pipeline trail for each file.

Each file that moves through the import pipeline leaves 1 ordered trail
in Redis. The stages are Localizing, Moving into the library,
Cataloging, and Archiving to S3, plus SDR conversions, remuxes, and
restores. Thus, the queue page can answer "where is my file now"
without the worker logs.

The state updates come from JOB LIFECYCLE HOOKS, not from code in the
task bodies. TrackedQueue records "queued" or "scheduled" when a job is
enqueued. A deferred retry shows as "scheduled". PipelineWorker records
"started", "done", or "failed" around the execution. The STAGES
registry derives the file of a job from the function name and the
arguments of the job. Tasks that the registry does not know are not
trails (SQL refreshes, maintenance sweeps). The 1 permitted in-task
emitter is record_task_stage. It is for a phase that has no job
boundary of its own (the copy to the staging directory during
localization). It also derives the file from the current job through
the registry.

The recording must never break the pipeline. Each hook catches and logs
its own failures. A trail is only advisory display state. The TTL is 7
days. This matches the SQL window of the File Activity page. Thus, a
card that arrived keeps its chips while it stays on the page. Fitzflix
keeps the newest 100 files.

Trails are keyed by basename. The pipeline can RENAME a file while it
is in progress. The parse makes titles canonical against existing
series. The container conversion changes the extension to .mkv. Then
the localization calls migrate_trail. That merges the trail under the
new name and leaves an alias. Thus, 1 file stays 1 trail. It does not
become 2 trails that fight over the same File Activity card.
"""

import functools
import hashlib
import json
import logging
import os
import time
import traceback

from collections import namedtuple
from datetime import datetime, timezone

from rq import Queue, SimpleWorker
from rq.timeouts import BaseTimeoutException

FILE_KEY = "fitzflix:pipeline:file:{digest}"
ALIAS_KEY = "fitzflix:pipeline:alias:{digest}"

# A job whose chips a later job of the same run continues (the Atmos
# twin). rq removes the hash of a finished job after its result TTL.
# Without this key, the healing removed the open chips of the run while
# MediaConvert encoded.

CONTINUES_KEY = "fitzflix:pipeline:continues:{job_id}"
ACTIVE_KEY = "fitzflix:pipeline:active"
TRAIL_TTL_SECONDS = 7 * 86400
ACTIVE_LIMIT = 100

# The waiting jobs that an admin moved to the front of their queue
# (#236). The score is the time of the move. The queue page lists these
# jobs first, the newest move at the top. A worker removes the entry
# when it starts the job.

FRONT_KEY = "fitzflix:queue:front"
FRONT_TTL_SECONDS = 30 * 86400

# Move 1 waiting job to the head of its rq queue in 1 atomic step. rq
# pops from the head. A remove and a push in 2 round trips can lose the
# job if the second step fails. The result is the old index of the job,
# or -1 when the job does not wait in the queue.

_MOVE_TO_FRONT = """
local index = redis.call('LPOS', KEYS[1], ARGV[1])
if not index then
    return -1
end
if index > 0 then
    redis.call('LREM', KEYS[1], 0, ARGV[1])
    redis.call('LPUSH', KEYS[1], ARGV[1])
end
redis.call('ZADD', KEYS[2], ARGV[2], ARGV[1])
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', ARGV[2] - ARGV[3])
return index
"""


def _basename_from_path(args, kwargs):
    """Return the basename of the file from a leading path argument.

    A retry passes the path as the keyword file_path. Without it, a
    waiting retry left no chip on the trail (#262 review)."""

    path = args[0] if args else (kwargs or {}).get("file_path")
    return os.path.basename(path) if path else None


def _basename_from_details(args, kwargs):
    """Return the basename inside a file_details dict (second argument)."""

    if len(args) > 1 and isinstance(args[1], dict):
        return args[1].get("basename")
    return None


def _basename_from_second_arg(args, kwargs):
    """Return a literal basename passed second (the download_task signature)."""

    return args[1] if len(args) > 1 else None


def _basename_from_file_id(args, kwargs):
    """Return the basename of the File record found by a leading file_id.

    Enqueue-side hooks already run inside an app context. This includes
    the context of the test app. Never trust the get_app() singleton in
    tests. Worker-side hooks run outside an app context. Thus, use the
    own app of the worker as the fallback.
    """

    if not args:
        return None
    from flask import current_app

    from app import db

    try:
        flask_app = current_app._get_current_object()
    except RuntimeError:
        from app import get_app

        flask_app = get_app()
    from app.models import File

    with flask_app.app_context():
        file = db.session.get(File, args[0])
        return file.basename if file else None


# Each per-file pipeline task, by rq function name. The value is the
# stage label that the trail shows, and a function. The function
# derives the basename of the file from the arguments of the job. A
# task that is not listed leaves no trail.

STAGES = {
    "app.videos.localization_task": ("Localizing", _basename_from_path),
    "app.videos.move_localized_file": (
        "Moving into the library",
        _basename_from_details,
    ),
    "app.videos.convert_localized_file": (
        "Converting to SDR",
        _basename_from_details,
    ),
    "app.videos.finalize_localization": ("Cataloging", _basename_from_details),
    "app.videos.upload_task": ("Archiving to S3", _basename_from_file_id),
    "app.videos.rearchive_untouched_object": (
        "Re-archiving to S3",
        _basename_from_file_id,
    ),
    "app.aws_storage.move_archive_object": (
        "Moving the S3 archive",
        _basename_from_file_id,
    ),
    "app.videos.track_metadata_scan_task": (
        "Scanning track metadata",
        _basename_from_file_id,
    ),
    "app.videos.mkvpropedit_task": ("Setting track flags", _basename_from_file_id),
    "app.videos.mkvmerge_task": ("Remuxing", _basename_from_file_id),
    "app.videos.remux_audio_plan_task": (
        "Rebuilding audio",
        _basename_from_file_id,
    ),
    "app.videos.download_task": ("Restoring from S3", _basename_from_second_arg),
    "app.atmos.atmos_supplement_task": ("Adding Atmos twin", _basename_from_file_id),
}


# The later jobs of a multi-job run. They leave no chips of their own.
# They write to the chips of the first job. But the queue page sorts and
# dates their rows by the anchors of the file. Thus, first_run and
# first_enqueued find the file of these jobs here.

CONTINUATIONS = {
    "app.atmos.atmos_check_task": lambda args, kwargs: args[0]["basename"],
    "app.atmos.atmos_finish_task": lambda args, kwargs: args[0]["basename"],
}


# The own chips of jobs that record all of their work as TaskPhases
# chips. While the job waits or runs, its own chip shows that more work
# comes. When the job is done, its phase chips tell the full story.
# Thus, pipeline_trails hides the own chip when it is done (Incredibles
# 2, 2026-10-15). A failed own chip stays red.

PHASED_STAGES = frozenset({"Adding Atmos twin"})


def _file_of(job):
    """Return the basename of the file of a pipeline or continuation job, or None."""

    found = _stage_for(job)
    if found is not None:
        return found[0]
    extractor = CONTINUATIONS.get(job.func_name)
    if extractor is None:
        return None
    try:
        return extractor(job.args or (), job.kwargs or {})
    except (IndexError, KeyError, TypeError):
        return None


def _stage_for(job):
    """Return (basename, stage label) for a pipeline job, or None."""

    entry = STAGES.get(job.func_name)
    if entry is None:
        return None
    label, extractor = entry
    basename = extractor(job.args or (), job.kwargs or {})
    if not basename:
        return None
    return basename, label


def _digest(basename):
    """Return the stable Redis key fragment for the trail of 1 file."""

    return hashlib.sha1(basename.encode("utf-8", "replace")).hexdigest()[:16]


def _decode_trail(raw):
    """Return the trail list from its stored JSON, or a new empty list."""

    return json.loads(raw) if raw else []


def _resolve_basename(connection, basename):
    """Return the current identity of the basename.

    This follows the rename alias that migrate_trail leaves. The number
    of steps is limited, in case renames chain. For example, a
    canonical title can get a container conversion later."""

    for _ in range(4):
        value = connection.get(ALIAS_KEY.format(digest=_digest(basename)))
        if not value:
            break
        value = value.decode() if isinstance(value, bytes) else value
        if value == basename:
            break
        basename = value
    return basename


def _rerun_start(trail, index):
    """Return the position where the earlier run of a job starts.

    The sub-stages of a job sit directly before its own entry. They
    share its id. Thus, the earlier run starts at the first entry of
    that contiguous group."""

    job_id = trail[index].get("job")
    while index > 0 and trail[index - 1].get("job") == job_id:
        index -= 1
    return index


# 1 stage event for _apply_stage_event. now is the local wall-clock stamp
# of the chips. utc_now is the UTC stamp of first_enqueued, for an event
# with no anchor.

StageEvent = namedtuple(
    "StageEvent",
    "stage status job_id before_job sibling enqueued_at anchor now utc_now",
)


def _text(value):
    """Return a Redis hash value as text, or None."""

    return value.decode() if isinstance(value, bytes) else value


def _write_trail_entry(
    connection,
    basename,
    stage,
    status,
    job_id,
    before_job=False,
    sibling=None,
    enqueued_at=None,
    anchor=None,
):
    """Apply 1 stage event to the trail of the file atomically.

    Two workers touch the same trail at a stage handoff. The move job
    starts at the instant that the localization task enqueues it. Thus,
    the "started" stamp of the file-operation worker races the "done"
    stamp of the import worker for the stage before it. With a plain
    read-modify-write, the second write erases the update of the first.
    The trails revisit froze 2 files at "Localizing · running" for
    ever. Thus, the write WATCHes the trail key. If the key changed, the
    write reads again and retries. The retries are limited. The trail
    is advisory. It is never worth a stalled worker.

    The localization job id comes from the basename. Thus, a re-import
    of the same file runs the same job id again. Its stamp finds the
    settled entry of the earlier run. That entry and all entries after
    it are from the earlier run. Thus, the write removes them, and the
    new run starts a new journey. Without this, the chips of the
    earlier run (Moved into the library, Cataloged) stayed green after
    the new Localizing chip. enqueued_at is the enqueue time of the job.
    It tells a new enqueue apart from a late enqueue stamp.

    anchor is the same time in UTC, the format of first_enqueued. A free
    worker can start a job before its "queued" stamp arrives. The late
    stamp is then dropped. Before, its enqueue time went with it. The
    trail had no first_enqueued, and the next enqueue of the journey
    (the library copy) filled it. Thus, the Enqueued column showed the
    end of the localization (Gandhi, How to Train Your Dragon The Hidden
    World, Oct 2026). Now each stamp can fill a missing first_enqueued
    from anchor.

    The rules are in _apply_stage_event. This function reads the trail
    under WATCH, applies the event, and writes the result in 1 MULTI.
    """

    from redis import WatchError

    basename = _resolve_basename(connection, basename)
    digest = _digest(basename)
    key = FILE_KEY.format(digest=digest)

    for _ in range(10):
        event = StageEvent(
            stage,
            status,
            job_id,
            before_job,
            sibling,
            enqueued_at,
            anchor,
            now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            utc_now=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        )
        with connection.pipeline() as pipe:
            try:
                pipe.watch(key)
                trail = _decode_trail(pipe.hget(key, "trail"))
                first_run = _text(pipe.hget(key, "first_run"))
                first_enqueued = _text(pipe.hget(key, "first_enqueued"))
                trail, new_first_run, new_first_enqueued, changed = _apply_stage_event(
                    trail, first_run, first_enqueued, event
                )

                # An event that leaves the trail as it is writes at most a
                # missing first_enqueued.

                if not changed:
                    if new_first_enqueued != first_enqueued:
                        pipe.multi()
                        pipe.hset(key, "first_enqueued", new_first_enqueued)
                        pipe.execute()
                    return

                pipe.multi()
                if new_first_run is None and first_run is not None:
                    pipe.hdel(key, "first_run")
                elif new_first_run != first_run:
                    pipe.hset(key, "first_run", new_first_run)
                if new_first_enqueued != first_enqueued:
                    pipe.hset(key, "first_enqueued", new_first_enqueued)
                pipe.hset(
                    key,
                    mapping={
                        "basename": basename,
                        "trail": json.dumps(trail),
                        "updated": event.now,
                    },
                )
                pipe.expire(key, TRAIL_TTL_SECONDS)
                pipe.zadd(ACTIVE_KEY, {digest: time.time()})
                pipe.zremrangebyrank(ACTIVE_KEY, 0, -(ACTIVE_LIMIT + 1))
                pipe.execute()
                return
            except WatchError:
                continue


def _apply_stage_event(trail, first_run, first_enqueued, event):
    """Apply 1 StageEvent to a trail. Return (trail, first_run, first_enqueued, changed).

    This function is pure. It copies the trail and changes only the copy.
    changed is False when the trail stays as it is. Then only
    first_enqueued can change. first_run and first_enqueued are the
    stamps of the file, or None."""

    stage, status, job_id = event.stage, event.status, event.job_id
    before_job, sibling, enqueued_at = (
        event.before_job,
        event.sibling,
        event.enqueued_at,
    )
    now = event.now
    trail = [dict(entry) for entry in trail]
    fresh_journey = False

    # A job that moves through its lifecycle updates its own entry. Thus,
    # queued, started, and done are 1 line, not 3. A re-enqueued retry is
    # a NEW job id. Thus, it appends a new entry, and the earlier failure
    # stays visible. A sub-stage from a task shares the id of its job. It
    # carries its own stage label. Thus, it is its own line.

    rerun = False
    append = True
    for position in range(len(trail) - 1, -1, -1):
        entry = trail[position]
        if entry.get("job") == job_id and entry.get("stage") == stage:
            # The same job id runs again after its earlier run settled. The
            # worker stamps "started" again, or the queue stamps a new
            # enqueue that comes after the settled stamp. Then the earlier
            # run leaves the trail.
            if not before_job and entry.get("status") in (
                "done",
                "failed",
                "deferred",
            ):
                if status == "started" or (
                    status in ("queued", "scheduled")
                    and enqueued_at is not None
                    and entry.get("at", "") < enqueued_at
                ):
                    del trail[_rerun_start(trail, position) :]
                    rerun = True
                    break

            # A job that booked its own retry is "deferred". Its worker
            # still stamps it done when it returns. The retry chip already
            # stands for the stage. Thus, the stamp of the worker changes
            # nothing.

            if entry.get("status") == "deferred" and status in ("done", "failed"):
                return trail, first_run, first_enqueued, False

            # A job can complete before its own enqueue-side stamp is
            # written. The deferred re-archive skips a superseded key in
            # milliseconds. A late "queued" would pull the chip back and
            # freeze it there. The enqueue side never overrides a stamp that
            # the worker already made.
            if status in ("queued", "scheduled") and entry.get("status") in (
                "started",
                "done",
                "failed",
                "deferred",
            ):
                # Keep the enqueue time of the dropped stamp. An anchor that
                # is set never moves.

                return (
                    trail,
                    first_run,
                    first_enqueued or event.anchor or event.utc_now,
                    False,
                )
            entry["status"] = status
            entry["at"] = now
            append = False
            break

    if append:
        # A sub-stage reports preparatory work inside its job. The staging
        # copy comes before the localization itself. Thus, the sub-stage
        # goes BEFORE the own entry of the job. Then the chips read in
        # pipeline order, not in the order of the first stamp. The
        # job-level entry always wins that order, because it exists from
        # the enqueue time (Glenn, 2026-08).

        # A waiting stamp can arrive on an empty or fully settled trail.
        # Then it opens a NEW journey through the pipeline (a re-import, or
        # a later re-archive of a file that arrived). Its enqueue anchor
        # starts again. A retry after a failure continues the same journey.
        # Thus, the anchor holds.

        if rerun:
            fresh_journey = True
        elif status in ("queued", "scheduled", "started") and not before_job:
            fresh_journey = not trail or all(
                existing.get("status") in ("done", "deferred") for existing in trail
            )

        # A running job that books a new job of its own stage for the same
        # file is deferring itself. Examples are a file that is still
        # copying in, a title lock that another task holds, and a transient
        # copy error. The running job then returns without error, and its
        # worker stamped it done. A "Localized" chip showed before
        # "Localizing · waiting to retry" (NOVA, 2026-10-15). Now the
        # running entry is "deferred". The trail does not show it. The
        # retry chip stands for the stage.

        if status in ("queued", "scheduled") and not before_job:
            for existing in trail:
                if (
                    existing.get("stage") == stage
                    and existing.get("job") != job_id
                    and existing.get("status") == "started"
                ):
                    existing["status"] = "deferred"
                    existing["at"] = now

        # A job can report more than 1 sub-stage. Each one goes directly
        # before the own entry of the job. Thus, the sub-stages keep the
        # order of their first stamps. The job entry carries the stage
        # label of the sibling.

        index = len(trail)
        if before_job:
            own_stage = sibling[0] if sibling is not None else None
            for position, existing in enumerate(trail):
                if existing.get("job") == job_id and (
                    own_stage is None or existing.get("stage") == own_stage
                ):
                    index = position
                    break
        new_entry = {"stage": stage, "status": status, "at": now, "job": job_id}

        # The first entry of a new journey carries a mark. The Queue table
        # shows the trail from the last mark only. The File Activity page
        # shows the full history (#297).

        if fresh_journey:
            new_entry["journey"] = True
        trail.insert(index, new_entry)

    # A sub-stage event can change the own entry of its job in the same
    # atomic write. Thus, only 1 chip reads "running" at a time (Glenn,
    # 2026-08-20). The job chip goes to "queued" while its sub-stage runs.
    # It returns to "started" after. This is an update only. This never
    # creates a sibling.

    if sibling is not None:
        sibling_stage, sibling_status = sibling
        for entry in reversed(trail):
            if entry.get("job") == job_id and entry.get("stage") == sibling_stage:
                entry["status"] = sibling_status
                entry["at"] = now
                break
    del trail[:-40]

    # The FIRST start of the file is the sort anchor of the running
    # banners (the original banner-ordering request of Glenn). It does not
    # move while the journey continues. Thus, a file that steps between
    # queues keeps its place. A new journey (a re-import) sets it again.
    # The anchor is UTC with an explicit offset (#405). A local wall-clock
    # time is ambiguous in the repeated hour of the fall-back night. The
    # offset also tells a new anchor from a local one of an older trail.

    if fresh_journey:
        first_run = None
    if status == "started":
        first_run = first_run or f"{event.utc_now}+00:00"

    # The first ENQUEUE of the file anchors the Enqueued column of the
    # queue page in the same way. It stays still while the work steps
    # between queues. Each step is a new job with its own enqueued_at.
    # Fitzflix stores it as naive UTC to match the enqueued_at convention
    # of rq. The stamps of the chips are local wall clock. See
    # first_enqueued.

    if status in ("queued", "scheduled", "started") or fresh_journey:
        stamp = event.anchor or event.utc_now
        first_enqueued = stamp if fresh_journey else (first_enqueued or stamp)

    return trail, first_run, first_enqueued, True


def _log_traceback():
    """Log the current exception as a warning.

    The trail is advisory. A failure here must never fail the task or the
    page that wrote or read it. Outside an app context, the warning goes
    to the logger of this module."""

    message = traceback.format_exc()
    try:
        from flask import current_app

        current_app.logger.warning(message)
    except Exception:
        logging.getLogger(__name__).warning(message)


def _best_effort(fallback=None):
    """Decorate a trail function. An exception in it only logs a warning.

    On an exception, the function returns fallback(*args, **kwargs), or
    None with no fallback. The timeout of rq and an interrupt are not
    caught (#376). The worker raises the timeout in the task process.
    If a trail call catches it, the job runs on with no time limit."""

    def decorate(function):
        @functools.wraps(function)
        def wrapper(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except (BaseTimeoutException, KeyboardInterrupt):
                raise
            except Exception:
                _log_traceback()
                return fallback(*args, **kwargs) if fallback else None

        return wrapper

    return decorate


@_best_effort()
def migrate_trail(connection, old_basename, new_basename):
    """Merge the trail of a file under its new basename.

    The pipeline can rename a file while it is in progress. The parse
    makes a title canonical against an existing series. A container
    conversion changes the extension to .mkv. Without this, the journey
    splits into 2 trails. Both claim the same File Activity card and
    overwrite the chips of the other. The Futurama S11 imports lost
    their Moved and Cataloged chips this way (2026-08).

    Localization calls this before it enqueues the move job. Thus, the
    "queued" stamp of the move already goes onto the merged trail. An
    alias redirects the writes that arrive under the old name AFTER the
    rename onto it too. The own "done" stamp of the localization worker
    occurs when the task body returns. This is advisory, like each
    hook. It logs and catches failures. The retries are limited."""

    if not old_basename or not new_basename or old_basename == new_basename:
        return
    from redis import WatchError

    old_digest = _digest(old_basename)
    new_digest = _digest(new_basename)
    old_key = FILE_KEY.format(digest=old_digest)
    new_key = FILE_KEY.format(digest=new_digest)
    alias_key = ALIAS_KEY.format(digest=old_digest)

    for _ in range(10):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with connection.pipeline() as pipe:
            try:
                pipe.watch(old_key, new_key)
                old_trail = _decode_trail(pipe.hget(old_key, "trail"))
                old_first = pipe.hget(old_key, "first_run")
                old_enqueued = pipe.hget(old_key, "first_enqueued")

                # Nothing is recorded under the old name. It expired,
                # or the hooks never ran. Only leave the redirect.

                if not old_trail and old_first is None:
                    pipe.multi()
                    pipe.set(alias_key, new_basename, ex=TRAIL_TTL_SECONDS)
                    pipe.execute()
                    return

                # A re-import within the TTL finds the chips of the
                # last run under the new name. The entries of the
                # current run are newer. Thus, they go after.

                merged = _decode_trail(pipe.hget(new_key, "trail")) + old_trail
                del merged[:-40]

                pipe.multi()
                if old_first is not None:
                    pipe.hsetnx(new_key, "first_run", old_first)
                if old_enqueued is not None:
                    pipe.hsetnx(new_key, "first_enqueued", old_enqueued)
                pipe.hset(
                    new_key,
                    mapping={
                        "basename": new_basename,
                        "trail": json.dumps(merged),
                        "updated": now,
                    },
                )
                pipe.expire(new_key, TRAIL_TTL_SECONDS)
                pipe.set(alias_key, new_basename, ex=TRAIL_TTL_SECONDS)
                # The new name holds the trail now. An old redirect away
                # from it (a rename back to an earlier name) must go.
                pipe.delete(ALIAS_KEY.format(digest=new_digest))
                pipe.delete(old_key)
                pipe.zrem(ACTIVE_KEY, old_digest)
                pipe.zadd(ACTIVE_KEY, {new_digest: time.time()})
                pipe.zremrangebyrank(ACTIVE_KEY, 0, -(ACTIVE_LIMIT + 1))
                pipe.execute()
                return
            except WatchError:
                continue


@_best_effort()
def migrate_renamed_file(connection, old_names, new_names):
    """Merge the trail of a renamed File row under its new names (#375).

    old_names and new_names are (basename, untouched_basename) pairs.
    The import keeps the trail under the library name when the import
    name differs from it (an .mp4 remuxed to .mkv, a canonical series
    title). Thus, the library name moves first. The untouched name moves
    only when it has no redirect of its own. A redirect already follows
    the library name."""

    (old_basename, old_untouched), (new_basename, new_untouched) = old_names, new_names
    migrate_trail(connection, old_basename, new_basename)
    if (
        old_untouched
        and old_untouched != old_basename
        and _resolve_basename(connection, old_untouched) == old_untouched
    ):
        migrate_trail(connection, old_untouched, new_untouched)


@_best_effort()
def record_job_event(connection, job, event):
    """Append or update 1 stage entry on the file trail of the job.

    This is advisory only. It logs and catches each failure. It never
    shows a failure to the pipeline."""

    found = _stage_for(job)
    if found is None:
        return
    basename, stage = found

    # The trail stamps are local wall clock. rq stamps enqueued_at in
    # UTC. Thus, convert it before the compare.

    enqueued_at = None
    anchor = None
    if job.enqueued_at is not None:
        moment = job.enqueued_at
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        anchor = moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        if event in ("queued", "scheduled"):
            enqueued_at = moment.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    _write_trail_entry(
        connection,
        basename,
        stage,
        event,
        job.id,
        enqueued_at=enqueued_at,
        anchor=anchor,
    )


@_best_effort()
def record_task_stage(stage, status):
    """Record a named phase INSIDE 1 pipeline job from the task body.

    This is for work that can fail before the own stage of the job
    shows movement, and that has no job boundary of its own. One example
    is the copy to the staging directory during localization. This
    derives the file from the current job through the STAGES registry.
    The entry is keyed by the id of that job. Thus, a retry job leaves
    its own new sub-stage line. The line renders BEFORE the own chip of
    the job, because it reports work that comes before the main stage
    of the job. The job chip reads "queued" while the sub-stage runs.
    Thus, only 1 chip is "running" at a time. This is advisory, like
    each hook. It catches failures. Outside a worker (direct calls in
    tests), it does nothing."""

    from rq import get_current_job

    job = get_current_job()
    if job is None:
        return
    found = _stage_for(job)
    if found is None:
        return
    basename, job_stage = found

    # While the sub-stage runs, the own chip of the job gives
    # "running" to it. The main phase has not started. Thus, the job
    # chip reads "queued" until the sub-stage completes. Then it
    # continues. The own done/failed hook of the worker has the last
    # word at the end of the job.

    sibling = (job_stage, "queued" if status == "started" else "started")
    _write_trail_entry(
        job.connection,
        basename,
        stage,
        status,
        job.id,
        before_job=True,
        sibling=sibling,
    )


class TaskPhases:
    """Record the phases of 1 job as sub-stage chips, in order.

    A long job can do many steps. The Atmos twin copies the file to
    staging, decodes, encodes, remuxes, and copies back. enter starts a
    phase and completes the phase before it. finish completes the last
    phase. fail marks the phase that runs as failed. The chips go before
    the own chip of the job. Each call is advisory, like
    record_task_stage.

    target is (job id, basename, own stage) of an EARLIER job. The Atmos
    run is 3 jobs. The check and the finish jobs continue the chips of
    the first job. current is the phase that the earlier job left open.
    """

    def __init__(self, target=None, current=None):
        self.target = target
        self.current = current

    def _record(self, stage, status):
        """Stamp 1 phase on the current job, or on the target job."""

        if self.target is None:
            record_task_stage(stage, status)
        else:
            record_stage_for(*self.target, stage, status)

    def enter(self, stage):
        """Complete the phase that runs, then start the phase stage."""

        if stage == self.current:
            return
        self.finish()
        self._record(stage, "started")
        self.current = stage

    def finish(self):
        """Complete the phase that runs, if there is one."""

        if self.current is not None:
            self._record(self.current, "done")
            self.current = None

    def fail(self):
        """Mark the phase that runs as failed, if there is one."""

        if self.current is not None:
            self._record(self.current, "failed")
            self.current = None

    def close(self, status):
        """Stamp the own chip of the target job done or failed."""

        if self.target is not None:
            record_stage_for(*self.target, None, status)


@_best_effort()
def record_stage_for(job_id, basename, own_stage, stage, status):
    """Stamp a chip of an earlier job of the same run.

    stage is a sub-stage of that job, or None for the own chip of the
    job. A sub-stage moves the own chip between "queued" and "started",
    the same as record_task_stage. This is advisory. It catches each
    failure."""

    from flask import current_app

    connection = current_app.redis
    if stage is None:
        _write_trail_entry(connection, basename, own_stage, status, job_id)
        return
    sibling = (own_stage, "queued" if status == "started" else "started")
    _write_trail_entry(
        connection,
        basename,
        stage,
        status,
        job_id,
        before_job=True,
        sibling=sibling,
    )


def keep_trail_open(connection, job_id, ttl):
    """Keep the open chips of a finished job while a later job continues them."""

    if job_id:
        connection.set(CONTINUES_KEY.format(job_id=job_id), "1", ex=ttl)


def close_trail(connection, job_id):
    """End keep_trail_open. The run wrote its last chip."""

    if job_id:
        connection.delete(CONTINUES_KEY.format(job_id=job_id))


# The status of an ended rq job, and the chip status that settles it.

SETTLED_STATUS = {
    "finished": "done",
    "failed": "failed",
    "stopped": "failed",
    "canceled": "failed",
}


def _waiting_chip_fate(connection, job_id):
    """Return what happens to a waiting chip of a job.

    None keeps the chip. "gone" removes it, because rq does not know
    the job. "done" or "failed" settles it, because the job ended and
    its own stamp was lost. On 2026-10-22, 6 track jobs failed before
    the failure stamp could find their file. Their chips stayed
    "queued" for 7 days."""

    if connection.exists(CONTINUES_KEY.format(job_id=job_id)):
        return None
    status = connection.hget(f"rq:job:{job_id}", "status")
    if status is None:
        return None if connection.exists(f"rq:job:{job_id}") else "gone"
    status = status.decode() if isinstance(status, bytes) else status
    return SETTLED_STATUS.get(status)


def trail_continues(job):
    """Return True if a later job of the run closes the chips of this job.

    The first Atmos job returns when MediaConvert has the encode. The
    run is not done. Thus, the worker does not stamp its chip done."""

    try:
        meta = job.get_meta(refresh=True)
    except Exception:
        meta = job.meta or {}
    return bool(meta.get("trail_continues"))


def first_run(connection, job):
    """Return the time when the FILE of the job first started a stage.

    This returns None for a job outside the pipeline, or for a file
    with no recorded start. The running banners sort by this. Thus, a
    file that steps between queues holds its position. Each step is a
    new job with a new started_at."""

    try:
        name = _file_of(job)
        if name is None:
            return None
        basename = _resolve_basename(connection, name)
        value = connection.hget(FILE_KEY.format(digest=_digest(basename)), "first_run")
        return value.decode() if isinstance(value, bytes) else value
    except Exception:
        return None


def first_enqueued(connection, job):
    """Return the time when the FILE of the job first entered the pipeline
    on its current journey.

    The value is an aware-UTC datetime. It matches the own enqueued_at
    of rq. rq 2 stamps aware datetimes. A naive datetime in the mix
    would break the sort of the queue list. The Enqueued column of the
    queue page holds this still while the work steps between queues.
    This returns None for a job outside the pipeline, or for a file
    whose trail has no anchor. Then the caller uses the own time of the
    job."""

    try:
        name = _file_of(job)
        if name is None:
            return None
        basename = _resolve_basename(connection, name)
        value = connection.hget(
            FILE_KEY.format(digest=_digest(basename)), "first_enqueued"
        )
        if value is None:
            return None
        value = value.decode() if isinstance(value, bytes) else value
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )
    except Exception:
        return None


@_best_effort(fallback=lambda connection, digest, entries: entries)
def _heal_orphaned_entries(connection, digest, entries):
    """Remove or settle the waiting chips of jobs that are not waiting.

    A queued job that is cancelled and deleted outside the pipeline
    leaves its "queued" stamp on the trail for ever. Nothing advances
    it. Thus, the queue page shows a phantom job until the TTL of the
    full trail expires. Examples are the 9 cancelled scaffold
    re-archives of 2026-08-29, and the tenth one found on 2026-08-31.
    Fitzflix writes the stamp only after the own job hash of rq exists.
    Thus, a waiting or running chip with no job hash is orphaned. It is
    not early. This removes those entries.

    A job that ended can also keep a waiting chip, when its own stamp
    was lost. This settles that chip to "done" or "failed", from the
    status of the job in rq.

    Terminal chips (done, failed) are history and always stay. This
    returns the entries that remain, or None if the full trail was
    orphaned and deleted. This is advisory, like each trail write. On a
    failure, it returns the entries as they were.
    """

    from redis import WatchError

    waiting = ("queued", "scheduled", "started")
    candidates = {
        entry.get("job")
        for entry in entries
        if entry.get("status") in waiting and entry.get("job")
    }
    if not candidates or all(
        _waiting_chip_fate(connection, job_id) is None for job_id in candidates
    ):
        return entries

    key = FILE_KEY.format(digest=digest)
    with connection.pipeline() as pipe:
        try:
            pipe.watch(key)

            # Read and verify again under WATCH. A re-enqueue can use
            # a deterministic job id (safe_job_id) again. rq creates
            # its hash before its trail stamp. The second check here
            # keeps the chip of a job that was just revived.

            trail = _decode_trail(pipe.hget(key, "trail"))
            kept = []
            changed = False
            for entry in trail:
                fate = None
                if entry.get("status") in waiting and entry.get("job") in candidates:
                    fate = _waiting_chip_fate(pipe, entry.get("job"))
                if fate == "gone":
                    changed = True
                    continue
                if fate in ("done", "failed"):
                    entry = dict(entry, status=fate)
                    changed = True
                kept.append(entry)
            if not changed:
                return kept
            pipe.multi()
            if kept:
                # hset keeps the remaining TTL of the key. The repair
                # does not extend the life of the trail. It does not
                # increase its recency in the active set.
                pipe.hset(key, "trail", json.dumps(kept))
            else:
                pipe.delete(key)
                pipe.delete(ALIAS_KEY.format(digest=digest))
                pipe.zrem(ACTIVE_KEY, digest)
            pipe.execute()
            return kept or None
        except WatchError:
            return entries


def dismiss_trail(connection, digest):
    """Delete the trail of 1 file from the queue page (an admin Dismiss).

    The button shows only on a "no job in queue" row. A trail with a
    waiting chip of a live job is still work in line. Thus, it stays,
    and this returns False. Otherwise, this deletes the trail and
    returns True. A trail that is already gone also returns True."""

    from redis import WatchError

    key = FILE_KEY.format(digest=digest)
    waiting = ("queued", "scheduled", "started")
    with connection.pipeline() as pipe:
        try:
            pipe.watch(key)
            for entry in _decode_trail(pipe.hget(key, "trail")):
                if (
                    entry.get("status") in waiting
                    and entry.get("job")
                    and _waiting_chip_fate(pipe, entry.get("job")) is None
                ):
                    return False
            pipe.multi()
            pipe.delete(key)
            pipe.delete(ALIAS_KEY.format(digest=digest))
            pipe.zrem(ACTIVE_KEY, digest)
            pipe.execute()
            return True
        except WatchError:
            return False


def _stem(name):
    """Return the name without its extension, or the name itself."""

    dot = name.rfind(".")
    return name[:dot] if dot > 0 else name


def index_trails(trails):
    """Return the trails of pipeline_trails keyed by basename and by stem.

    The cards of the File Activity page look their trail up here. The
    basename of a trail can be the original import filename, while the
    card carries the name of the File row. A container conversion also
    changes the extension to .mkv. Thus, the index holds each trail under
    its full name and under its stem. The newest trail wins a shared key.
    This mirrors cardSlots and renderFiles in base.html."""

    index = {}
    for trail in trails:
        name = trail.get("basename") or ""
        if not name:
            continue
        index.setdefault(name, trail)
        index.setdefault(_stem(name), trail)
    return index


def trail_entries_for(index, *names):
    """Return the entries of the trail that matches one of the names, or [].

    Each name is tried as it is and then as its stem. The first match
    wins. The names of a card are the basename of the File row and the
    original import filename."""

    for name in names:
        if not name:
            continue
        trail = index.get(name) or index.get(_stem(name))
        if trail:
            return trail["entries"]
    return []


def queue_rows(tasks, trails):
    """Return the rows of the queue page: the jobs, then the orphan trails.

    This is the twin of renderAll in base.html. The server renders the
    table from it. Thus, the rows are in place at the first paint. Each
    row is {"task": task or None, "trail": trail or None}. A job finds
    the trail of its file by the job id stamped on the trail entries. A
    trail whose last stage is not done, and that no listed job owns,
    comes after the jobs as a "no job in queue" row."""

    trail_by_job = {}
    for trail in trails:
        for entry in trail["entries"]:
            if entry.get("job"):
                trail_by_job[entry["job"]] = trail

    rows = []
    listed = []
    for task in tasks:
        # A check or finish job of a multi-job run writes to the chips of
        # the first job. trail_job names that job.

        trail = trail_by_job.get(task.get("trail_job") or task["id"])
        if trail is not None and all(trail is not seen for seen in listed):
            listed.append(trail)
        rows.append({"task": task, "trail": trail})

    for trail in trails:
        if any(trail is seen for seen in listed):
            continue
        last = trail["entries"][-1] if trail["entries"] else None
        if last is None or last.get("status") == "done":
            continue
        rows.append({"task": None, "trail": trail})
    return rows


def current_journey(entries):
    """Return the entries of the newest journey of a trail (#297).

    A journey starts at the last entry with the journey mark. The
    sub-stages of that job sit directly before it. Thus, they belong to
    the journey too. A trail with no mark is 1 journey."""

    for position in range(len(entries) - 1, -1, -1):
        if entries[position].get("journey"):
            return entries[_rerun_start(entries, position) :]
    return entries


def pipeline_trails(connection, limit=25, journey_only=False):
    """Return the newest file trails for the queue page, most recent
    first: [{basename, updated, entries: [{stage, status, at, job}, ...]}, ...].

    Each entry carries the rq job id that stamped it. A queue-page row
    finds the trail of its own file by this id. The match is exact. No
    basename-versus-description match can become stale. With
    journey_only, each trail has only its newest journey. The Queue
    table uses that. The File Activity page uses the full history."""

    trails = []
    try:
        digests = connection.zrevrange(ACTIVE_KEY, 0, limit - 1)
        for digest in digests:
            digest = digest.decode() if isinstance(digest, bytes) else digest
            data = connection.hgetall(FILE_KEY.format(digest=digest))
            if not data:
                connection.zrem(ACTIVE_KEY, digest)
                continue
            decoded = {
                (k.decode() if isinstance(k, bytes) else k): (
                    v.decode() if isinstance(v, bytes) else v
                )
                for k, v in data.items()
            }
            raw_entries = _heal_orphaned_entries(
                connection, digest, json.loads(decoded.get("trail") or "[]")
            )
            if raw_entries is None:
                continue
            if journey_only:
                raw_entries = current_journey(raw_entries)
            # A deferred attempt does not show. Its retry chip stands for
            # the stage. The done own chip of a phased job does not show
            # when the job has phase chips. A job with no phase chips
            # keeps it, because it is the only record of the job.

            phased_jobs = {
                entry.get("job")
                for entry in raw_entries
                if entry.get("job") and entry.get("stage") not in PHASED_STAGES
            }
            entries = [
                {
                    "stage": entry.get("stage"),
                    "status": entry.get("status"),
                    "at": entry.get("at"),
                    "job": entry.get("job"),
                }
                for entry in raw_entries
                if entry.get("status") != "deferred"
                and not (
                    entry.get("status") == "done"
                    and entry.get("stage") in PHASED_STAGES
                    and entry.get("job") in phased_jobs
                )
            ]
            trails.append(
                {
                    "basename": decoded.get("basename"),
                    "digest": digest,
                    "updated": decoded.get("updated"),
                    "entries": entries,
                }
            )
    except Exception:
        _log_traceback()
    return trails


class TrackedQueue(Queue):
    """An rq Queue that leaves trail entries when it enqueues jobs.

    The entry is "queued" for immediate work and "scheduled" for a
    deferred retry. Thus, a file that waits for its turn is already
    visible on the queue page."""

    def enqueue_job(self, job, pipeline=None, at_front=False, unique=False):
        """Enqueue the job and stamp the trail. The job waits for its turn."""

        job = super().enqueue_job(
            job, pipeline=pipeline, at_front=at_front, unique=unique
        )
        record_job_event(self.connection, job, "queued")
        return job

    def schedule_job(self, job, datetime, pipeline=None, unique=False):
        """Schedule the job and stamp the trail. A deferred retry is booked."""

        job = super().schedule_job(job, datetime, pipeline=pipeline, unique=unique)
        record_job_event(self.connection, job, "scheduled")
        return job


def move_to_front(queue, job_id):
    """Move a waiting job to the front of its queue (#236).

    The return value is the old index of the job in the queue. It is
    None when the job does not wait in the queue. A worker possibly
    started it already. Only the queue of the job changes. A worker
    that also takes jobs from other queues takes their jobs first."""

    index = queue.connection.eval(
        _MOVE_TO_FRONT,
        2,
        queue.key,
        FRONT_KEY,
        job_id,
        time.time(),
        FRONT_TTL_SECONDS,
    )
    return None if index < 0 else index


def front_times(connection, job_ids):
    """Return {job_id: time of the move} for the jobs that an admin moved."""

    if not job_ids:
        return {}
    scores = connection.zmscore(FRONT_KEY, job_ids)
    return {
        job_id: score for job_id, score in zip(job_ids, scores) if score is not None
    }


class PipelineWorker(SimpleWorker):
    """A SimpleWorker that stamps the trail around the execution.

    It stamps started when it picks up a job. It stamps done or failed
    when the job completes."""

    def execute_job(self, job, queue):
        """Stamp started and clear a front mark. Then run the job as SimpleWorker does."""

        record_job_event(self.connection, job, "started")
        self.connection.zrem(FRONT_KEY, job.id)
        return super().execute_job(job, queue)

    def handle_job_success(self, job, queue, started_job_registry):
        """Run the success handling of rq. Then stamp the trail done."""

        super().handle_job_success(job, queue, started_job_registry)
        if not trail_continues(job):
            record_job_event(self.connection, job, "done")

    def handle_job_failure(self, job, queue, started_job_registry=None, exc_string=""):
        """Run the failure handling of rq. Then stamp the trail failed."""

        super().handle_job_failure(
            job,
            queue,
            started_job_registry=started_job_registry,
            exc_string=exc_string,
        )
        record_job_event(self.connection, job, "failed")
