"""Probe library files for the SMB lost-handle state.

The NAS sometimes loses its handle for a single file. Every read
continues to succeed. But close(2) answers EBADF for that file until
the state clears. The state clears on its own, or when the user
remounts the share. The state is invisible until something treats
close() as part of a copy. The final close of an upload reports it
from inside s3transfer. Thus, it looks like an S3 error. It is better
to ask each file directly than to wait for a 28GB transfer to find it.

The question costs 1 open and 1 close. Thus, a task can ask after
every file that it writes. The results go into Redis. Thus, a bulk run
reports which files it broke. A later recheck can measure how long the
state lasts. Nothing has recorded that duration before.

That measurement is the reason why Fitzflix records a recovery instead
of a simple deletion. The next task that touches a file will probably
get a clean probe. If that erased the record, the duration would be
gone before a recheck could read it. That is exactly how the first real
measurement was lost. Thus, a clean probe of a known-broken file
converts its entry into a healed record. `recheck` reports those
records and then removes them.
"""

import errno
import json
import os
from datetime import datetime, timezone
from typing import NamedTuple

from flask import current_app

STATE_KEY = "fitzflix:smb:handle_state"

# Fitzflix reports a recovery one time and then removes it from the
# state. Without this list, the durations would exist only in the
# terminal that ran the recheck. They accumulate here instead. The
# state says what is broken. The history says what the state has ever
# cost. Only the history answers how long the state lasts.

HISTORY_KEY = "fitzflix:smb:handle_history"
HISTORY_LIMIT = 1000

# A record describes one of 2 things: a file that fails its probe now,
# or a file that recovered and holds its duration until a recheck
# reports it.

FAILING = "failing"
HEALED = "healed"


def _load_entry(path):
    """Return the recorded entry for a path, or None if there is no usable entry."""

    payload = current_app.redis.hget(STATE_KEY, path)
    if not payload:
        return None

    if isinstance(payload, bytes):
        payload = payload.decode()

    try:
        return json.loads(payload)
    except ValueError:
        return None


def _entry_state(entry):
    """Return the state of an entry.

    An entry written before Fitzflix recorded recoveries is a failure."""

    return entry.get("state", FAILING)


def _held_for_seconds(first_seen, healed_at):
    """Return how long the state held, as far as the record can tell.

    This is a minimum, not a measurement of the start. first_seen is
    the time when something first asked. The file was already broken
    at that time.
    """

    try:
        return (
            datetime.fromisoformat(healed_at) - datetime.fromisoformat(first_seen)
        ).total_seconds()
    except (TypeError, ValueError):
        return None


def probe_path(path):
    """Open `path` read-only, close it, and report what failed.

    Return a dict with `ok`. On failure, the dict also has the `stage`
    that raised ("open" or "close") and its errno. A file that opens
    and closes cleanly is healthy. A file that opens but does not close
    is in the lost-handle state. A file that does not open has a
    different problem, such as a missing file or a dead mount. The
    result says so. It does not count as a handle failure.
    """

    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError as e:
        return {
            "path": path,
            "ok": False,
            "stage": "open",
            "errno": e.errno,
            "message": f"open failed: {e.strerror} (errno {e.errno})",
        }

    try:
        os.close(fd)
    except OSError as e:
        # Do not retry the close. The kernel already released the
        # descriptor. A second attempt on a reused number would close
        # the file of a different caller.

        return {
            "path": path,
            "ok": False,
            "stage": "close",
            "errno": e.errno,
            "message": f"close failed: {e.strerror} (errno {e.errno})",
        }

    return {"path": path, "ok": True, "stage": None, "errno": None, "message": "ok"}


def lost_handle(result):
    """Return True if a probe result is the lost-handle state specifically."""

    return (
        not result["ok"]
        and result["stage"] == "close"
        and result["errno"] == errno.EBADF
    )


