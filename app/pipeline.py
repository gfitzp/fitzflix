"""Per-file pipeline trails (#18).

Every file moving through the import pipeline leaves one ordered trail
in Redis — Localizing → Moving into the library → Cataloging →
Archiving to S3, plus remuxes, transcodes, and restores — so the queue
page can answer "where is my file right now" without reading worker
logs.

State updates come from JOB LIFECYCLE HOOKS, not from instrumenting
task bodies: TrackedQueue records "queued"/"scheduled" as jobs are
enqueued (deferred retries surface as "scheduled"), and PipelineWorker
records "started"/"done"/"failed" around execution. Which file a job
belongs to is derived centrally by the STAGES registry from the job's
function name and arguments — tasks the registry doesn't know are
simply not trails (SQL refreshes, maintenance sweeps). The one
sanctioned in-task emitter is record_task_stage, for a phase that has
no job boundary of its own (localization's copy to the staging
directory); it still derives the file from the current job through
the registry.

Recording must never break the pipeline: every hook swallows and logs
its own failures, and a trail is only ever advisory display state —
seven days of TTL (matching the File Activity page's SQL window, so a
landed card keeps its chips as long as it stays on the page), newest
hundred files kept.
"""

import hashlib
import json
import os
import time
import traceback

from datetime import datetime

from rq import Queue, SimpleWorker

FILE_KEY = "fitzflix:pipeline:file:{digest}"
ACTIVE_KEY = "fitzflix:pipeline:active"
TRAIL_TTL_SECONDS = 7 * 86400
ACTIVE_LIMIT = 100


def _basename_from_path(args, kwargs):
    """The file's basename from a leading path argument."""

    return os.path.basename(args[0]) if args else None


def _basename_from_details(args, kwargs):
    """The basename inside a file_details dict (second argument)."""

    if len(args) > 1 and isinstance(args[1], dict):
        return args[1].get("basename")
    return None


def _basename_from_second_arg(args, kwargs):
    """A literal basename passed second (download_task's signature)."""

    return args[1] if len(args) > 1 else None


def _basename_from_file_id(args, kwargs):
    """The File record's basename, looked up by a leading file_id.

    Enqueue-side hooks already run inside an app context (the test
    app's included — never trust the get_app() singleton in tests);
    worker-side hooks run outside one, so fall back to the worker's
    own app.
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


# Every per-file pipeline task, by rq function name: the stage label
# the trail shows, and how to derive the file's basename from the
# job's arguments. Anything not listed leaves no trail.

STAGES = {
    "app.videos.localization_task": ("Localizing", _basename_from_path),
    "app.videos.move_localized_file": (
        "Moving into the library",
        _basename_from_details,
    ),
    "app.videos.finalize_localization": ("Cataloging", _basename_from_details),
    "app.videos.upload_task": ("Archiving to S3", _basename_from_file_id),
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
    "app.videos.transcode_task": ("Transcoding", _basename_from_file_id),
    "app.videos.download_task": ("Restoring from S3", _basename_from_second_arg),
}


def _stage_for(job):
    """(basename, stage label) for a pipeline job, or None."""

    entry = STAGES.get(job.func_name)
    if entry is None:
        return None
    label, extractor = entry
    basename = extractor(job.args or (), job.kwargs or {})
    if not basename:
        return None
    return basename, label


def _digest(basename):
    """The stable Redis key fragment for one file's trail."""

    return hashlib.sha1(basename.encode("utf-8", "replace")).hexdigest()[:16]


def _decode_trail(raw):
    """The trail list from its stored JSON (or a fresh empty one)."""

    return json.loads(raw) if raw else []


def _write_trail_entry(
    connection, basename, stage, status, job_id, before_job=False, sibling=None
):
    """Atomically apply one stage event to the file's trail.

    Two workers touch the same trail at a stage handoff: the move job
    starts the instant the localization task enqueues it, so the
    file-operation worker's "started" stamp races the import worker's
    "done" stamp for the stage before it. A plain read-modify-write
    lets whichever lands second erase the other's update (#76 froze
    two files at "Localizing · running" forever), so the write WATCHes
    the trail key and retries from a fresh read when it changed
    underneath. Bounded retries: the trail is advisory, never worth
    stalling a worker over.
    """

    from redis import WatchError

    digest = _digest(basename)
    key = FILE_KEY.format(digest=digest)

    for _ in range(10):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with connection.pipeline() as pipe:
            try:
                pipe.watch(key)
                trail = _decode_trail(pipe.hget(key, "trail"))

                # The same job moving through its lifecycle updates its
                # own entry — queued → started → done is one line, not
                # three; a re-enqueued retry is a NEW job id, so it
                # appends a fresh entry and the earlier failure stays
                # visible. A task-emitted sub-stage shares its job's id
                # but carries its own stage label, so it is its own line.

                for entry in reversed(trail):
                    if entry.get("job") == job_id and entry.get("stage") == stage:
                        entry["status"] = status
                        entry["at"] = now
                        break
                else:
                    # A sub-stage reports preparatory work inside its job
                    # (the staging copy precedes the localizing proper),
                    # so it slots in AHEAD of the job's own entry — the
                    # chips then read in pipeline order, not in order of
                    # first stamp, which the job-level entry always wins
                    # by existing from enqueue time (Glenn, Aug 2026)

                    index = len(trail)
                    if before_job:
                        for position, existing in enumerate(trail):
                            if existing.get("job") == job_id:
                                index = position
                                break
                    trail.insert(
                        index,
                        {"stage": stage, "status": status, "at": now, "job": job_id},
                    )

                # A sub-stage event may adjust its job's own entry in the
                # same atomic write, so only one chip reads "running" at a
                # time (Glenn, Aug 20): the job chip drops to "queued"
                # while its sub-stage runs and resumes "started" after.
                # Update-only — a sibling is never created here

                if sibling is not None:
                    sibling_stage, sibling_status = sibling
                    for entry in reversed(trail):
                        if (
                            entry.get("job") == job_id
                            and entry.get("stage") == sibling_stage
                        ):
                            entry["status"] = sibling_status
                            entry["at"] = now
                            break
                del trail[:-40]

                pipe.multi()

                # The file's FIRST start is the running banners' sort
                # anchor (Glenn's original #18 ask): it never moves once
                # set, so a file hopping queues keeps its place

                if status == "started":
                    pipe.hsetnx(key, "first_run", now)

                pipe.hset(
                    key,
                    mapping={
                        "basename": basename,
                        "trail": json.dumps(trail),
                        "updated": now,
                    },
                )
                pipe.expire(key, TRAIL_TTL_SECONDS)
                pipe.zadd(ACTIVE_KEY, {digest: time.time()})
                pipe.zremrangebyrank(ACTIVE_KEY, 0, -(ACTIVE_LIMIT + 1))
                pipe.execute()
                return
            except WatchError:
                continue


