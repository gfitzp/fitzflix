"""Probe library files for the SMB lost-handle state.

The NAS sometimes loses its handle for a single file: every read still
succeeds and close(2) answers EBADF forever, per file, until the state
clears — on its own, or when the share is remounted. It is invisible until something treats close()
as part of a copy — an upload's final close reports it from inside
s3transfer, so it reads as an S3 error — which makes it worth asking
each file directly instead of waiting for a 28GB transfer to discover it.

The question costs one open and one close, so a task can afford to ask
after every file it writes. Results land in Redis so a bulk run reports
which files it broke, and so a later recheck can measure how long the
state actually lasts, which nothing has ever recorded.

That measurement is why a recovery is recorded rather than simply
deleted. Whichever task next touches a file is likely to probe it
cleanly, and if that erased the record the duration would be gone before
any recheck could read it — which is exactly how the first real
measurement was lost. A clean probe of a known-broken file therefore
converts its entry into a healed record; `recheck` reports those and
reaps them.
"""

import errno
import json
import os
from datetime import datetime, timezone
from typing import NamedTuple

from flask import current_app

STATE_KEY = "fitzflix:smb:handle_state"

# Recoveries are reported once and reaped from the state, so the durations
# would live nowhere but the terminal that happened to run the recheck.
# They accumulate here instead: the state says what is broken, the history
# says what the state has ever cost, and only the history answers how long
# this lasts.

HISTORY_KEY = "fitzflix:smb:handle_history"
HISTORY_LIMIT = 1000

# What a record describes: a file failing its probe now, or one that has
# recovered and is holding its duration until a recheck reports it

FAILING = "failing"
HEALED = "healed"


def _load_entry(path):
    """The recorded entry for a path, or None if there isn't a usable one."""

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
    """An entry written before recoveries were recorded is a failure."""

    return entry.get("state", FAILING)


def _held_for_seconds(first_seen, healed_at):
    """How long the state held, as far as the record can tell.

    A floor, not a measurement of onset: first_seen is when something
    first asked, and the file was already broken by then.
    """

    try:
        return (
            datetime.fromisoformat(healed_at) - datetime.fromisoformat(first_seen)
        ).total_seconds()
    except (TypeError, ValueError):
        return None


