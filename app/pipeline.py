"""Record a pipeline trail for each file.

Each file that moves through the import pipeline leaves 1 ordered trail
in Redis. The stages are Localizing, Moving into the library,
Cataloging, and Archiving to S3, plus remuxes, transcodes, and
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

import hashlib
import json
import os
import time
import traceback

from datetime import datetime, timezone

from rq import Queue, SimpleWorker

FILE_KEY = "fitzflix:pipeline:file:{digest}"
ALIAS_KEY = "fitzflix:pipeline:alias:{digest}"
ACTIVE_KEY = "fitzflix:pipeline:active"
TRAIL_TTL_SECONDS = 7 * 86400
ACTIVE_LIMIT = 100


def _basename_from_path(args, kwargs):
    """Return the basename of the file from a leading path argument."""

    return os.path.basename(args[0]) if args else None


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
    "app.videos.finalize_localization": ("Cataloging", _basename_from_details),
    "app.videos.upload_task": ("Archiving to S3", _basename_from_file_id),
    "app.videos.rearchive_untouched_object": (
        "Re-archiving to S3",
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
    "app.videos.transcode_task": ("Transcoding", _basename_from_file_id),
    "app.videos.download_task": ("Restoring from S3", _basename_from_second_arg),
}


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


def _write_trail_entry(
    connection, basename, stage, status, job_id, before_job=False, sibling=None
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
    """

    from redis import WatchError

    basename = _resolve_basename(connection, basename)
    digest = _digest(basename)
    key = FILE_KEY.format(digest=digest)

    for _ in range(10):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        fresh_journey = False
        with connection.pipeline() as pipe:
            try:
                pipe.watch(key)
                trail = _decode_trail(pipe.hget(key, "trail"))

                # A job that moves through its lifecycle updates its own
                # entry. Thus, queued, started, and done are 1 line, not
                # 3. A re-enqueued retry is a NEW job id. Thus, it appends
                # a new entry, and the earlier failure stays visible. A
                # sub-stage from a task shares the id of its job. It
                # carries its own stage label. Thus, it is its own line.

                for entry in reversed(trail):
                    if entry.get("job") == job_id and entry.get("stage") == stage:
                        # A job can complete before its own enqueue-side
                        # stamp is written. The deferred re-archive skips
                        # a superseded key in milliseconds. A late
                        # "queued" would pull the chip back and freeze it
                        # there. The enqueue side never overrides a stamp
                        # that the worker already made.
                        if status in ("queued", "scheduled") and entry.get(
                            "status"
                        ) in ("started", "done", "failed"):
                            return
                        entry["status"] = status
                        entry["at"] = now
                        break
                else:
                    # A sub-stage reports preparatory work inside its job.
                    # The staging copy comes before the localization
                    # itself. Thus, the sub-stage goes BEFORE the own
                    # entry of the job. Then the chips read in pipeline
                    # order, not in the order of the first stamp. The
                    # job-level entry always wins that order, because it
                    # exists from the enqueue time (Glenn, 2026-08).

                    # A waiting stamp can arrive on an empty or fully
                    # settled trail. Then it opens a NEW journey through
                    # the pipeline (a re-import, or a later re-archive of
                    # a file that arrived). Its enqueue anchor starts again.
                    # A retry after a failure continues the same journey.
                    # Thus, the anchor holds.

                    if status in ("queued", "scheduled") and not before_job:
                        fresh_journey = not trail or all(
                            existing.get("status") == "done" for existing in trail
                        )

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

                # A sub-stage event can change the own entry of its job in
                # the same atomic write. Thus, only 1 chip reads "running"
                # at a time (Glenn, 2026-08-20). The job chip goes to
                # "queued" while its sub-stage runs. It returns to
                # "started" after. This is an update only. This never
                # creates a sibling.

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

                # The FIRST start of the file is the sort anchor of the
                # running banners (the original banner-ordering request
                # of Glenn). It never moves after it is set. Thus, a file
                # that steps between queues keeps its place.

                if status == "started":
                    pipe.hsetnx(key, "first_run", now)

                # The first ENQUEUE of the file anchors the Enqueued
                # column of the queue page in the same way. It stays still
                # while the work steps between queues. Each step is a new
                # job with its own enqueued_at. Fitzflix stores it as
                # naive UTC to match the enqueued_at convention of rq.
                # The other stamps of the trail are local wall clock, for
                # the chips. See first_enqueued.

                if status in ("queued", "scheduled"):
                    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                    if fresh_journey:
                        pipe.hset(key, "first_enqueued", stamp)
                    else:
                        pipe.hsetnx(key, "first_enqueued", stamp)

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

    try:
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
                    pipe.delete(old_key)
                    pipe.zrem(ACTIVE_KEY, old_digest)
                    pipe.zadd(ACTIVE_KEY, {new_digest: time.time()})
                    pipe.zremrangebyrank(ACTIVE_KEY, 0, -(ACTIVE_LIMIT + 1))
                    pipe.execute()
                    return
                except WatchError:
                    continue
    except Exception:
        try:
            from flask import current_app

            current_app.logger.warning(traceback.format_exc())
        except Exception:
            pass


