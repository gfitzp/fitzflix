"""Run the tasks that maintain the application itself, not the video library."""

import glob
import gzip
import json
import os
import shutil
import subprocess
import threading
import time

from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

import requests

from flask import current_app
from rq import Queue, Worker, get_current_job
from rq.registry import StartedJobRegistry
from sqlalchemy import text
from sqlalchemy.engine import make_url
from werkzeug.local import LocalProxy

from app import db, get_app
from app.email import task_send_email

# This is the app instance of this process. Fitzflix resolves it lazily.
# Thus, an import of this module from a process that already has an
# application does not build a second one.

app = LocalProxy(get_app)

# This is the worker roster from fitzflix_supervisor.ini. It lists the
# queues that each program listens to, and how many processes it runs
# (numprocs, default 1). Update it when the roster changes there. The
# expected per-queue counts and the self-healing restart targets both
# come from it.

SUPERVISOR_GROUP = "fitzflix"

PROGRAM_QUEUES = {
    "fitzflix-maintenance": ["fitzflix-maintenance"],
    "fitzflix-sql": ["fitzflix-sql"],
    "fitzflix-user-request": ["fitzflix-user-request"],
    "fitzflix-import": ["fitzflix-import", "fitzflix-file-operation"],
    "fitzflix-transcode": [
        "fitzflix-transcode",
        "fitzflix-import",
        "fitzflix-file-operation",
    ],
    "fitzflix-file-operation": ["fitzflix-file-operation", "fitzflix-import"],
}

PROGRAM_COUNTS = {
    "fitzflix-import": 2,
    "fitzflix-file-operation": 2,
    "fitzflix-user-request": 2,
}

EXPECTED_WORKERS = {}
for _program, _queues in PROGRAM_QUEUES.items():
    for _queue in _queues:
        EXPECTED_WORKERS[_queue] = EXPECTED_WORKERS.get(_queue, 0) + PROGRAM_COUNTS.get(
            _program, 1
        )

# This is the default worker_ttl of rq. An idle worker whose heartbeat is
# older than this is a leftover registration, not a live worker.

WORKER_HEARTBEAT_STALE_SECONDS = 420

PROBES_KEY = "fitzflix:health:probes"
FAILCOUNT_KEY = "fitzflix:health:failcount"
ISSUES_KEY = "fitzflix:health:issues"
ALERTED_KEY_PREFIX = "fitzflix:health:alerted:"
OBSERVER_KEY_PREFIX = "fitzflix:observer:"
SCHEDULER_KEY_PREFIX = "rq:cron_scheduler:"

# While a problem continues, send the alert again daily, not on every
# probe.

ALERT_REMINDER_SECONDS = 86400

BACKUP_STALE_HOURS = 25


def _human_size(size):
    """Format a byte count for display."""

    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(size) < 1024 or unit == "PB":
            break
        size /= 1024.0
    return f"{size:,.1f} {unit}"


def _live_workers(connection):
    """Find the workers from their heartbeat keys, not from the registry set of rq.

    A worker that misses 1 heartbeat deadline, for example under heavy
    load, can be removed from the rq:workers set by the registry cleanup
    of rq. The worker never adds itself again. But it continues to work
    and to refresh its own key. Thus, the per-worker keys with a TTL are
    the true source for liveness.
    """

    workers = []
    for key in connection.scan_iter("rq:worker:*"):
        # The key of a worker that shut down cleanly stays for a short
        # time with a death timestamp. It is not a live worker.

        if connection.hget(key, "death"):
            continue
        try:
            worker = Worker.find_by_key(key.decode(), connection=connection)
        except Exception:
            continue
        if worker is not None:
            workers.append(worker)
    return workers


def worker_health(connection):
    """Summarize the rq worker liveness per queue against the expected roster."""

    now = datetime.now(timezone.utc)
    queues = {name: {"queue": name, "live": 0, "busy": []} for name in EXPECTED_WORKERS}
    for worker in _live_workers(connection):
        # A busy worker does not refresh its heartbeat while the job
        # runs. Thus, only an idle worker can count as stale. rq 2 returns
        # aware datetimes. Normalize them for older stored values.

        heartbeat = worker.last_heartbeat
        if heartbeat is not None and heartbeat.tzinfo is None:
            heartbeat = heartbeat.replace(tzinfo=timezone.utc)
        if (
            worker.get_state() != "busy"
            and heartbeat
            and (now - heartbeat).total_seconds() > WORKER_HEARTBEAT_STALE_SECONDS
        ):
            continue

        job = None
        if worker.get_state() == "busy":
            try:
                job = worker.get_current_job()
            except Exception:
                pass

        for name in worker.queue_names():
            entry = queues.setdefault(name, {"queue": name, "live": 0, "busy": []})
            entry["live"] += 1

            # Report the running job only under the queue that it came
            # from, not under every queue that its worker listens to.

            if job is not None and job.origin == name:
                entry["busy"].append(job.description or job.id)

    for entry in queues.values():
        entry["expected"] = EXPECTED_WORKERS.get(entry["queue"])
        entry["ok"] = entry["expected"] is None or entry["live"] >= entry["expected"]
        entry["queued"] = Queue(entry["queue"], connection=connection).count

    return [queues[name] for name in sorted(queues)]


# This is where the mounted volumes appear. It is a module constant.
# Thus, the tests can build a fake mount tree in a writable location.

VOLUMES_ROOT = "/Volumes"


def mountpoint_ok(path):
    """Return True if a path meets the mountpoint requirement (#227).

    A path under VOLUMES_ROOT must BE a mountpoint. When a share drops,
    macOS leaves the mountpoint behind as a normal directory on the boot
    disk. Thus, every existence check (statvfs, isdir) succeeds
    immediately, because it answered for the boot volume. It calls a
    dead share alive. ismount compares the device of the path with the
    device of its parent. That is the real question, for the cost of 1
    stat.

    A path outside VOLUMES_ROOT was never expected to be a mountpoint.
    It passes unconditionally. The staging directory and the logs are
    on the boot disk.
    """

    return not path.startswith(VOLUMES_ROOT + os.sep) or os.path.ismount(path)


def volume_alive(path, timeout=10):
    """Return True if the filesystem behind the path responds in the timeout.

    A dead SMB mount can hang stat calls instead of failing them. Thus,
    the probe runs in a daemon thread. A hang counts as dead.

    A path under VOLUMES_ROOT must also BE a mountpoint (#227). statvfs
    answers for the filesystem that is behind the path at that moment.
    When a share drops, macOS leaves the mountpoint behind as a normal
    directory on the boot disk. Then statvfs succeeds immediately,
    because it measured the boot volume. isdir agrees that the directory
    is there. Seen live on 2026-08-24: Movies and TV Shows were out of
    the mount table while both read as alive. The Plex refresh would
    have scanned an empty tree and emptied the trash behind it. ismount
    compares the device of the path with the device of its parent. Thus,
    it costs 1 stat and answers the real question. It adds to the
    timeout. It does not replace it. The watchdog catches a share that
    is still mounted but wedged. This check does not.
    """

    result = {}

    def probe():
        try:
            os.statvfs(path)
            result["ok"] = mountpoint_ok(path)
        except OSError:
            result["ok"] = False

    thread = threading.Thread(target=probe, daemon=True)
    thread.start()
    thread.join(timeout)
    return result.get("ok", False)


