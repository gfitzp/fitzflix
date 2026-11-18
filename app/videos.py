"""The code that the file tasks share.

This module has the file copy with progress and with retries after
transient errors. It has the acquire-or-defer sequence for the title
lock. It also has the control of the command-line tools and the probe
for a dead volume.

No task lives here. The name of a job is the module that defines its
function, for example "app.importing.localization_task".
"""

import errno
import json
import os
import subprocess
import time

from flask import current_app
from werkzeug.local import LocalProxy

from app import get_app, path_tag, safe_job_id
from app.tasks import report_progress, requeue
from app.maintenance import VOLUMES_ROOT, volume_alive

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


def copy_with_progress(src, dst, job, name, activity="Moving into the library"):
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
                    report_progress(job, name, activity, percent)
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
    key=None,
):
    """Take the redlock for a title, or schedule the task to retry later.

    This function returns the lock on success. It returns None after it
    schedules the retry. The retry goes into the native
    ScheduledJobRegistry of the queue. The mover in scheduler.py enqueues
    the retry when it is due. key makes the retry id unique to 1 file
    (see schedule_retry).
    """

    lock = current_app.lock_manager.lock(resource, ttl_ms)
    if lock:
        current_app.logger.info(f"Created lock {lock}")
        return lock

    schedule_retry(
        queue,
        func,
        minutes,
        timeout,
        description,
        "Lock exists",
        args,
        kwargs,
        key=key,
    )
    return None


# The tasks whose lock wait id holds a digest of the arguments (#595).
# Each is an edit of 1 file with arguments that differ per edit. Under a
# fixed id, a second deferred edit of the file replaces the first.

ARGUMENT_TAGGED_RETRIES = frozenset(
    {"mkvpropedit_task", "mkvmerge_task", "remux_audio_plan_task"}
)


def arguments_tag(args):
    """Return a short digest of the positional arguments of a task (#595).

    The same arguments give the same digest. Thus, a repeat deferral of
    1 edit still replaces its own pending retry."""

    return path_tag(json.dumps(list(args or ()), sort_keys=True, default=repr))


def lock_wait_job_id(func, description, args=(), key=None):
    """Return the job id of the lock wait of a task (see schedule_retry).

    With a key, the id starts with the requeue prefix of the key, like
    every other retry. A repeat deferral of a task for 1 file replaces
    its pending retry. The track edits put a digest of their arguments
    into the id (#595). Thus, 2 different edits of 1 file wait apart."""

    short_name = func.rsplit(".", 1)[-1]
    edit_tag = (
        f"{arguments_tag(args)}:" if short_name in ARGUMENT_TAGGED_RETRIES else ""
    )
    return safe_job_id(
        f"retry:{short_name}:"
        + (f"{path_tag(key)}:" if key is not None else "")
        + edit_tag
        + description
    )


def schedule_retry(
    queue, func, minutes, timeout, description, reason, args=(), kwargs=None, key=None
):
    """Put a task back on its queue after a random delay.

    The delay is a random number of minutes in the given range. The
    deterministic id makes a repeat deferral replace the pending retry.
    It does not add a new one. The result ttl of 1 day keeps the record
    of a finished retry alive. Thus, a retry that it scheduled itself can
    still find that record. The function returns the delay in minutes.

    key (for example, a file id) goes into the id with a digest (#338). The
    description holds a basename, and 2 files can have the same basename.
    Without the key, the retry of 1 file replaces the retry of the other.
    """

    delay = requeue(
        func,
        key=key,
        delay=minutes,
        reason=reason,
        args=args,
        kwargs=kwargs,
        description=description,
        queue=queue,
        timeout=timeout,
        job_id=lock_wait_job_id(func, description, args, key=key),
    )
    return int(delay.total_seconds() // 60)


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
