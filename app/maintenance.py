"""Tasks for maintaining the application itself, rather than the video library."""

import glob
import gzip
import json
import os
import shutil
import subprocess
import time

from datetime import datetime, timedelta, timezone

import requests

from flask import current_app
from rq import Worker, get_current_job
from rq.registry import StartedJobRegistry
from sqlalchemy import text
from sqlalchemy.engine import make_url
from werkzeug.local import LocalProxy

from app import db, get_app
from app.email import task_send_email

# This process's app instance, resolved lazily so importing this module from
# a process that already has an application doesn't build a second one

app = LocalProxy(get_app)

# The worker roster from fitzflix_supervisor.ini: the queues each program
# listens to, and how many processes it runs (numprocs, default 1). Update
# when the roster changes there; the expected per-queue counts and the
# self-healing restart targets are both derived from it

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

PROGRAM_COUNTS = {"fitzflix-import": 2, "fitzflix-file-operation": 2}

EXPECTED_WORKERS = {}
for _program, _queues in PROGRAM_QUEUES.items():
    for _queue in _queues:
        EXPECTED_WORKERS[_queue] = EXPECTED_WORKERS.get(
            _queue, 0
        ) + PROGRAM_COUNTS.get(_program, 1)

# rq's default worker_ttl; an idle worker whose heartbeat is older than this
# is a leftover registration, not a live worker

WORKER_HEARTBEAT_STALE_SECONDS = 420

PROBES_KEY = "fitzflix:health:probes"
FAILCOUNT_KEY = "fitzflix:health:failcount"
ISSUES_KEY = "fitzflix:health:issues"
ALERTED_KEY_PREFIX = "fitzflix:health:alerted:"
OBSERVER_KEY_PREFIX = "fitzflix:observer:"
SCHEDULER_KEY_PREFIX = "rq:scheduler_instance:"

# While a problem persists, re-alert daily rather than every probe

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
    """Discover workers from their heartbeat keys, not rq's registry set.

    A worker that misses one heartbeat deadline (e.g. under heavy load) can
    be swept out of the rq:workers set by rq's registry cleanup and never
    re-adds itself, while continuing to work and heartbeat its own key. The
    TTL'd per-worker keys are therefore the ground truth for liveness.
    """

    workers = []
    for key in connection.scan_iter("rq:worker:*"):
        # A cleanly shut-down worker's key lingers briefly with a death
        # timestamp; it's not a live worker

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
    """Summarize rq worker liveness per queue against the expected roster."""

    now = datetime.utcnow()
    queues = {name: {"queue": name, "live": 0, "busy": []} for name in EXPECTED_WORKERS}
    for worker in _live_workers(connection):
        # A busy worker stops refreshing its heartbeat for the duration of
        # the job, so only idle workers can be considered stale

        if (
            worker.get_state() != "busy"
            and worker.last_heartbeat
            and (now - worker.last_heartbeat).total_seconds()
            > WORKER_HEARTBEAT_STALE_SECONDS
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

            # Report the running job only under the queue it came from, not
            # under every queue its worker listens to

            if job is not None and job.origin == name:
                entry["busy"].append(job.description or job.id)

    for entry in queues.values():
        entry["expected"] = EXPECTED_WORKERS.get(entry["queue"])
        entry["ok"] = entry["expected"] is None or entry["live"] >= entry["expected"]

    return [queues[name] for name in sorted(queues)]


def disk_health(config):
    """Report usage for each distinct volume backing the app's directories."""

    alert_free_bytes = config["DISK_ALERT_FREE_GB"] * 1024**3
    volumes = {}
    for path in (
        config["MEDIA_LOCATION"],
        config["IMPORT_DIR"],
        config["MOVIE_LIBRARY"],
        config["TV_LIBRARY"],
        config["TRANSCODES_DIR"],
        config["REJECTS_DIR"],
        config["DB_BACKUP_DIR"],
        os.path.dirname(config["LOG_FILE"]),
    ):
        if not os.path.isdir(path):
            continue
        device = os.stat(path).st_dev
        if device in volumes:
            continue

        # Walk up to the mountpoint so the gauge is labeled by volume,
        # not by whichever configured directory happened to find it first

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
            # The library volumes are kept nearly full by design, so alert on
            # the absolute free space imports and transcodes need, not percent
            "ok": usage.free >= alert_free_bytes,
        }

    return sorted(volumes.values(), key=lambda volume: volume["mount"])


def backup_health(config):
    """Report when the newest database backup was made."""

    backups = glob.glob(os.path.join(config["DB_BACKUP_DIR"], "*.sql.gz"))
    newest = max((os.path.getmtime(path) for path in backups), default=None)
    if newest is None:
        return {"ok": False, "last": None}
    last = datetime.fromtimestamp(newest, tz=timezone.utc)
    age_hours = (datetime.now(timezone.utc) - last).total_seconds() / 3600
    return {"ok": age_hours <= BACKUP_STALE_HOURS, "last": last}


def observer_health(connection):
    """Count the processes with a live import-directory observer heartbeat."""

    watchers = sum(1 for _ in connection.scan_iter(f"{OBSERVER_KEY_PREFIX}*"))
    return {"ok": watchers > 0, "watchers": watchers}


