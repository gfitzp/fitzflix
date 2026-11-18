"""The free space for an import: the library volume and the reservations of the staging area."""

import errno
import json
import os
import shutil
import time
from datetime import timedelta
from flask import current_app
from app.media.naming import hidden

# An import waits for free space on the library volume. It is never
# rejected for space. The check asks for the size of the source
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


# Staging reservations. Up to 5 workers localize at the same
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
    """Record or change the staging reservation of an import."""

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
    # Then 2 imports can take the same space. The import tries
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