def record_job_event(connection, job, event):
    """Append (or update in place) one stage entry on the job's file
    trail. Advisory only — any failure is logged and swallowed, never
    surfaced to the pipeline itself."""

    try:
        found = _stage_for(job)
        if found is None:
            return
        basename, stage = found
        _write_trail_entry(connection, basename, stage, event, job.id)
    except Exception:
        try:
            from flask import current_app

            current_app.logger.warning(traceback.format_exc())
        except Exception:
            pass


def record_task_stage(stage, status):
    """A named phase INSIDE one pipeline job, reported from the task
    body — for work that can fail before the job's own stage shows any
    movement and has no job boundary of its own, like localization's
    copy to the staging directory. The file is still derived from the
    current job via the STAGES registry and the entry is keyed by that
    job's id, so a retry job leaves its own fresh sub-stage line — and
    it renders AHEAD of the job's own chip, since it reports work that
    precedes the job's headline stage — which reads "queued" while the
    sub-stage runs, so only one chip is "running" at a time. Advisory
    like every hook: failures are swallowed, and outside a worker
    (direct calls in tests) it is a no-op."""

    try:
        from rq import get_current_job

        job = get_current_job()
        if job is None:
            return
        found = _stage_for(job)
        if found is None:
            return
        basename, job_stage = found

        # While the sub-stage runs, the job's own chip yields "running"
        # to it — the headline phase hasn't begun, so it reads "queued"
        # until the sub-stage lands, then resumes (the worker's own
        # done/failed hook still has the last word at job end)

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
    except Exception:
        try:
            from flask import current_app

            current_app.logger.warning(traceback.format_exc())
        except Exception:
            pass


def first_run(connection, job):
    """When the job's FILE first began running any stage, or None for
    jobs outside the pipeline (or files with no start recorded yet).
    The running banners sort by this so a file whose work hops queues
    — each hop a new job with a new started_at — holds its position."""

    try:
        found = _stage_for(job)
        if found is None:
            return None
        basename, _ = found
        value = connection.hget(FILE_KEY.format(digest=_digest(basename)), "first_run")
        return value.decode() if isinstance(value, bytes) else value
    except Exception:
        return None


def pipeline_trails(connection, limit=25):
    """The newest file trails for the queue page, most recent first:
    [{basename, updated, entries: [{stage, status, at}, …]}, …]."""

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
            entries = [
                {
                    "stage": entry.get("stage"),
                    "status": entry.get("status"),
                    "at": entry.get("at"),
                }
                for entry in json.loads(decoded.get("trail") or "[]")
            ]
            trails.append(
                {
                    "basename": decoded.get("basename"),
                    "updated": decoded.get("updated"),
                    "entries": entries,
                }
            )
    except Exception:
        try:
            from flask import current_app

            current_app.logger.warning(traceback.format_exc())
        except Exception:
            pass
    return trails


class TrackedQueue(Queue):
    """An rq Queue that leaves trail entries as jobs are enqueued —
    "queued" for immediate work, "scheduled" for deferred retries —
    so a file waiting its turn is already visible on the queue page."""

    def enqueue_job(self, job, pipeline=None, at_front=False, unique=False):
        """Enqueue and stamp the trail: the job is waiting its turn."""

        job = super().enqueue_job(
            job, pipeline=pipeline, at_front=at_front, unique=unique
        )
        record_job_event(self.connection, job, "queued")
        return job

    def schedule_job(self, job, datetime, pipeline=None, unique=False):
        """Schedule and stamp the trail: a deferred retry is booked."""

        job = super().schedule_job(job, datetime, pipeline=pipeline, unique=unique)
        record_job_event(self.connection, job, "scheduled")
        return job


class PipelineWorker(SimpleWorker):
    """A SimpleWorker that stamps the trail around execution: started
    when a job is picked up, done or failed when it lands."""

    def execute_job(self, job, queue):
        """Stamp started, then run the job as SimpleWorker does."""

        record_job_event(self.connection, job, "started")
        return super().execute_job(job, queue)

    def handle_job_success(self, job, queue, started_job_registry):
        """Run rq's success handling, then stamp the trail done."""

        super().handle_job_success(job, queue, started_job_registry)
        record_job_event(self.connection, job, "done")

    def handle_job_failure(self, job, queue, started_job_registry=None, exc_string=""):
        """Run rq's failure handling, then stamp the trail failed."""

        super().handle_job_failure(
            job,
            queue,
            started_job_registry=started_job_registry,
            exc_string=exc_string,
        )
        record_job_event(self.connection, job, "failed")