def absent(result):
    """Return True if the file is not on the local volume.

    This is not a finding. A File row lives longer than its local copy.
    When a better edition replaces one, the row and its S3 archive stay.
    The local file goes away. Thus, thousands of rows are legitimately
    absent. Without this check, they would hide the real failures.
    """

    return (
        not result["ok"]
        and result["stage"] == "open"
        and result["errno"] == errno.ENOENT
    )


def share_root(path):
    """Return the share that a library path is on.

    A library path is LIBRARY_DIR plus a share name, for example
    /Volumes/Movies or /Volumes/TV Shows. Thus, the share is the first
    component below the configured library directory. For a path from
    a different location, this function returns the library directory
    itself.
    """

    library_dir = current_app.config["LIBRARY_DIR"]
    first = os.path.relpath(path, library_dir).split(os.sep)[0]

    if first in ("", os.curdir, os.pardir):
        return library_dir

    return os.path.join(library_dir, first)


def share_available(path):
    """Return True if the share of a path is there now.

    An existing directory is not sufficient (#232). macOS usually
    deletes the mount point when a share unmounts cleanly. The original
    isdir check depended on that. But when the SMB session dies, macOS
    leaves the mount point behind as a normal directory on the boot
    disk. Then isdir answers True for a share that is not there. Seen
    on 2026-08-25: /Volumes/TV Shows was an empty stub for approximately
    25 minutes while the real share ran at /Volumes/TV Shows-1.

    That is the one case that this function exists to catch. If it
    answers True for a dead share, absent() reports every file on it as
    a legitimate departure. That is thousands at one time, none of them
    a finding, and recheck removes their durations. Thus, a path under
    /Volumes must BE a mountpoint, exactly as #227 established for
    volume_alive.

    A library that is not under /Volumes still gets a correct answer.
    The mountpoint requirement applies only below VOLUMES_ROOT.
    """

    # Import here, not at module scope. maintenance imports the app
    # factory. This module stays light enough for a CLI or a test to
    # import on its own.

    from app.maintenance import mountpoint_ok

    share = share_root(path)
    try:
        return os.path.isdir(share) and mountpoint_ok(share)
    except OSError:
        return False


def share_responsive(path):
    """Return True if the share of a path answers in a timeout AND is mounted.

    share_available asks "is it there". This function adds "will a
    touch hang" (#237). A WEDGED share is still in the mount table, but
    its syscalls hang. It stalls the next os.open until the job timeout
    of the caller kills it. Thus, a task that will probe many files
    must ask this 1 time per share first, through the watchdog thread of
    volume_alive. A path outside /Volumes gets its answer from a plain
    and safe statvfs.
    """

    # Import here, not at module scope, the same as mountpoint_ok above.
    # maintenance imports the app factory.

    from app.maintenance import volume_alive

    return volume_alive(share_root(path))


def unmounted(result):
    """Return True if an ENOENT means that the share is gone, not the file.

    The difference is very important. One missing file is routine. A
    missing share makes every file look deleted at one time. If
    Fitzflix treated that as thousands of departures, it would drop the
    whole record. That includes the durations that exist nowhere else.
    """

    return absent(result) and not share_available(result["path"])


def library_path(file):
    """Return the absolute path of a File row on the library volume."""

    return os.path.join(current_app.config["LIBRARY_DIR"], file.file_path)