def record_job_event(connection, job, event):
    """Append or update 1 stage entry on the file trail of the job.

    This is advisory only. It logs and catches each failure. It never
    shows a failure to the pipeline."""

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

    try:
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
    except Exception:
        try:
            from flask import current_app

            current_app.logger.warning(traceback.format_exc())
        except Exception:
            pass


def first_run(connection, job):
    """Return the time when the FILE of the job first started a stage.

    This returns None for a job outside the pipeline, or for a file
    with no recorded start. The running banners sort by this. Thus, a
    file that steps between queues holds its position. Each step is a
    new job with a new started_at."""

    try:
        found = _stage_for(job)
        if found is None:
            return None
        basename = _resolve_basename(connection, found[0])
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
        found = _stage_for(job)
        if found is None:
            return None
        basename = _resolve_basename(connection, found[0])
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


def _heal_orphaned_entries(connection, digest, entries):
    """Remove the trail entries of jobs that rq does not know.

    A queued job that is cancelled and deleted outside the pipeline
    leaves its "queued" stamp on the trail for ever. Nothing advances
    it. Thus, the queue page shows a phantom job until the TTL of the
    full trail expires. Examples are the 9 cancelled scaffold
    re-archives of 2026-08-29, and the tenth one found on 2026-08-31.
    Fitzflix writes the stamp only after the own job hash of rq exists.
    Thus, a waiting or running chip with no job hash is orphaned. It is
    not early.

    This removes those entries. Terminal chips (done, failed) are
    history and always stay. This returns the entries that remain, or
    None if the full trail was orphaned and deleted. This is advisory,
    like each trail write. On a failure, it returns the entries as they
    were.
    """

    from redis import WatchError

    waiting = ("queued", "scheduled", "started")
    try:
        candidates = {
            entry.get("job")
            for entry in entries
            if entry.get("status") in waiting and entry.get("job")
        }
        if not candidates or all(
            connection.exists(f"rq:job:{job_id}") for job_id in candidates
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
                kept = [
                    entry
                    for entry in trail
                    if not (
                        entry.get("status") in waiting
                        and entry.get("job") in candidates
                        and not pipe.exists(f"rq:job:{entry.get('job')}")
                    )
                ]
                if len(kept) == len(trail):
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
    except Exception:
        try:
            from flask import current_app

            current_app.logger.warning(traceback.format_exc())
        except Exception:
            pass
        return entries


def pipeline_trails(connection, limit=25):
    """Return the newest file trails for the queue page, most recent
    first: [{basename, updated, entries: [{stage, status, at, job}, ...]}, ...].

    Each entry carries the rq job id that stamped it. A queue-page row
    finds the trail of its own file by this id. The match is exact. No
    basename-versus-description match can become stale."""

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
            entries = [
                {
                    "stage": entry.get("stage"),
                    "status": entry.get("status"),
                    "at": entry.get("at"),
                    "job": entry.get("job"),
                }
                for entry in raw_entries
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


class PipelineWorker(SimpleWorker):
    """A SimpleWorker that stamps the trail around the execution.

    It stamps started when it picks up a job. It stamps done or failed
    when the job completes."""

    def execute_job(self, job, queue):
        """Stamp started. Then run the job as SimpleWorker does."""

        record_job_event(self.connection, job, "started")
        return super().execute_job(job, queue)

    def handle_job_success(self, job, queue, started_job_registry):
        """Run the success handling of rq. Then stamp the trail done."""

        super().handle_job_success(job, queue, started_job_registry)
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