def _monitored_paths(config):
    """Return the configured directories whose volumes the health checks
    watch.
    """

    return (
        config["MEDIA_LOCATION"],
        config["IMPORT_DIR"],
        config["MOVIE_LIBRARY"],
        config["TV_LIBRARY"],
        config["TRANSCODES_DIR"],
        config["REJECTS_DIR"],
        config["STAGING_DIR"],
        config["DB_BACKUP_DIR"],
        os.path.dirname(config["LOG_FILE"]),
    )


def missing_volumes(config):
    """Return the mountpoints under /Volumes that must be present but do not respond."""

    missing = []
    checked = set()
    for path in _monitored_paths(config):
        if not path.startswith(VOLUMES_ROOT + os.sep):
            continue
        mount = "/".join(path.split("/")[:3])
        if mount in checked:
            continue
        checked.add(mount)
        if not volume_alive(mount):
            missing.append(mount)
    return sorted(missing)


def disk_health(config):
    """Report the usage of each distinct volume behind the directories of the app."""

    alert_free_bytes = config["DISK_ALERT_FREE_GB"] * 1024**3
    dead_mounts = missing_volumes(config)
    volumes = {}
    for path in _monitored_paths(config):
        # A dead network mount can hang stat calls. Report it through
        # missing_volumes. Do not risk a hang on a gauge.

        if any(path.startswith(mount) for mount in dead_mounts):
            continue
        if not os.path.isdir(path):
            continue
        device = os.stat(path).st_dev
        if device in volumes:
            continue

        # Walk up to the mountpoint. Then the gauge label is the volume,
        # not the configured directory that found it first.

        mount = path
        while (
            mount != os.path.dirname(mount)
            and os.stat(os.path.dirname(mount)).st_dev == device
        ):
            mount = os.path.dirname(mount)

        usage = shutil.disk_usage(mount)
        percent = round(usage.used / usage.total * 100, 1)
        volumes[device] = {
            "mount": mount,
            "percent": percent,
            "free": _human_size(usage.free),
            "total": _human_size(usage.total),
            "color": (
                "danger" if percent >= 95 else "warning" if percent >= 90 else "success"
            ),
            # The library volumes are almost full by design. Thus, alert
            # on the absolute free space that imports and transcodes need,
            # not on the percentage.
            "ok": usage.free >= alert_free_bytes,
        }

    return sorted(volumes.values(), key=lambda volume: volume["mount"])


def backup_health(config):
    """Report when Fitzflix made the newest database backup."""

    backups = glob.glob(os.path.join(config["DB_BACKUP_DIR"], "*.sql.gz"))
    newest = max((os.path.getmtime(path) for path in backups), default=None)
    if newest is None:
        return {"ok": False, "last": None}
    last = datetime.fromtimestamp(newest, tz=timezone.utc)
    age_hours = (datetime.now(timezone.utc) - last).total_seconds() / 3600
    return {"ok": age_hours <= BACKUP_STALE_HOURS, "last": last}


def observer_health(connection):
    """Count the processes with a live import-directory observer heartbeat.

    Only the import-program workers watch. supervisor.py limits the
    observer to them. Thus, the expected count is the numprocs of that
    program.
    """

    watchers = sum(1 for _ in connection.scan_iter(f"{OBSERVER_KEY_PREFIX}*"))
    expected = PROGRAM_COUNTS.get("fitzflix-import", 1)
    return {"ok": watchers >= expected, "watchers": watchers, "expected": expected}


def scheduler_health(connection):
    """Report if the recurring-jobs process is registered and alive.

    The CronScheduler of rq refreshes its hash with a short TTL. Thus, a
    live key means a live scheduler.py process.
    """

    alive = any(True for _ in connection.scan_iter(f"{SCHEDULER_KEY_PREFIX}*"))
    return {"ok": alive}


def repair_worker_registry(connection):
    """List again the live workers that the registry sweep of rq dropped.

    This is the intact-but-unlisted state. The hash of the worker still
    has its queues field and a live heartbeat. But a short key expiry
    removed it from the rq:workers set. A worker joins that set only at
    birth. To add it back is a pure repair with no restart.
    """

    actions = []
    for key in connection.scan_iter("rq:worker:*"):
        if (
            connection.hget(key, "queues")
            and not connection.hget(key, "death")
            and not connection.sismember(Worker.redis_workers_keys, key)
        ):
            connection.sadd(Worker.redis_workers_keys, key)
            actions.append(f"re-listed {key.decode()} in the worker registry")
    return actions