def record_result(result, context=None):
    """Merge a probe result into the recorded state.

    A failure keeps the timestamp of its first sighting. Thus, the record
    can say how long the state has held. A clean probe of a known-broken
    file does not discard that timestamp. It rewrites the entry as a
    healed record with `healed_at` and `held_for_seconds`. That record
    stays until a recheck reports it.

    A clean probe of a file that has no record stays unrecorded. Healthy
    files are the large majority and are not news. A file that is not on
    the volume at all also gets no record.

    Return the entry that this function wrote, or None if it wrote
    nothing.
    """

    if absent(result):
        # There is nothing to say about the handle of a file that is not
        # there. recheck drops a recorded file that went away later.

        return None

    path = result["path"]
    now = datetime.now(timezone.utc).isoformat()
    existing = _load_entry(path)

    if result["ok"]:
        if existing is None or _entry_state(existing) == HEALED:
            return None

        first_seen = existing.get("first_seen", now)
        entry = dict(existing)
        entry.update(
            {
                "state": HEALED,
                "first_seen": first_seen,
                "healed_at": now,
                "healed_by": context,
                "held_for_seconds": _held_for_seconds(first_seen, now),
            }
        )
        current_app.redis.hset(STATE_KEY, path, json.dumps(entry))
        _append_history(path, entry)
        return entry

    # A file that broke again after a recovery is a new episode. Thus,
    # it starts a new clock. It does not inherit the old one.

    first_seen = now
    if existing is not None and _entry_state(existing) == FAILING:
        first_seen = existing.get("first_seen", now)

    entry = {
        "state": FAILING,
        "stage": result["stage"],
        "errno": result["errno"],
        "message": result["message"],
        "context": context,
        "first_seen": first_seen,
        "last_seen": now,
    }
    current_app.redis.hset(STATE_KEY, path, json.dumps(entry))
    return entry


def forget(path):
    """Drop the record of a file completely, after the report is done."""

    current_app.redis.hdel(STATE_KEY, path)


def _append_history(path, entry):
    """Record one completed episode, permanently.

    This is the only durable trace. The next recheck removes the state
    entry that it came from. This function swallows failures for the
    same reason that the probe swallows its own. A lost measurement is
    bad. A failed task that produced it is worse.
    """

    try:
        current_app.redis.rpush(
            HISTORY_KEY,
            json.dumps(
                {
                    "path": path,
                    "stage": entry.get("stage"),
                    "errno": entry.get("errno"),
                    "context": entry.get("context"),
                    "healed_by": entry.get("healed_by"),
                    "first_seen": entry.get("first_seen"),
                    "healed_at": entry.get("healed_at"),
                    "held_for_seconds": entry.get("held_for_seconds"),
                }
            ),
        )
        current_app.redis.ltrim(HISTORY_KEY, -HISTORY_LIMIT, -1)
    except Exception:
        current_app.logger.warning(
            f"Could not record the SMB recovery of '{path}' to history",
            exc_info=True,
        )


def history():
    """Return every recovery ever recorded, oldest first."""

    episodes = []
    for payload in current_app.redis.lrange(HISTORY_KEY, 0, -1):
        payload = payload.decode() if isinstance(payload, bytes) else payload
        try:
            episodes.append(json.loads(payload))
        except ValueError:
            continue
    return episodes


def probe_and_record(path, context=None):
    """Probe a path, record the result, and log a failure.

    This function never raises. It runs after work that already
    succeeded. A diagnostic that can fail the task it reports on is
    worse than no diagnostic. Return the probe result, or None if the
    probe itself could not run.
    """

    try:
        result = probe_path(path)
        entry = record_result(result, context=context)
    except Exception:
        current_app.logger.warning(
            f"SMB handle probe could not run against '{path}'", exc_info=True
        )
        return None

    if entry is not None and _entry_state(entry) == HEALED:
        held = entry.get("held_for_seconds")
        duration = f" after at least {held / 60:.0f} minute(s)" if held else ""
        current_app.logger.info(
            f"'{os.path.basename(path)}' has come out of the SMB lost-handle "
            f"state{duration} (seen by {context or 'an unnamed task'}); "
            f"'flask smb recheck' will report it"
        )

    if lost_handle(result):
        current_app.logger.warning(
            f"'{os.path.basename(path)}' is in the SMB lost-handle state "
            f"(close returned EBADF) after {context or 'an unnamed task'}; "
            f"reads still work, but anything that closes this file will fail "
            f"until the NAS clears it"
        )
    elif not result["ok"]:
        current_app.logger.warning(
            f"'{os.path.basename(path)}' failed its SMB probe: "
            f"{result['message']} (after {context or 'an unnamed task'})"
        )

    return result