def scheduler_health(connection):
    """Report whether an rq-scheduler instance is registered and alive.

    The scheduler registers itself under a key that expires shortly after
    its polling interval, so a live key means a live scheduler.
    """

    alive = any(True for _ in connection.scan_iter(f"{SCHEDULER_KEY_PREFIX}*"))
    return {"ok": alive}


def repair_worker_registry(connection):
    """Re-list live workers that rq's registry sweep dropped.

    The intact-but-unlisted state: the worker's hash still has its queues
    field and a live heartbeat, but a momentary key expiry got it removed
    from the rq:workers set, which workers only join at birth. Adding it
    back is a pure repair with no restart.
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
    """Run a supervisorctl command; return (succeeded, output)."""

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
    """Start dead worker processes, and restart amnesiac ones.

    Two failure states, told apart with supervisor's process view:
    - Not RUNNING: the process died and supervisor gave up (or it was
      stopped); start it again.
    - RUNNING but its pid isn't attached to any registered rq worker: the
      process is alive but its Redis registration lost its identity (a full
      key expiry wiped the queues field), which no supervisor-level check
      can see and only a restart (a fresh birth registration) can fix. Only
      restarted while its queues are idle, so no job is killed mid-run.

    Each process is healed at most once per cooldown window, so a genuinely
    sick worker can't cause a restart loop.
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


def probe_health(connection):
    """Return the latest external-service probe results written by health_probe."""

    results = []
    for service, raw in sorted(connection.hgetall(PROBES_KEY).items()):
        result = json.loads(raw)
        result["service"] = service.decode()
        result["checked"] = datetime.fromisoformat(result["checked"])
        results.append(result)
    return results


def system_health(flask_app):
    """Collect the metrics shown on the admin page's system health card.

    Everything here reads Redis or the local filesystem; the external-service
    results come from the Redis hash the health_probe task maintains, so
    rendering the card never makes a network call.
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
        "backup": backup_health(flask_app.config),
        "observer": observer_health(flask_app.redis),
        "scheduler": scheduler_health(flask_app.redis),
        "probes": probe_health(flask_app.redis),
    }


def rotate_logs():
    """Archive the current log file and prune archives past the retention window.

    The log file is renamed with a date suffix and gzipped; every process
    writes through a WatchedFileHandler, so each reopens the fresh log file on
    its next write. Archives older than LOG_RETENTION_DAYS are deleted.
    """

    with app.app_context():
        log_file = current_app.config["LOG_FILE"]
        retention_days = current_app.config["LOG_RETENTION_DAYS"]

        archived = None
        if os.path.isfile(log_file) and os.path.getsize(log_file) > 0:
            stamp = datetime.now().strftime("%Y-%m-%d")
            archive = f"{log_file}.{stamp}"
            if os.path.exists(archive) or os.path.exists(f"{archive}.gz"):
                # Already rotated today; timestamp instead of clobbering
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
    """Dump the database to a compressed backup and prune old backups.

    The media files are archived at AWS, but the database — reviews,
    Criterion details, shopping priorities — exists only here, so it gets a
    nightly dump with its own retention window.
    """

    with app.app_context():
        backup_dir = current_app.config["DB_BACKUP_DIR"]
        retention_days = current_app.config["DB_BACKUP_RETENTION_DAYS"]
        url = make_url(current_app.config["SQLALCHEMY_DATABASE_URI"])

        os.makedirs(backup_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d")
        backup_file = os.path.join(backup_dir, f"{url.database}-{stamp}.sql.gz")
        if os.path.exists(backup_file):
            # Already backed up today; timestamp instead of clobbering
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

        # Pass the password through the environment so it doesn't appear in
        # the process list

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
            # Don't leave a partial backup that looks like a good one
            if os.path.exists(backup_file):
                os.remove(backup_file)
            raise

        size_mb = round(os.path.getsize(backup_file) / 1024 / 1024, 1)

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
            f"({size_mb} MB), deleted {len(deleted)} backup(s) older than "
            f"{retention_days} days{' ' + str(deleted) if deleted else ''}"
        )
        return True


def _probe_http(url, **kwargs):
    """Probe an HTTP endpoint, raising on any failure or error status."""

    r = requests.get(url, timeout=10, **kwargs)
    r.raise_for_status()


def health_probe():
    """Probe external services, record results, and alert on problems.

    Probe results land in a Redis hash the admin page reads, so page loads
    never make external calls themselves. A problem is emailed when it first
    appears (external services must fail two consecutive probes first), again
    daily while it persists, and once more when it recovers.
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
                "TMDb",
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

        # Gather every current problem into condition -> message. External
        # services must fail twice in a row, so one flaky request or a service
        # mid-restart doesn't fire an alert

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

        # Self-healing: re-list registry-swept workers every run, and bring
        # dead or amnesiac worker processes back when a queue is short

        heal_actions = repair_worker_registry(redis)
        if any(not entry["ok"] for entry in worker_entries):
            heal_actions += heal_worker_processes(redis, config)
        for action in heal_actions:
            current_app.logger.warning(f"Health self-heal: {action}")

        if not observer_health(redis)["ok"]:
            issues["observer"] = "No process is watching the import directory"

        if not scheduler_health(redis)["ok"]:
            issues["scheduler"] = "The rq scheduler is not running"

        # Compare against the problems known from the previous run to find
        # what's new and what recovered; persisting problems are re-reported
        # once their daily reminder key expires

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