def _supervisor_status(config):
    """Return {process_name: (state, pid)} for the supervisor group, or None."""

    try:
        result = subprocess.run(
            [config["SUPERVISORCTL_BIN"], "status", f"{SUPERVISOR_GROUP}:*"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    processes = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].startswith(f"{SUPERVISOR_GROUP}:"):
            pid = None
            if parts[1] == "RUNNING" and len(parts) >= 4 and parts[2] == "pid":
                pid = int(parts[3].rstrip(","))
            processes[parts[0]] = (parts[1], pid)
    return processes or None


def _run_supervisorctl(config, command, process_name):
    """Run a supervisorctl command and return (succeeded, output)."""

    try:
        result = subprocess.run(
            [config["SUPERVISORCTL_BIN"], command, process_name],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    output = result.stdout.strip()
    return result.returncode == 0 and "ERROR" not in output, output


HEAL_COOLDOWN_SECONDS = 3600
HEALED_KEY_PREFIX = "fitzflix:health:healed:"


def heal_worker_processes(connection, config):
    """Start the dead worker processes, and restart the ones that lost their identity.

    There are 2 failure states. The process view of supervisor tells
    them apart:
    - Not RUNNING: the process died and supervisor gave up, or someone
      stopped it. Start it again.
    - RUNNING, but its pid is not attached to a registered rq worker:
      the process is alive, but its Redis registration lost its
      identity. A full key expiry deleted the queues field. No
      supervisor-level check can see this. Only a restart, with a new
      birth registration, can repair it. This function restarts the
      process only while its queues are idle. Thus, it kills no job
      that is in progress.

    This function heals each process at most 1 time per cooldown window.
    Thus, a worker that is really sick cannot cause a restart loop.
    """

    actions = []
    status = _supervisor_status(config)
    if not status:
        current_app.logger.warning(
            "Health: supervisorctl is unavailable, cannot heal worker processes"
        )
        return actions

    registered_pids = set()
    for key in connection.scan_iter("rq:worker:*"):
        pid = connection.hget(key, "pid")
        if pid:
            registered_pids.add(int(pid))

    own_job = get_current_job()
    for process_name, (state, pid) in sorted(status.items()):
        program = process_name.split(":", 1)[1].rsplit("_", 1)[0]
        cooldown_key = f"{HEALED_KEY_PREFIX}{process_name}"

        if state in ("STOPPED", "EXITED", "FATAL"):
            if connection.set(cooldown_key, "1", ex=HEAL_COOLDOWN_SECONDS, nx=True):
                ok, output = _run_supervisorctl(config, "start", process_name)
                actions.append(
                    f"started {process_name} (was {state})"
                    if ok
                    else f"failed to start {process_name} (was {state}): {output}"
                )

        elif (
            state == "RUNNING"
            and pid is not None
            and pid not in registered_pids
            and program in PROGRAM_QUEUES
        ):
            busy = False
            for queue_name in PROGRAM_QUEUES[program]:
                started = StartedJobRegistry(
                    queue_name, connection=connection
                ).get_job_ids()
                if own_job:
                    started = [job_id for job_id in started if job_id != own_job.id]
                if started or connection.llen(f"rq:queue:{queue_name}"):
                    busy = True
            if busy:
                current_app.logger.info(
                    f"Health: {process_name} has no registered rq worker and "
                    f"needs a restart; deferring while its queues are busy"
                )
                continue
            if connection.set(cooldown_key, "1", ex=HEAL_COOLDOWN_SECONDS, nx=True):
                ok, output = _run_supervisorctl(config, "restart", process_name)
                actions.append(
                    f"restarted {process_name} (running, but not registered "
                    f"as an rq worker)"
                    if ok
                    else f"failed to restart {process_name}: {output}"
                )

    return actions


def share_mounted_elsewhere(share, mount):
    """Return the other paths where `share` is mounted now.

    When the SMB session dies, it takes every share with it. macOS
    leaves each mount point behind as a normal directory. A share that
    comes back while its stub is still there goes to a path with a
    suffix, for example /Volumes/TV Shows-1. Finder, an app that touches
    the path, or a person can bring it back. The canonical path stays a
    dead directory on the boot disk.

    A remount cannot free that path while the share is still mounted in
    a different location. Thus, the duplicate must go first (#233). Seen
    live on 2026-08-25: TV Shows and Transcoded both ran at -1 paths for
    approximately 25 minutes. The recovery needed the -1 mount unmounted
    before the remount would go where it was asked to.

    This function examines only the network mounts. A local disk that
    appears in the output of `mount` is not ours to unmount. An SMB
    device reads `//user@host/Share`, with the share name URL-encoded.
    An NFS device reads `host:/export/Share`, with the share name
    literal (#239). The session collapse that caused this function
    occurred during an NFS test. The SMB-only match could not see the
    NFS duplicates.
    """

    try:
        result = subprocess.run(["mount"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return []

    elsewhere = []
    for line in result.stdout.splitlines():
        # Example line:
        # `//server@host/TV%20Shows on /Volumes/TV Shows-1 (smbfs, ...)`.
        # Use rsplit on the options. Then a path that contains " (" stays
        # intact.
        device, separator, path = line.rsplit(" (", 1)[0].partition(" on ")
        if not separator:
            continue
        if device.startswith("//"):
            name = unquote(device.rsplit("/", 1)[-1])
        elif ":" in device and device.partition(":")[2].startswith("/"):
            name = device.partition(":")[2].rstrip("/").rsplit("/", 1)[-1]
        else:
            continue
        if name == share and path != mount:
            elsewhere.append(path)

    return elsewhere


def heal_mounts(dead_mounts, connection, config):
    """Try to remount the dead network volumes and return the actions taken.

    This function force-unmounts a half-dead mountpoint first. Then it
    remounts the volume through the user session. The `mount volume`
    command of osascript authenticates from the keychain. NFS URLs need
    no credentials. The remount URL of each share comes from
    MOUNT_URLS, keyed by the mount-point name. These are per-share URLs,
    not a server prefix, because the NFS exports are on different volume
    roots. When the map is empty, this function only alerts. It reports
    a dead share that is absent from a configured map. A silent skip
    would hide why the share never heals.
    """

    actions = []
    mount_urls = config.get("MOUNT_URLS") or {}
    if not mount_urls:
        return actions

    for mount in dead_mounts:
        cooldown_key = f"{HEALED_KEY_PREFIX}mount:{mount}"
        if not connection.set(cooldown_key, "1", ex=HEAL_COOLDOWN_SECONDS, nx=True):
            continue

        share = os.path.basename(mount)
        url = mount_urls.get(share)
        if url is None:
            actions.append(
                f"no MOUNT_URLS entry for {share}, so not attempting to "
                f"remount {mount}"
            )
            continue
        if os.path.ismount(mount):
            # The volume is still mounted but dead. Unmount it before the
            # remount. The condition is ismount, not isdir (#227). The
            # leftover-directory case now reaches here. There is nothing
            # to unmount then. A force-unmount of a path that is not a
            # mountpoint is a hazard. Do not make it possible.
            try:
                subprocess.run(
                    ["diskutil", "unmount", "force", mount],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass

        for duplicate in share_mounted_elsewhere(share, mount):
            # Try a clean unmount first. The duplicate is usually a
            # healthy mount with readers on it. Plex streams from these
            # shares. Thus, force is the last resort, not the first step.
            # But to keep the duplicate is worse than to interrupt it.
            # Every config path stays pointed at an empty directory on the
            # boot disk. A TRANSCODES_DIR that points there fills the boot
            # disk.
            freed = False
            for command in (
                ["diskutil", "unmount", duplicate],
                ["diskutil", "unmount", "force", duplicate],
            ):
                try:
                    outcome = subprocess.run(
                        command, capture_output=True, text=True, timeout=60
                    )
                except (OSError, subprocess.TimeoutExpired):
                    continue
                if outcome.returncode == 0:
                    freed = True
                    actions.append(f"unmounted {duplicate} to free {mount}")
                    break

            if not freed:
                actions.append(
                    f"failed to unmount {duplicate}, which is holding the "
                    f"share {mount} needs"
                )

        try:
            result = subprocess.run(
                ["osascript", "-e", f'mount volume "{url}"'],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            actions.append(f"failed to remount {mount}: {e}")
            continue

        if result.returncode == 0 and volume_alive(mount):
            actions.append(f"remounted {mount}")
        else:
            reason = result.stderr.strip() or "still dead"

            # Name the location where the share went. Then "still dead"
            # becomes something that a person can act on without a search.

            stranded = share_mounted_elsewhere(share, mount)
            if stranded:
                reason = f"{reason} — share is mounted at {', '.join(stranded)}"

            actions.append(f"failed to remount {mount}: {reason}")

    return actions


def probe_health(connection):
    """Return the latest external-service probe results that health_probe wrote."""

    results = []
    for service, raw in sorted(connection.hgetall(PROBES_KEY).items()):
        result = json.loads(raw)
        result["service"] = service.decode()
        result["checked"] = datetime.fromisoformat(result["checked"])
        results.append(result)
    return results


def system_health(flask_app):
    """Collect the metrics that the system health card of the admin page shows.

    Everything here reads Redis or the local filesystem. The
    external-service results come from the Redis hash that the
    health_probe task maintains. Thus, the render of the card never makes
    a network call.
    """

    started = time.perf_counter()
    flask_app.redis.ping()
    redis_ms = round((time.perf_counter() - started) * 1000, 1)

    started = time.perf_counter()
    db.session.execute(text("SELECT 1"))
    db_ms = round((time.perf_counter() - started) * 1000, 1)

    return {
        "redis_ms": redis_ms,
        "db_ms": db_ms,
        "workers": worker_health(flask_app.redis),
        "disks": disk_health(flask_app.config),
        "missing_mounts": missing_volumes(flask_app.config),
        "backup": backup_health(flask_app.config),
        "observer": observer_health(flask_app.redis),
        "scheduler": scheduler_health(flask_app.redis),
        "probes": probe_health(flask_app.redis),
    }


def rotate_logs():
    """Archive the current log file and delete the archives older than the retention window.

    This task renames the log file with a date suffix and compresses it
    with gzip. Every process writes through a WatchedFileHandler. Thus,
    each process opens the new log file on its next write. This task
    deletes the archives older than LOG_RETENTION_DAYS.
    """

    with app.app_context():
        log_file = current_app.config["LOG_FILE"]
        retention_days = current_app.config["LOG_RETENTION_DAYS"]

        archived = None
        if os.path.isfile(log_file) and os.path.getsize(log_file) > 0:
            stamp = datetime.now().strftime("%Y-%m-%d")
            archive = f"{log_file}.{stamp}"
            if os.path.exists(archive) or os.path.exists(f"{archive}.gz"):
                # The log was already rotated today. Add a timestamp. Do
                # not overwrite.
                stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
                archive = f"{log_file}.{stamp}"

            os.rename(log_file, archive)
            with open(archive, "rb") as source, gzip.open(
                f"{archive}.gz", "wb"
            ) as target:
                shutil.copyfileobj(source, target)
            os.remove(archive)
            archived = f"{archive}.gz"

        cutoff = datetime.now() - timedelta(days=retention_days)
        deleted = []
        for path in sorted(glob.glob(f"{log_file}.*.gz")):
            if datetime.fromtimestamp(os.path.getmtime(path)) < cutoff:
                os.remove(path)
                deleted.append(os.path.basename(path))

        current_app.logger.info(
            f"Rotated logs: "
            f"archived {os.path.basename(archived) if archived else 'nothing'}, "
            f"deleted {len(deleted)} archive(s) older than {retention_days} days"
            f"{' ' + str(deleted) if deleted else ''}"
        )


def backup_database():
    """Dump the database to a compressed backup and delete the old backups.

    The media files are archived at AWS. But the database exists only
    here. It holds the reviews, the Criterion details, and the shopping
    priorities. Thus, it gets a nightly dump with its own retention
    window. This task also copies each dump to the S3 bucket. Thus, a
    machine failure cannot destroy the database and its backups
    together. The task deletes the remote copies on the same retention
    window.
    """

    with app.app_context():
        backup_dir = current_app.config["DB_BACKUP_DIR"]
        retention_days = current_app.config["DB_BACKUP_RETENTION_DAYS"]
        url = make_url(current_app.config["SQLALCHEMY_DATABASE_URI"])

        os.makedirs(backup_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d")
        backup_file = os.path.join(backup_dir, f"{url.database}-{stamp}.sql.gz")
        if os.path.exists(backup_file):
            # The database was already backed up today. Add a timestamp.
            # Do not overwrite.
            stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
            backup_file = os.path.join(backup_dir, f"{url.database}-{stamp}.sql.gz")

        command = [
            current_app.config["MYSQLDUMP_BIN"],
            "--single-transaction",
            f"--user={url.username}",
        ]
        if url.host:
            command.append(f"--host={url.host}")
        if url.port:
            command.append(f"--port={url.port}")
        command.append(url.database)

        # Pass the password through the environment. Then it does not
        # appear in the process list.

        env = dict(os.environ)
        if url.password:
            env["MYSQL_PWD"] = url.password

        try:
            with gzip.open(backup_file, "wb") as target:
                dump = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                )
                shutil.copyfileobj(dump.stdout, target)
                dump.stdout.close()
                stderr = dump.stderr.read().decode("utf-8", "replace")
                if dump.wait() != 0:
                    raise RuntimeError(
                        f"mysqldump exited {dump.returncode}: {stderr[:300]}"
                    )

        except Exception:
            # Do not leave a partial backup that looks like a good one.
            if os.path.exists(backup_file):
                os.remove(backup_file)
            raise

        size_mb = round(os.path.getsize(backup_file) / 1024 / 1024, 1)

        # Copy the backup to the S3 bucket. The backup prefix is outside
        # AWS_UNTOUCHED_PREFIX. Thus, the S3 sync task never touches it
        # when it deletes the unreferenced media keys.

        uploaded_key = None
        remote_deleted = []
        if current_app.config["AWS_BUCKET"]:
            # Import here, because app.videos imports this module.

            from app.videos import aws_s3_client, get_matching_s3_objects

            backup_prefix = current_app.config["AWS_BACKUP_PREFIX"]
            uploaded_key = f"{backup_prefix}/{os.path.basename(backup_file)}"
            client = aws_s3_client(with_retries=True)
            client.upload_file(
                backup_file, current_app.config["AWS_BUCKET"], uploaded_key
            )
            current_app.logger.info(
                f"Uploaded '{backup_file}' to "
                f"'s3://{os.path.join(current_app.config['AWS_BUCKET'], uploaded_key)}'"
            )

            # Back up the environment file too. It is the one configuration
            # that exists nowhere else. Encrypt it with BACKUP_PASSPHRASE.
            # Keep the passphrase in a password manager. It is the key
            # that recovers everything else.

            env_file = current_app.config["ENV_FILE"]
            if not current_app.config["BACKUP_PASSPHRASE"]:
                current_app.logger.info(
                    "BACKUP_PASSPHRASE is not set, "
                    "so the environment file is not being backed up"
                )
            elif os.path.isfile(env_file):
                encrypted_file = os.path.join(backup_dir, f".dotenv-{stamp}.enc")
                subprocess.run(
                    [
                        "openssl",
                        "enc",
                        "-aes-256-cbc",
                        "-pbkdf2",
                        "-iter",
                        "200000",
                        "-salt",
                        "-in",
                        env_file,
                        "-out",
                        encrypted_file,
                        "-pass",
                        "env:BACKUP_PASSPHRASE",
                    ],
                    check=True,
                    capture_output=True,
                    env={
                        **os.environ,
                        "BACKUP_PASSPHRASE": current_app.config["BACKUP_PASSPHRASE"],
                    },
                )
                env_key = f"{backup_prefix}/dotenv-{stamp}.enc"
                client.upload_file(
                    encrypted_file, current_app.config["AWS_BUCKET"], env_key
                )
                os.remove(encrypted_file)
                current_app.logger.info(
                    f"Uploaded the encrypted environment file as '{env_key}'"
                )

            # Mirror the custom posters to S3. They are user-created
            # artwork that exists only on this machine. The task uploads
            # the new and changed files. It removes the remote copies of
            # the deleted files. The prefix is outside AWS_BACKUP_PREFIX.
            # Thus, the retention cleanup never touches it.

            posters_dir = current_app.config["CUSTOM_ARTWORK_DIR"]
            posters_prefix = current_app.config["AWS_CUSTOM_POSTERS_PREFIX"]
            posters_uploaded = 0
            posters_deleted = 0
            if os.path.isdir(posters_dir):
                remote_posters = {
                    object["Key"]: object["Size"]
                    for object in get_matching_s3_objects(
                        current_app.config["AWS_BUCKET"],
                        prefix=f"{posters_prefix}/",
                    )
                }
                local_keys = set()
                for dirpath, dirnames, filenames in os.walk(posters_dir):
                    dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                    for name in filenames:
                        if name.startswith("."):
                            continue
                        full_path = os.path.join(dirpath, name)
                        key = (
                            f"{posters_prefix}/"
                            f"{os.path.relpath(full_path, posters_dir)}"
                        )
                        local_keys.add(key)
                        if remote_posters.get(key) != os.path.getsize(full_path):
                            client.upload_file(
                                full_path, current_app.config["AWS_BUCKET"], key
                            )
                            posters_uploaded += 1
                for key in remote_posters:
                    if key not in local_keys:
                        client.delete_object(
                            Bucket=current_app.config["AWS_BUCKET"], Key=key
                        )
                        posters_deleted += 1
                if posters_uploaded or posters_deleted:
                    current_app.logger.info(
                        f"Custom posters synced to S3: "
                        f"{posters_uploaded} uploaded, {posters_deleted} removed"
                    )

            # Delete the remote backups older than the retention window.

            remote_cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
            for object in get_matching_s3_objects(
                current_app.config["AWS_BUCKET"], prefix=f"{backup_prefix}/"
            ):
                if (
                    object["Key"] != uploaded_key
                    and object["Key"] != f"{backup_prefix}/"
                    and object["LastModified"] < remote_cutoff
                ):
                    client.delete_object(
                        Bucket=current_app.config["AWS_BUCKET"], Key=object["Key"]
                    )
                    remote_deleted.append(object["Key"])

        cutoff = datetime.now() - timedelta(days=retention_days)
        deleted = []
        for path in sorted(glob.glob(os.path.join(backup_dir, "*.sql.gz"))):
            if path != backup_file and (
                datetime.fromtimestamp(os.path.getmtime(path)) < cutoff
            ):
                os.remove(path)
                deleted.append(os.path.basename(path))

        current_app.logger.info(
            f"Backed up the database to {os.path.basename(backup_file)} "
            f"({size_mb} MB)"
            f"{f', uploaded to AWS as {uploaded_key!r}' if uploaded_key else ''}, "
            f"deleted {len(deleted)} local backup(s) and {len(remote_deleted)} "
            f"remote backup(s) older than {retention_days} days"
            f"{' ' + str(deleted + remote_deleted) if deleted or remote_deleted else ''}"
        )
        return True


RESTORE_CHECK_DATABASE = "fitzflix_restore_check"

# The restored row counts of these tables prove that the dump holds the
# real library. A nightly dump can be slightly behind the live growth.
# Thus, the tolerance below.

RESTORE_CHECK_TABLES = ("movie", "file", "tv_series", "user", "user_movie_review")
RESTORE_CHECK_TOLERANCE = 0.9


def _newest_backup_key(objects, database):
    """Return the most recent database dump in a backup-prefix S3 listing."""

    dumps = [
        object
        for object in objects
        if os.path.basename(object["Key"]).startswith(f"{database}-")
        and object["Key"].endswith(".sql.gz")
    ]
    if not dumps:
        return None
    return max(dumps, key=lambda object: object["LastModified"])["Key"]


def restore_drill():
    """Prove that the offsite database backup restores.

    This task downloads the newest dump from S3. It deliberately does
    not use the local copy. The drill verifies what a disaster recovery
    would really use. It loads the dump into a scratch database. It
    compares the restored contents with the live database. The scratch
    database needs a one-time grant:

        GRANT ALL PRIVILEGES ON `fitzflix_restore_check`.* TO '<user>'@'localhost';

    A failure raises. Then the job goes to the failed-tasks page, and
    Fitzflix emails the error.
    """

    with app.app_context():
        if not current_app.config["AWS_BUCKET"]:
            current_app.logger.info("AWS is not configured, skipping the restore drill")
            return True

        # Import here, because app.videos imports this module.

        from app.videos import aws_s3_client, get_matching_s3_objects

        url = make_url(current_app.config["SQLALCHEMY_DATABASE_URI"])
        backup_prefix = current_app.config["AWS_BACKUP_PREFIX"]

        key = _newest_backup_key(
            get_matching_s3_objects(
                current_app.config["AWS_BUCKET"], prefix=f"{backup_prefix}/"
            ),
            url.database,
        )
        if key is None:
            raise RuntimeError(
                f"Restore drill failed: no database dumps found under "
                f"'{backup_prefix}/' in S3"
            )

        download_path = os.path.join(
            current_app.config["DB_BACKUP_DIR"], ".restore-drill.sql.gz"
        )
        os.makedirs(current_app.config["DB_BACKUP_DIR"], exist_ok=True)
        client = aws_s3_client(with_retries=True)
        client.download_file(current_app.config["AWS_BUCKET"], key, download_path)

        mysql_env = dict(os.environ)
        if url.password:
            mysql_env["MYSQL_PWD"] = url.password
        mysql_command = [
            current_app.config["MYSQL_BIN"],
            f"--user={url.username}",
        ]
        if url.host:
            mysql_command.append(f"--host={url.host}")
        if url.port:
            mysql_command.append(f"--port={url.port}")

        def run_mysql(arguments, **kwargs):
            return subprocess.run(
                mysql_command + arguments,
                env=mysql_env,
                check=True,
                capture_output=True,
                **kwargs,
            )

        try:
            run_mysql(
                [
                    "-e",
                    f"DROP DATABASE IF EXISTS {RESTORE_CHECK_DATABASE}; "
                    f"CREATE DATABASE {RESTORE_CHECK_DATABASE}",
                ]
            )
            with gzip.open(download_path, "rb") as f:
                run_mysql([RESTORE_CHECK_DATABASE], input=f.read())

            # The restored schema must be at a migration state.

            version = db.session.execute(
                text(
                    f"SELECT version_num "
                    f"FROM {RESTORE_CHECK_DATABASE}.alembic_version"
                )
            ).scalar()
            if not version:
                raise RuntimeError(
                    "Restore drill failed: the restored database has no "
                    "alembic_version"
                )

            # The restored row counts must be close to the live counts.

            summary = []
            for table in RESTORE_CHECK_TABLES:
                live = db.session.execute(
                    text(f"SELECT COUNT(*) FROM `{table}`")
                ).scalar()
                restored = db.session.execute(
                    text(f"SELECT COUNT(*) " f"FROM {RESTORE_CHECK_DATABASE}.`{table}`")
                ).scalar()
                if restored < int(live * RESTORE_CHECK_TOLERANCE) or (
                    live > 0 and restored == 0
                ):
                    raise RuntimeError(
                        f"Restore drill failed: '{table}' restored "
                        f"{restored} row(s) but the live table has {live}"
                    )
                summary.append(f"{table} {restored}/{live}")

            current_app.logger.info(
                f"Restore drill passed: '{key}' restored at migration "
                f"{version} ({', '.join(summary)})"
            )

        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or b"").decode("utf-8", "replace")[:300]
            current_app.logger.error(f"Restore drill failed running mysql: {stderr}")
            raise

        finally:
            try:
                run_mysql(["-e", f"DROP DATABASE IF EXISTS {RESTORE_CHECK_DATABASE}"])
            except Exception:
                pass
            try:
                os.remove(download_path)
            except OSError:
                pass

        return True


ORPHAN_MAX_AGE_DAYS = 7

# macOS puts its own dot-prefixed metadata (Finder, AppleDouble) in these
# directories. None of it is a pipeline leftover. Thus, it stays as it is.

ORPHAN_IGNORED_NAMES = {".DS_Store", ".localized"}
ORPHAN_IGNORED_PREFIXES = ("._",)

# These are the contents that a directory can hold and still count as
# removable leftovers (specified by Glenn): the @eaDir metadata trees of
# Synology, macOS metadata, and stray image files. An example is a
# custom poster that stayed after its film moved away. The image cap
# prevents the sweep from deleting something that looks like a
# deliberate picture collection.

SYNOLOGY_METADATA_DIRNAME = "@eaDir"
ORPHAN_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tbn"}
ORPHAN_JUNK_FILE_CAP = 25


def _leftover_junk_scan(path, cutoff):
    """Return (junk_only, file_count) for the contents under `path`.

    junk_only is True if everything under `path` is leftover junk: @eaDir
    trees, macOS metadata, and at most ORPHAN_JUNK_FILE_CAP old image
    files. file_count is the number of non-Synology files that a
    clearance would remove.

    Anything else keeps the folder alive. So does a recent image, for
    example a poster that someone placed a moment ago. So does a recent
    subdirectory. So does an image count above the cap, because that is
    a picture collection, not a leftover. An @eaDir subtree is junk as a
    whole. This function does not check the age of its contents and does
    not count them. Synology rewrites them on its own schedule. That must
    not keep a dead folder alive forever.
    """

    junk_files = 0
    images = 0
    for dirpath, dirnames, filenames in os.walk(path):
        kept = []
        for name in dirnames:
            if name == SYNOLOGY_METADATA_DIRNAME:
                continue
            try:
                if os.stat(os.path.join(dirpath, name)).st_mtime > cutoff:
                    return False, 0
            except OSError:
                return False, 0
            kept.append(name)
        dirnames[:] = kept
        for name in filenames:
            if name in ORPHAN_IGNORED_NAMES or name.startswith(ORPHAN_IGNORED_PREFIXES):
                junk_files += 1
                continue
            extension = os.path.splitext(name)[1].lower()
            if extension in ORPHAN_IMAGE_EXTENSIONS and not name.startswith("."):
                try:
                    if os.stat(os.path.join(dirpath, name)).st_mtime > cutoff:
                        return False, 0
                except OSError:
                    return False, 0
                images += 1
                junk_files += 1
                if images > ORPHAN_JUNK_FILE_CAP:
                    return False, 0
                continue
            return False, 0
    return True, junk_files


def clear_leftover_directory(path):
    """Remove a directory that a delete or a rename emptied of media.

    This function removes the directory only when all its remaining
    contents are leftover junk. Examples are the poster art placed next
    to a film for Plex, OS metadata, and @eaDir trees. Then it climbs
    toward the library root and clears the junk-only parents. This is
    the same way that os.removedirs climbs the empty parents. Unlike the
    directory pass of the weekly sweep, there is no age condition. The
    caller deleted or moved the media of the folder on purpose. Thus, a
    poster-only shell is already known to be a shell. Anything that is
    not junk keeps the folder alive, and everything above it. The
    configured roots themselves never fall. This function refuses a
    path that is outside the roots. It returns the removed directories,
    deepest first.
    """

    config = current_app.config
    roots = {
        os.path.realpath(config[name])
        for name in (
            "LIBRARY_DIR",
            "STAGING_DIR",
            "MOVIE_LIBRARY",
            "TV_LIBRARY",
            "IMPORT_DIR",
            "REJECTS_DIR",
            "TRANSCODES_DIR",
        )
        if config.get(name)
    }
    removed = []
    current = os.path.realpath(path)
    while (
        current not in roots
        and any(current.startswith(root + os.sep) for root in roots)
        and os.path.isdir(current)
    ):
        junk_only, junk_files = _leftover_junk_scan(current, float("inf"))
        if not junk_only:
            break
        try:
            if junk_files:
                shutil.rmtree(current)
            else:
                os.rmdir(current)
        except OSError as e:
            current_app.logger.warning(
                f"'{current}' Couldn't clear leftover directory: {e}"
            )
            break
        current_app.logger.info(f"'{current}' Cleared leftover directory")
        removed.append(current)
        current = os.path.dirname(current)
    return removed


def cleanup_orphaned_files():
    """Delete the hidden partial files that failed tasks left behind.

    This task also drops the leftover scratch database of a failed
    restore drill. Every pipeline stage that moves media writes it under
    a dot-prefixed name first. Examples are the localization staging
    file (and its .convert.mkv scratch file) and cross-volume library
    copies. Other examples are AWS downloads into the import directory,
    reject moves (.partial), and transcode outputs. The stage promotes
    the file to the visible name only on success. Thus, a hidden file
    that is 1 week old can only be the residue of a failed task. The
    deletions have an age condition. They are limited to those
    directories. The task sends a summary by email.
    """

    with app.app_context():
        config = current_app.config
        cutoff = time.time() - ORPHAN_MAX_AGE_DAYS * 86400
        removed = []

        roots = (
            config["STAGING_DIR"],
            config["MOVIE_LIBRARY"],
            config["TV_LIBRARY"],
            config["IMPORT_DIR"],
            config["REJECTS_DIR"],
            config["TRANSCODES_DIR"],
        )
        for root in roots:
            if not os.path.isdir(root):
                continue
            for dirpath, dirnames, filenames in os.walk(root):
                # Never go into hidden directories (Spotlight, trashes).

                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for name in filenames:
                    if not name.startswith("."):
                        continue
                    if name in ORPHAN_IGNORED_NAMES or name.startswith(
                        ORPHAN_IGNORED_PREFIXES
                    ):
                        continue
                    path = os.path.join(dirpath, name)
                    try:
                        stats = os.stat(path)
                    except OSError:
                        continue
                    if stats.st_mtime > cutoff:
                        continue
                    try:
                        os.remove(path)
                    except OSError as e:
                        current_app.logger.warning(
                            f"'{path}' Couldn't delete orphaned file: {e}"
                        )
                        continue
                    current_app.logger.info(f"'{path}' Deleted orphaned partial file")
                    removed.append(f"{path} ({_human_size(stats.st_size)})")

        # This is the empty-directory pass. It removes the import
        # leftovers of Radarr and Sonarr and the folders that a person
        # emptied. A directory falls when all its remaining contents are
        # LEFTOVER JUNK: Synology @eaDir trees, macOS metadata, and some
        # stray image files, such as a custom poster whose film moved
        # away. Nothing real in it can have changed in the same week that
        # the file pass uses. Anything else keeps the folder alive. The
        # roots themselves are never candidates. The pass never enters
        # hidden directories. It records the mtimes before any removal.
        # Thus, an old leftover tree collapses in a single pass.

        removed_dirs = []
        for root in roots:
            if not os.path.isdir(root):
                continue
            candidates = []
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [
                    d
                    for d in dirnames
                    if not d.startswith(".") and d != SYNOLOGY_METADATA_DIRNAME
                ]
                for name in dirnames:
                    path = os.path.join(dirpath, name)
                    try:
                        candidates.append((path, os.stat(path).st_mtime))
                    except OSError:
                        continue
            for path, mtime in sorted(
                candidates, key=lambda entry: -entry[0].count(os.sep)
            ):
                if mtime > cutoff:
                    continue
                junk_only, junk_files = _leftover_junk_scan(path, cutoff)
                if not junk_only:
                    continue
                try:
                    if junk_files:
                        shutil.rmtree(path)
                    else:
                        os.rmdir(path)
                except OSError as e:
                    current_app.logger.warning(
                        f"'{path}' Couldn't delete leftover directory: {e}"
                    )
                    continue
                note = f" (cleared {junk_files} leftover file(s))" if junk_files else ""
                current_app.logger.info(f"'{path}' Deleted leftover directory{note}")
                removed_dirs.append(f"{path}{note}")

        dropped_scratch_db = _drop_leftover_restore_database()

        if not removed and not removed_dirs and not dropped_scratch_db:
            current_app.logger.info("Orphan cleanup found nothing to delete")
            return

        lines = []
        if removed:
            lines.append(
                f"Deleted {len(removed)} orphaned partial file(s) older than "
                f"{ORPHAN_MAX_AGE_DAYS} days:"
            )
            lines.extend(f"  {entry}" for entry in removed)
        if removed_dirs:
            lines.append(
                f"Removed {len(removed_dirs)} leftover director"
                f"{'y' if len(removed_dirs) == 1 else 'ies'} (empty, or "
                f"holding only @eaDir/macOS metadata/stray images) untouched "
                f"for {ORPHAN_MAX_AGE_DAYS} days:"
            )
            lines.extend(f"  {entry}" for entry in removed_dirs)
        if dropped_scratch_db:
            lines.append(
                f"Dropped the leftover {RESTORE_CHECK_DATABASE} scratch database "
                f"from a failed restore drill."
            )
        task_send_email(
            "Fitzflix orphaned-file cleanup",
            sender=config["SERVER_EMAIL"],
            recipients=[config["ADMIN_EMAIL"]],
            text_body="\n".join(lines),
            html_body=None,
        )


def _drop_leftover_restore_database():
    """Drop the scratch database of the restore drill if a failed drill left it.

    This runs on the same single-worker maintenance queue as
    restore_drill. Thus, it can never run while a drill is in the middle
    of a restore.
    """

    url = make_url(current_app.config["SQLALCHEMY_DATABASE_URI"])
    if not url.drivername.startswith("mysql"):
        return False

    mysql_env = dict(os.environ)
    if url.password:
        mysql_env["MYSQL_PWD"] = url.password
    mysql_command = [
        current_app.config["MYSQL_BIN"],
        f"--user={url.username}",
    ]
    if url.host:
        mysql_command.append(f"--host={url.host}")
    if url.port:
        mysql_command.append(f"--port={url.port}")

    try:
        leftover = subprocess.run(
            mysql_command
            + ["-N", "-e", f"SHOW DATABASES LIKE '{RESTORE_CHECK_DATABASE}'"],
            env=mysql_env,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if not leftover:
            return False
        subprocess.run(
            mysql_command + ["-e", f"DROP DATABASE {RESTORE_CHECK_DATABASE}"],
            env=mysql_env,
            check=True,
            capture_output=True,
        )
    except Exception as e:
        current_app.logger.error(f"Couldn't drop {RESTORE_CHECK_DATABASE}: {e}")
        return False

    current_app.logger.info(
        f"Dropped the leftover {RESTORE_CHECK_DATABASE} scratch database"
    )
    return True


def _probe_http(url, **kwargs):
    """Probe an HTTP endpoint and raise on a failure or an error status."""

    r = requests.get(url, timeout=10, **kwargs)
    r.raise_for_status()


def smb_handle_sweep():
    """Ask every library file if the NAS still holds its handle.

    The lost-handle state is silent. Reads succeed. Only close(2) fails.
    Thus, nothing finds the state until the final close of an upload
    does, hours later, from inside s3transfer, with an S3 traceback. A
    direct question costs 1 open and 1 close per file, about 1 minute
    for the whole library. It turns that surprise into a report.

    The recheck runs afterwards. Thus, the recoveries that this sweep
    saw go into the history. The history is the only place that keeps
    a duration.
    """

    with app.app_context():
        from app.models import File
        from app.smb_probe import (
            absent,
            library_path,
            lost_handle,
            probe_path,
            recheck,
            record_result,
            share_responsive,
            share_root,
            unmounted,
        )

        job = get_current_job()
        files = File.query.order_by(File.file_path).all()

        broken = []
        unreadable = []
        not_local = 0
        offline_shares = set()

        # The sweep health-checks each share ONCE, through the watchdog
        # of volume_alive, before it opens the files of that share
        # (#237). An unmounted share fails the probes fast. But a WEDGED
        # share is still in the mount table, and its syscalls hang. It
        # would stall the next os.open of the sweep until the job timeout
        # killed it. That would lose the rest of the sweep, the recheck,
        # and the history write. It would also occupy the maintenance
        # queue for the hour.

        share_alive = {}

        for i, file in enumerate(files):
            if job and i % 500 == 0:
                job.meta["description"] = "Probing library files for lost SMB handles"
                job.meta["progress"] = int((i / len(files)) * 100) if files else 100
                job.save_meta()

            path = library_path(file)
            share = share_root(path)
            if share not in share_alive:
                share_alive[share] = share_responsive(path)
                if not share_alive[share]:
                    offline_shares.add(share)
            if not share_alive[share]:
                continue

            result = probe_path(path)
            record_result(result, context="nightly sweep")

            if lost_handle(result):
                broken.append(file)

            elif unmounted(result):
                offline_shares.add(share_root(path))

            elif absent(result):
                # A superseded edition keeps its row and its S3 archive
                # after the local copy goes. Thousands are legitimately
                # absent. None of them is a finding.

                not_local += 1

            elif not result["ok"]:
                unreadable.append(file)

        # An unmounted share makes every file on it look missing at one
        # time. That says nothing about handles. It says everything about
        # the mount.

        for share in sorted(offline_shares):
            current_app.logger.error(
                f"SMB sweep: '{share}' is not mounted or not responding, so "
                f"none of its files were probed"
            )

        for file in broken:
            current_app.logger.warning(
                f"SMB sweep: '{file.basename}' is in the lost-handle state "
                f"(close returns EBADF); reads still work, but anything that "
                f"closes this file will fail until the NAS clears it"
            )

        for file in unreadable:
            current_app.logger.warning(
                f"SMB sweep: '{file.basename}' could not be read"
            )

        report = recheck()

        for result in report.healed:
            held = result.get("held_for_seconds")
            duration = f" after at least {held / 60:.0f} minute(s)" if held else ""
            current_app.logger.info(
                f"SMB sweep: '{os.path.basename(result['path'])}' has come out "
                f"of the lost-handle state{duration}"
            )

        current_app.logger.info(
            f"SMB sweep: {len(files)} file(s) probed, {len(broken)} in the "
            f"lost-handle state, {len(unreadable)} otherwise unreadable, "
            f"{not_local} not on the local volume, "
            f"{len(report.healed)} recovery(ies) recorded"
        )

        return True


def health_probe():
    """Probe the external services, record the results, and alert on problems.

    The probe results go into a Redis hash that the admin page reads.
    Thus, a page load never makes an external call itself. Fitzflix
    emails a problem when it first appears. An external service must
    fail 2 consecutive probes first. It emails the problem again daily
    while the problem continues, and 1 more time when it recovers.
    """

    with app.app_context():
        config = current_app.config
        redis = current_app.redis
        results = {}

        def probe(service, check):
            started = time.perf_counter()
            try:
                check()
                ok, detail = True, "ok"
            except Exception as e:
                ok, detail = False, f"{type(e).__name__}: {e}"[:200]
            results[service] = {
                "ok": ok,
                "detail": detail,
                "latency_ms": round((time.perf_counter() - started) * 1000),
                "checked": datetime.now(timezone.utc).isoformat(),
            }

        if config["TMDB_API_KEY"]:
            probe(
                "TMDB",
                lambda: _probe_http(
                    f"{config['TMDB_API_URL']}/configuration",
                    params={"api_key": config["TMDB_API_KEY"]},
                ),
            )

        if config["SONARR_URL"] and config["SONARR_API_KEY"]:
            probe(
                "Sonarr",
                lambda: _probe_http(
                    f"{config['SONARR_URL']}/api/v3/system/status",
                    headers={"X-Api-Key": config["SONARR_API_KEY"]},
                ),
            )

        if config["RADARR_URL"] and config["RADARR_API_KEY"]:
            probe(
                "Radarr",
                lambda: _probe_http(
                    f"{config['RADARR_URL']}/api/v3/system/status",
                    headers={"X-Api-Key": config["RADARR_API_KEY"]},
                ),
            )

        if (
            config["AWS_BUCKET"]
            and config["AWS_ACCESS_KEY"]
            and config["AWS_SECRET_KEY"]
        ):
            from app.videos import aws_s3_client

            probe(
                "AWS S3",
                lambda: aws_s3_client(with_retries=False).head_bucket(
                    Bucket=config["AWS_BUCKET"]
                ),
            )

        pipe = redis.pipeline()
        pipe.delete(PROBES_KEY)
        if results:
            pipe.hset(
                PROBES_KEY,
                mapping={service: json.dumps(r) for service, r in results.items()},
            )
        pipe.execute()

        # Collect every current problem as condition -> message. An
        # external service must fail 2 times in a row. Thus, one flaky
        # request or a service in a restart does not cause an alert.

        issues = {}
        for service, result in results.items():
            if result["ok"]:
                redis.hdel(FAILCOUNT_KEY, service)
            else:
                failures = redis.hincrby(FAILCOUNT_KEY, service, 1)
                if failures >= 2:
                    issues[f"probe:{service}"] = (
                        f"{service} has failed {failures} consecutive probes: "
                        f"{result['detail']}"
                    )

        for volume in disk_health(config):
            if not volume["ok"]:
                issues[f"disk:{volume['mount']}"] = (
                    f"Volume {volume['mount']} has only {volume['free']} free "
                    f"({volume['percent']}% used), below the "
                    f"{config['DISK_ALERT_FREE_GB']} GB alert threshold"
                )

        backup = backup_health(config)
        if not backup["ok"]:
            if backup["last"] is None:
                issues["backup"] = "No database backups exist yet"
            else:
                issues["backup"] = (
                    f"The newest database backup is from "
                    f"{backup['last'].strftime('%Y-%m-%d %H:%M %Z')}, more than "
                    f"{BACKUP_STALE_HOURS} hours ago"
                )

        worker_entries = worker_health(redis)
        for entry in worker_entries:
            if not entry["ok"]:
                issues[f"workers:{entry['queue']}"] = (
                    f"Queue {entry['queue']} has {entry['live']} of "
                    f"{entry['expected']} expected workers"
                )

        dead_mounts = missing_volumes(config)
        for mount in dead_mounts:
            issues[f"mount:{mount}"] = (
                f"Volume {mount} is not mounted or not responding"
            )

        # Self-healing: list the registry-swept workers again on every
        # run. Bring back the dead worker processes and the ones that lost
        # their identity when a queue is short. Remount the dead network
        # volumes.

        heal_actions = repair_worker_registry(redis)
        if any(not entry["ok"] for entry in worker_entries):
            heal_actions += heal_worker_processes(redis, config)
        if dead_mounts:
            heal_actions += heal_mounts(dead_mounts, redis, config)
        for action in heal_actions:
            current_app.logger.warning(f"Health self-heal: {action}")

        observers = observer_health(redis)
        if not observers["ok"]:
            issues["observer"] = (
                f"Only {observers['watchers']} of {observers['expected']} "
                f"import-directory watchers are alive"
            )

        if not scheduler_health(redis)["ok"]:
            issues["scheduler"] = "The rq scheduler is not running"

        # Compare with the problems known from the previous run to find
        # what is new and what recovered. Fitzflix reports a continued
        # problem again when its daily reminder key expires.

        previous = {
            condition.decode(): message.decode()
            for condition, message in redis.hgetall(ISSUES_KEY).items()
        }
        recovered = {c: m for c, m in previous.items() if c not in issues}
        report = {
            condition: message
            for condition, message in issues.items()
            if condition not in previous
            or not redis.exists(f"{ALERTED_KEY_PREFIX}{condition}")
        }

        for message in issues.values():
            current_app.logger.warning(f"Health: {message}")
        for message in recovered.values():
            current_app.logger.info(f"Health recovered: {message}")

        if (report or recovered) and config["MAIL_SERVER"] and config["ADMIN_EMAIL"]:
            lines = []
            if report:
                lines.append("Problems:")
                lines.extend(f"  - {message}" for message in report.values())
            if recovered:
                if lines:
                    lines.append("")
                lines.append("Recovered:")
                lines.extend(f"  - {message}" for message in recovered.values())
            if heal_actions:
                if lines:
                    lines.append("")
                lines.append("Self-healing:")
                lines.extend(f"  - {action}" for action in heal_actions)

            subject = (
                f"Fitzflix health: {len(issues)} problem(s)"
                if issues
                else "Fitzflix health: all clear"
            )
            task_send_email(
                subject,
                sender=config["SERVER_EMAIL"],
                recipients=[config["ADMIN_EMAIL"]],
                text_body="\n".join(lines),
                html_body=None,
            )
            for condition in report:
                redis.set(
                    f"{ALERTED_KEY_PREFIX}{condition}",
                    "1",
                    ex=ALERT_REMINDER_SECONDS,
                )
            for condition in recovered:
                redis.delete(f"{ALERTED_KEY_PREFIX}{condition}")

        pipe = redis.pipeline()
        pipe.delete(ISSUES_KEY)
        if issues:
            pipe.hset(ISSUES_KEY, mapping=issues)
        pipe.execute()

        return f"{len(results)} service(s) probed, {len(issues)} problem(s)"