def probe_path(path):
    """Open `path` read-only and close it, reporting what failed.

    Returns a dict with `ok`, and on failure the `stage` that raised
    ("open" or "close") along with its errno. A file that opens and
    closes cleanly is healthy; one that opens but won't close is in the
    lost-handle state; one that won't open at all is a different problem
    (missing file, dead mount) and says so rather than being counted as
    a handle failure.
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
        # Don't retry the close: the descriptor is already gone as far as
        # the kernel is concerned, and a second attempt on a reused number
        # would close somebody else's file

        return {
            "path": path,
            "ok": False,
            "stage": "close",
            "errno": e.errno,
            "message": f"close failed: {e.strerror} (errno {e.errno})",
        }

    return {"path": path, "ok": True, "stage": None, "errno": None, "message": "ok"}


def lost_handle(result):
    """Whether a probe result is the lost-handle state specifically."""

    return (
        not result["ok"]
        and result["stage"] == "close"
        and result["errno"] == errno.EBADF
    )


def absent(result):
    """Whether the file simply isn't on the local volume.

    Not a finding. A File row outlives its local copy: when a better
    edition supersedes one, the row and its S3 archive stay while the
    local file goes away, so thousands of rows are legitimately absent
    and would otherwise drown the real failures.
    """

    return (
        not result["ok"]
        and result["stage"] == "open"
        and result["errno"] == errno.ENOENT
    )


def share_root(path):
    """The share a library path lives on.

    Library paths are LIBRARY_DIR plus a share name — /Volumes/Movies,
    /Volumes/TV Shows — so the share is the first component below the
    configured library directory. A path from somewhere else answers with
    the library directory itself.
    """

    library_dir = current_app.config["LIBRARY_DIR"]
    first = os.path.relpath(path, library_dir).split(os.sep)[0]

    if first in ("", os.curdir, os.pardir):
        return library_dir

    return os.path.join(library_dir, first)


def share_available(path):
    """Whether the share a path belongs to is currently there.

    The directory existing is not enough (#232). macOS does usually
    delete the mount point when a share unmounts cleanly, which is what
    the original isdir check relied on — but when the SMB session dies it
    leaves the mount point behind as an ordinary directory on the boot
    disk, and isdir then answers True for a share that is not there.
    Seen Aug 25 2026: /Volumes/TV Shows sat as an empty stub for ~25
    minutes while the real share ran at /Volumes/TV Shows-1.

    That is the one case this function exists to catch. If it answers
    True for a dead share, absent() claims every file on it as a
    legitimate departure — thousands at once, none of them a finding,
    and recheck reaps their durations. So a path under /Volumes has to
    BE a mountpoint, exactly as #227 established for volume_alive.

    A library that isn't under /Volumes still answers correctly: the
    mountpoint requirement only applies below VOLUMES_ROOT.
    """

    # Imported here rather than at module scope: maintenance pulls in the
    # app factory, and this module stays light enough for a CLI or a test
    # to import on its own

    from app.maintenance import mountpoint_ok

    share = share_root(path)
    try:
        return os.path.isdir(share) and mountpoint_ok(share)
    except OSError:
        return False


def share_responsive(path):
    """Whether the share behind a path answers within a timeout AND is
    really mounted.

    share_available asks "is it there"; this adds "will touching it
    hang" (#237). A WEDGED share — still in the mount table, hanging
    syscalls — stalls the very next os.open until the caller's job
    timeout kills it, so anything about to probe many files must ask
    this once per share first, through volume_alive's watchdog thread.
    A path outside /Volumes answers from a plain (and safe) statvfs.
    """

    # Imported here rather than at module scope, like mountpoint_ok
    # above: maintenance pulls in the app factory

    from app.maintenance import volume_alive

    return volume_alive(share_root(path))


def unmounted(result):
    """Whether an ENOENT means the share is gone, not the file.

    The difference matters enormously: one missing file is routine, a
    missing share makes every file look deleted at once, and treating
    that as thousands of departures would drop the whole record —
    including durations that exist nowhere else.
    """

    return absent(result) and not share_available(result["path"])


def library_path(file):
    """The absolute path of a File row on the library volume."""

    return os.path.join(current_app.config["LIBRARY_DIR"], file.file_path)


def record_result(result, context=None):
    """Fold a probe result into the recorded state.

    A failure keeps the timestamp of its first sighting, so the record can
    say how long the state has held. A clean probe of a file already known
    to be broken does not throw that timestamp away: it rewrites the entry
    as a healed record carrying `healed_at` and `held_for_seconds`, which
    survives until a recheck reports it.

    A clean probe of a file nobody recorded stays unrecorded — healthy
    files are the overwhelming majority and are not news — and a file
    that isn't on the volume at all is not recorded either.

    Returns the entry it wrote, or None when it wrote nothing.
    """

    if absent(result):
        # Nothing to say about the handle of a file that isn't there;
        # recheck is what drops a recorded file that has since gone away

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

    # A file that broke again after recovering is a new episode, so it
    # starts a new clock rather than inheriting the old one

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
    """Drop a file's record entirely, reported and done with."""

    current_app.redis.hdel(STATE_KEY, path)


def _append_history(path, entry):
    """Record one completed episode, permanently.

    This is the only durable trace: the state entry it came from is
    reaped by the next recheck. Failures here are swallowed for the same
    reason the probe swallows its own — losing a measurement is bad, but
    failing the task that produced it is worse.
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
    """Every recovery ever recorded, oldest first."""

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

    Never raises: this runs after work that has already succeeded, and a
    diagnostic that can fail the task it's reporting on is worse than no
    diagnostic. Returns the probe result, or None if the probe itself
    couldn't run.
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
    """Every recorded file keyed by path, optionally filtered to one state.

    Unfiltered this returns failures and unreported recoveries together,
    so callers that mean one or the other should say which.
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
    """The files failing their probe right now."""

    return recorded_state(FAILING)


def healed_state():
    """Recoveries recorded but not yet reported by a recheck."""

    return recorded_state(HEALED)


def _healed_result(path, entry):
    """A recovery shaped like a probe result, so callers treat it as one."""

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
    """What one recheck found. Named because four buckets is past the
    point where positional unpacking reads as anything."""

    healed: list
    still_failing: list
    gone: list
    skipped: list


def recheck():
    """Re-probe the recorded failures and collect every recovery.

    Returns a RecheckReport of result-dict lists. A healed result carries
    `held_for_seconds` — how long the file spent in the
    state — which is the number the investigation actually wants.

    Two kinds of recovery land here: a file this recheck found closing
    cleanly, and one whose recovery a task's own probe already recorded
    in the meantime. Both are reported once and then reaped, so what
    stays behind is a description of what is broken now.

    A recorded file that has since left the volume goes into `gone`
    rather than either bucket: it didn't recover, and it can't still be
    stuck, so tracking it forever would be the wrong answer twice.

    A file whose SHARE is unmounted goes into `skipped` and keeps its
    record untouched. Every file on an unmounted share reports ENOENT at
    once, and mistaking that for departure would drop the entire record —
    including durations that exist nowhere else — the moment a recheck
    happened to run mid-remount. Each share is health-checked ONCE,
    through a watchdog, before any of its files is probed (#237): a
    wedged share hangs the very open the probe would make, so asking
    the file directly is what must not happen.
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
            # record_result carries the failing entry's context forward,
            # so this keeps which task broke the file and adds who found
            # it healed

            recorded = record_result(result, context="recheck")
            healed.append(_healed_result(path, recorded or entry))
            forget(path)

        else:
            record_result(result, context=entry.get("context"))
            result["first_seen"] = entry.get("first_seen")
            still_failing.append(result)

    return RecheckReport(healed, still_failing, gone, skipped)
