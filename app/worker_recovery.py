"""Reclaim the work of a pipeline worker that died without cleanup (#498).

A SIGKILL (the end of the supervisor stop wait, a reboot, an OOM kill)
ends a worker before its job can clean up. rq keeps the worker hash,
the execution and the started entry for the full job timeout. That is
24 hours for an import and 48 hours for a transcode. During that time,
every busy check sees the dead job as running. The title lock blocks the
title, the staging reservation stays, and the manual-import marker
hides the Import button.

Each worker runs reclaim_dead_workers at its start, before it takes a
job. A worker hash of this host with no death and a dead process is a
dead worker. Its current job fails through the failure path of the
worker. Thus, the trail of the file is stamped. The abandon handler of
the stage releases the title lock and the staging reservation. The
source stays in the import directory for the hourly sweep. Then the
hash gets its death.
"""

import json
import os
import socket
import traceback

from flask import current_app
from redlock import Lock
from rq import Queue
from rq.job import JobStatus

# Only 1 starting worker reclaims a dead worker. The others skip it.

RECLAIM_KEY = "fitzflix:worker-reclaim:{name}"
RECLAIM_TTL = 600

DEAD_WORKER_REASON = (
    "The worker process died during the job (SIGKILL, a reboot, or an OOM "
    "kill). A new worker reclaimed it at its start (#498)."
)

# The title lock of a localization lives in this key, by job id. The
# later stages carry the lock as their third argument.

JOB_LOCK_KEY = "fitzflix:worker-reclaim:lock:{job_id}"
LATER_STAGE_LOCK_ARGUMENT = {
    "app.videos.convert_localized_file",
    "app.videos.move_localized_file",
    "app.videos.finalize_localization",
}
MANUAL_IMPORT_TASK = "app.manual_imports.manual_import_task"


def _pid_alive(pid):
    """Return True if a process with this pid exists."""

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _dead_workers(connection, worker_class):
    """Yield each worker of this host whose process is gone and has no death."""

    hostname = socket.gethostname()
    for worker in worker_class.all(connection=connection):
        if worker.hostname != hostname or not worker.pid:
            continue
        if worker.pid == os.getpid() or _pid_alive(worker.pid):
            continue
        if connection.hexists(worker.key, "death"):
            continue
        yield worker


def note_job_lock(job_id, lock, ttl_seconds):
    """Record the title lock that a job took. Its key expires with the lock."""

    current_app.redis.set(
        JOB_LOCK_KEY.format(job_id=job_id),
        json.dumps([lock.resource, _text(lock.key)]),
        ex=ttl_seconds,
    )


def _text(value):
    """Return value as text. A redlock token can be bytes."""

    return value.decode() if isinstance(value, bytes) else value


def _job_lock(job):
    """Return the title lock that the job holds, or None."""

    if job.func_name in LATER_STAGE_LOCK_ARGUMENT:
        lock = job.args[2] if len(job.args) > 2 else job.kwargs.get("lock")
        return lock or None
    payload = current_app.redis.get(JOB_LOCK_KEY.format(job_id=job.id))
    try:
        held = json.loads(payload) if payload else None
    except ValueError:
        held = None
    if held and len(held) == 2:
        return Lock(0, held[0], held[1])
    return None


def _source_of(job):
    """Return the source path of a pipeline job, or None."""

    if job.args and isinstance(job.args[0], str):
        return job.args[0]
    return (job.kwargs or {}).get("file_path") or (job.kwargs or {}).get("source_path")


def _next_stage_holds(job, source):
    """Return True if a different later-stage job holds the source."""

    from app.importing import _later_stage_jobs

    return any(
        other.id != job.id and _source_of(other) == source
        for other in _later_stage_jobs()
    )


def abandon_dead_job(job):
    """Run the cleanup of the stage of a job whose worker died.

    The source always stays for the hourly sweep. The localized output of
    a conversion or a move goes. The cataloging keeps its files, because
    its commit can be done."""

    from app.aws_storage import pending_archive_of
    from app.importing import abandon_import
    from app.naming import hidden
    from app.manual_imports import STARTED_KEY

    if job.func_name == MANUAL_IMPORT_TASK and len(job.args) > 1:
        current_app.redis.delete(STARTED_KEY.format(download_id=job.args[1]))
        return

    lock = _job_lock(job)
    source = _source_of(job)

    # The job can die after it queued the next stage. Then the next stage
    # holds the lock, the reservation and the output. Fail only the job.

    if source and _next_stage_holds(job, source):
        return
    if job.func_name == "app.videos.localization_task" and source:
        abandon_import(source, lock, keep_source=True)
        return
    if job.func_name not in LATER_STAGE_LOCK_ARGUMENT or not source:
        return

    details = job.args[1] if len(job.args) > 1 else {}
    reservation = details.get("untouched_basename") or os.path.basename(source)
    if job.func_name == "app.videos.finalize_localization":
        current_app.logger.warning(
            f"'{os.path.basename(source)}' The cataloging died. Its files stay. "
            f"Check the record before the next sweep imports the source again"
        )
        abandon_import(source, lock, reservation=reservation, keep_source=True)
        return

    strays = [job.args[3]] if len(job.args) > 3 else []
    if job.func_name == "app.videos.convert_localized_file" and strays:
        strays += [f"{strays[0]}.sdr-video.mkv", f"{strays[0]}.sdr.mkv"]
    if job.func_name == "app.videos.move_localized_file" and details.get("dirname"):
        strays.append(
            hidden(
                os.path.join(
                    current_app.config["LIBRARY_DIR"],
                    details["dirname"],
                    details.get("basename") or os.path.basename(source),
                )
            )
        )
    abandon_import(
        source,
        lock,
        strays=strays,
        reservation=reservation,
        keep_source=True,
        pending_archive=pending_archive_of(details),
    )


def reclaim_dead_workers(connection, worker_class):
    """Fail the jobs of the dead workers of this host and clean up. Return their names."""

    reclaimed = []
    for worker in _dead_workers(connection, worker_class):
        if not connection.set(
            RECLAIM_KEY.format(name=worker.name), 1, nx=True, ex=RECLAIM_TTL
        ):
            continue
        try:
            job = worker.get_current_job()
        except Exception:
            job = None
        if (
            job is not None
            and job.get_status(refresh=True) == JobStatus.STARTED
            and job.worker_name == worker.name
        ):
            current_app.logger.warning(
                f"Worker {worker.name} (pid {worker.pid}) died during job "
                f"{job.id} ({job.func_name}). Reclaiming it"
            )
            try:
                abandon_dead_job(job)
            except Exception:
                current_app.logger.error(traceback.format_exc())
            executions = job.get_executions()
            worker.execution = executions[0] if executions else None
            with connection.pipeline() as pipeline:
                for execution in executions[1:]:
                    execution.delete(job=job, pipeline=pipeline)
                pipeline.execute()
            worker.handle_job_failure(
                job,
                queue=Queue(job.origin, connection=connection),
                exc_string=DEAD_WORKER_REASON,
            )
        worker.register_death()
        reclaimed.append(worker.name)
    return reclaimed