def recorded_state(state=None):
    """Return every recorded file keyed by path, optionally filtered to one state.

    Without a filter, this returns failures and unreported recoveries
    together. Thus, a caller that wants one or the other must say which.
    """

    entries = {}
    for path, payload in current_app.redis.hgetall(STATE_KEY).items():
        path = path.decode() if isinstance(path, bytes) else path
        payload = payload.decode() if isinstance(payload, bytes) else payload
        try:
            entry = json.loads(payload)
        except ValueError:
            continue

        entry.setdefault("state", FAILING)
        if state is None or entry["state"] == state:
            entries[path] = entry

    return entries


def failing_state():
    """Return the files that fail their probe now."""

    return recorded_state(FAILING)


def healed_state():
    """Return the recoveries that are recorded but not yet reported by a recheck."""

    return recorded_state(HEALED)


def _healed_result(path, entry):
    """Return a recovery with the shape of a probe result, for the callers."""

    return {
        "path": path,
        "ok": True,
        "stage": None,
        "errno": None,
        "message": "ok",
        "context": entry.get("context"),
        "first_seen": entry.get("first_seen"),
        "healed_at": entry.get("healed_at"),
        "healed_by": entry.get("healed_by"),
        "held_for_seconds": entry.get("held_for_seconds"),
    }


class RecheckReport(NamedTuple):
    """Hold what one recheck found.

    The fields are named because 4 buckets are too many for positional
    unpacking to read clearly."""

    healed: list
    still_failing: list
    gone: list
    skipped: list


def recheck():
    """Probe the recorded failures again and collect every recovery.

    Return a RecheckReport of result-dict lists. A healed result has
    `held_for_seconds`, the time that the file spent in the state. That
    is the number that the investigation wants.

    Two kinds of recovery arrive here: a file that this recheck found
    closing cleanly, and a file whose recovery the probe of a task
    already recorded. This function reports both 1 time and then removes
    them. Thus, what stays is a description of what is broken now.

    A recorded file that left the volume later goes into `gone`, not
    into the other buckets. It did not recover. It cannot still be
    stuck. Thus, permanent tracking would be the wrong answer two times.

    A file whose SHARE is unmounted goes into `skipped` and keeps its
    record unchanged. Every file on an unmounted share reports ENOENT at
    one time. If Fitzflix took that for departure, it would drop the
    whole record, with the durations that exist nowhere else, when a
    recheck ran during a remount. This function health-checks each share
    ONCE, through a watchdog, before it probes the files of that share
    (#237). A wedged share hangs the open that the probe would make.
    Thus, a direct question to the file is what must not occur.
    """

    healed = []
    still_failing = []
    gone = []
    skipped = []
    responsive = {}

    for path, entry in recorded_state().items():
        if _entry_state(entry) == HEALED:
            healed.append(_healed_result(path, entry))
            forget(path)
            continue

        share = share_root(path)
        if share not in responsive:
            responsive[share] = share_responsive(path)
        if not responsive[share]:
            skipped.append(
                {
                    "path": path,
                    "ok": False,
                    "stage": "share",
                    "errno": None,
                    "message": "share not mounted or not responding",
                    "first_seen": entry.get("first_seen"),
                    "share": share,
                }
            )
            continue

        result = probe_path(path)

        if unmounted(result):
            result["first_seen"] = entry.get("first_seen")
            result["share"] = share_root(path)
            skipped.append(result)

        elif absent(result):
            result["first_seen"] = entry.get("first_seen")
            gone.append(result)
            forget(path)

        elif result["ok"]:
            # record_result carries the context of the failing entry
            # forward. Thus, this keeps which task broke the file and adds
            # which task found it healed.

            recorded = record_result(result, context="recheck")
            healed.append(_healed_result(path, recorded or entry))
            forget(path)

        else:
            record_result(result, context=entry.get("context"))
            result["first_seen"] = entry.get("first_seen")
            still_failing.append(result)

    return RecheckReport(healed, still_failing, gone, skipped)
