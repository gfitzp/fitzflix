"""Test the system health checks.

This covers the alert state machine of the probe task: the entry alert
after 2 consecutive failures, the debounce, the daily reminder, and the
recovery notice. It also covers the disk free-space floor and the
worker-liveness summarizer.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import app.maintenance as maintenance


@pytest.fixture
def health_env(app, monkeypatch):
    """Build a controlled environment for health_probe runs.

    This fixture fakes the scheduler and observer heartbeats. Nothing real
    runs against Redis DB 9. It clears the worker roster. It captures each
    email instead of sending it. It makes the HTTP probes fail on demand.
    """

    app.redis.set("rq:cron_scheduler:test", "1", ex=600)
    # 2 observer heartbeats. observer_health expects 1 for each import worker.
    app.redis.set("fitzflix:observer:test-1", "1", ex=600)
    app.redis.set("fitzflix:observer:test-2", "1", ex=600)
    monkeypatch.setattr(maintenance, "EXPECTED_WORKERS", {})

    # A new backup file. Thus, the backup-staleness check starts healthy.

    import os

    backup = os.path.join(app.config["DB_BACKUP_DIR"], "health-baseline.sql.gz")
    with open(backup, "wb") as f:
        f.write(b"baseline")

    sent = []
    monkeypatch.setattr(
        maintenance,
        "task_send_email",
        lambda subject, sender, recipients, text_body, html_body: sent.append(
            {"subject": subject, "body": text_body}
        ),
    )

    failing = set()

    def fake_probe_http(url, **kwargs):
        for service, marker in (
            ("TMDB", "tmdb"),
            ("Sonarr", "sonarr"),
            ("Radarr", "radarr"),
        ):
            if service in failing and marker in url.lower():
                raise ConnectionError(f"simulated {service} outage")

    monkeypatch.setattr(maintenance, "_probe_http", fake_probe_http)

    # All of the configured URLs are 127.0.0.1:1. Make them different.
    # Then fake_probe_http can fail 1 service at a time.

    monkeypatch.setitem(app.config, "TMDB_API_URL", "http://tmdb.test")
    monkeypatch.setitem(app.config, "TMDB_API_KEY", "tmdb-test-key")
    monkeypatch.setitem(app.config, "SONARR_URL", "http://sonarr.test")
    monkeypatch.setitem(app.config, "RADARR_URL", "http://radarr.test")
    monkeypatch.setitem(app.config, "MAIL_SERVER", "smtp.test")
    monkeypatch.setitem(app.config, "ADMIN_EMAIL", "alerts@example.test")
    monkeypatch.setitem(app.config, "SERVER_EMAIL", "noreply@example.test")

    def run():
        before = len(sent)
        with app.app_context():
            maintenance.health_probe()
        return sent[before:]

    return SimpleNamespace(run=run, failing=failing, sent=sent, redis=app.redis)


def test_alert_lifecycle(health_env):
    # Healthy: the probe records the results and sends no email.
    assert health_env.run() == []
    probes = {p["service"]: p for p in maintenance.probe_health(health_env.redis)}
    assert set(probes) == {"TMDB", "Sonarr", "Radarr"}
    assert all(p["ok"] for p in probes.values())

    # The probe holds back 1 failure. 2 consecutive failures send an alert.
    health_env.failing.add("TMDB")
    assert health_env.run() == []
    emails = health_env.run()
    assert len(emails) == 1
    assert "TMDB has failed 2 consecutive probes" in emails[0]["body"]

    # While the alert is active, the probe is silent.
    assert health_env.run() == []

    # After the daily reminder key expires, the probe sends the alert again.
    health_env.redis.delete("fitzflix:health:alerted:probe:TMDB")
    reminders = health_env.run()
    assert len(reminders) == 1
    assert "consecutive probes" in reminders[0]["body"]

    # Recovery: the probe sends 1 notice, then clears all of the state.
    health_env.failing.clear()
    recoveries = health_env.run()
    assert len(recoveries) == 1
    assert "Recovered:" in recoveries[0]["body"]
    assert "all clear" in recoveries[0]["subject"]
    assert not health_env.redis.hgetall("fitzflix:health:issues")
    assert not health_env.redis.exists("fitzflix:health:alerted:probe:TMDB")

    # A healthy run after the recovery is silent again.
    assert health_env.run() == []


def test_disk_floor_alerts_and_recovers(app, health_env, monkeypatch):
    monkeypatch.setitem(app.config, "DISK_ALERT_FREE_GB", 10**6)
    emails = health_env.run()
    with app.app_context():
        disks = maintenance.disk_health(app.config)
    assert disks and not any(d["ok"] for d in disks)
    assert len(emails) == 1
    assert all(f"Volume {d['mount']}" in emails[0]["body"] for d in disks)

    monkeypatch.setitem(app.config, "DISK_ALERT_FREE_GB", 0)
    recoveries = health_env.run()
    assert len(recoveries) == 1
    assert "Recovered:" in recoveries[0]["body"]


def test_missing_scheduler_and_observer_are_reported(health_env):
    health_env.redis.delete("rq:cron_scheduler:test")
    health_env.redis.delete("fitzflix:observer:test-1")
    health_env.redis.delete("fitzflix:observer:test-2")
    emails = health_env.run()
    assert len(emails) == 1
    assert "0 of 2 import-directory watchers are alive" in emails[0]["body"]
    assert "scheduler is not running" in emails[0]["body"]


def test_observer_health_expects_one_watcher_per_import_worker(app):
    """Report the observer as unhealthy if fewer than 2 processes run.

    The observer covers only the import workers. That program has 2
    processes. Fewer than 2 is unhealthy. This includes the state with 1
    watcher. The old any-heartbeat check called that state healthy."""

    import os

    # The test app runs its own observer. Its keeper rewrites this key
    # every 60 seconds. Delete the key directly before the counting
    # asserts. Then a tick during the test cannot increase the count.

    own_heartbeat = f"fitzflix:observer:{os.getpid()}"

    app.redis.delete(own_heartbeat)
    health = maintenance.observer_health(app.redis)
    assert health["expected"] == 2
    assert health["ok"] is False

    app.redis.set("fitzflix:observer:one", "1", ex=600)
    app.redis.delete(own_heartbeat)
    assert maintenance.observer_health(app.redis)["ok"] is False

    app.redis.set("fitzflix:observer:two", "1", ex=600)
    assert maintenance.observer_health(app.redis)["ok"] is True


def _worker(state, queues, heartbeat_age_seconds=0, job=None):
    return SimpleNamespace(
        get_state=lambda: state,
        last_heartbeat=datetime.utcnow() - timedelta(seconds=heartbeat_age_seconds),
        queue_names=lambda: queues,
        get_current_job=lambda: job,
    )


def test_worker_health_counts_and_staleness(app, monkeypatch):
    busy_job = SimpleNamespace(
        origin="fitzflix-import", description="'Jaws (1975) - [DVD].mkv'", id="jaws"
    )
    workers = [
        _worker("idle", ["fitzflix-import", "fitzflix-file-operation"]),
        _worker("busy", ["fitzflix-import", "fitzflix-file-operation"], job=busy_job),
        # A stale idle worker is a leftover registration. It does not count.
        _worker("idle", ["fitzflix-sql"], heartbeat_age_seconds=900),
        # A busy worker is never stale. The age of the heartbeat is not important.
        _worker("busy", ["fitzflix-transcode"], heartbeat_age_seconds=900),
    ]
    monkeypatch.setattr(maintenance, "_live_workers", lambda connection: workers)
    monkeypatch.setattr(
        maintenance,
        "EXPECTED_WORKERS",
        {"fitzflix-import": 2, "fitzflix-sql": 1, "fitzflix-transcode": 1},
    )

    entries = {e["queue"]: e for e in maintenance.worker_health(app.redis)}

    assert entries["fitzflix-import"]["live"] == 2
    assert entries["fitzflix-import"]["ok"]
    # The busy job shows only under its origin queue.
    assert entries["fitzflix-import"]["busy"] == ["'Jaws (1975) - [DVD].mkv'"]
    assert entries["fitzflix-file-operation"]["busy"] == []
    assert entries["fitzflix-sql"]["live"] == 0
    assert not entries["fitzflix-sql"]["ok"]
    assert entries["fitzflix-transcode"]["live"] == 1
    assert entries["fitzflix-transcode"]["ok"]


def test_expected_workers_derived_from_program_roster():
    assert maintenance.EXPECTED_WORKERS == {
        "fitzflix-user-request": 2,
        "fitzflix-import": 5,
        "fitzflix-file-operation": 5,
        "fitzflix-transcode": 1,
        "fitzflix-sql": 1,
        "fitzflix-maintenance": 1,
    }


def _fake_worker_key(redis, name, queues, pid=None):
    """Write a worker hash in the same shape that the rq heartbeat and birth write."""

    from rq.utils import utcformat

    key = f"rq:worker:{name}"
    mapping = {
        "queues": queues,
        "state": "idle",
        "last_heartbeat": utcformat(datetime.utcnow()),
    }
    if pid is not None:
        mapping["pid"] = pid
    redis.hset(key, mapping=mapping)
    redis.expire(key, 120)
    return key


def test_worker_health_sees_workers_missing_from_registry_set(app):
    """Test the incident case: a live worker that is gone from rq:workers."""

    _fake_worker_key(app.redis, "unlisted", "fitzflix-sql")
    assert not app.redis.sismember("rq:workers", "rq:worker:unlisted")

    entries = {e["queue"]: e for e in maintenance.worker_health(app.redis)}
    assert entries["fitzflix-sql"]["live"] == 1
    assert entries["fitzflix-sql"]["ok"]


def test_repair_worker_registry_relists_intact_workers(app):
    key = _fake_worker_key(app.redis, "unlisted", "fitzflix-sql")

    actions = maintenance.repair_worker_registry(app.redis)
    assert actions == [f"re-listed {key} in the worker registry"]
    assert app.redis.sismember("rq:workers", key)

    # Idempotent: after the worker is listed again, there is nothing more to do.
    assert maintenance.repair_worker_registry(app.redis) == []

    # A bare hash has no queues field. Its identity is lost. Fitzflix
    # cannot list it again.
    app.redis.hset("rq:worker:bare", "last_heartbeat", "2026-01-01T00:00:00.000000Z")
    assert maintenance.repair_worker_registry(app.redis) == []
    assert not app.redis.sismember("rq:workers", "rq:worker:bare")

    # The hash of a worker that shut down cleanly can remain. Fitzflix
    # does not list it again.
    dying = _fake_worker_key(app.redis, "dying", "fitzflix-sql")
    app.redis.hset(dying, "death", "2026-01-01T00:00:00.000000Z")
    assert maintenance.repair_worker_registry(app.redis) == []
    assert not app.redis.sismember("rq:workers", dying)


def test_worker_health_ignores_dying_workers(app):
    """Do not count a stopped worker as live.

    The key of a stopped worker remains for a short time with a death
    stamp. If Fitzflix counts it as live, a dead process looks healthy
    for 1 minute."""

    key = _fake_worker_key(app.redis, "dying", "fitzflix-sql")
    app.redis.hset(key, "death", "2026-01-01T00:00:00.000000Z")

    entries = {e["queue"]: e for e in maintenance.worker_health(app.redis)}
    assert entries["fitzflix-sql"]["live"] == 0


def test_supervisor_status_parsing(app, monkeypatch):
    canned = (
        "fitzflix:fitzflix-sql_00              RUNNING   pid 44324, uptime 16:35:46\n"
        "fitzflix:fitzflix-web_00              STOPPED   Aug 03 08:00 AM\n"
        "fitzflix:fitzflix-import_00           FATAL     Exited too quickly\n"
    )
    monkeypatch.setattr(
        maintenance.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout=canned, returncode=0),
    )
    assert maintenance._supervisor_status(app.config) == {
        "fitzflix:fitzflix-sql_00": ("RUNNING", 44324),
        "fitzflix:fitzflix-web_00": ("STOPPED", None),
        "fitzflix:fitzflix-import_00": ("FATAL", None),
    }


@pytest.fixture
def heal_env(app, monkeypatch):
    """Run heal_worker_processes with a fake supervisor and capture the commands."""

    env = SimpleNamespace(status={}, calls=[])
    monkeypatch.setattr(maintenance, "_supervisor_status", lambda config: env.status)

    def fake_run(config, command, process_name):
        env.calls.append((command, process_name))
        return True, "ok"

    monkeypatch.setattr(maintenance, "_run_supervisorctl", fake_run)

    def heal():
        env.calls.clear()
        with app.app_context():
            return maintenance.heal_worker_processes(app.redis, app.config)

    env.heal = heal
    return env


def test_heal_starts_dead_processes_once_per_cooldown(app, heal_env):
    heal_env.status = {"fitzflix:fitzflix-sql_00": ("STOPPED", None)}

    actions = heal_env.heal()
    assert heal_env.calls == [("start", "fitzflix:fitzflix-sql_00")]
    assert actions == ["started fitzflix:fitzflix-sql_00 (was STOPPED)"]

    # In the cooldown window, there is no second attempt.
    assert heal_env.heal() == []
    assert heal_env.calls == []


def test_heal_restarts_amnesiac_worker_when_idle(app, heal_env):
    # A registered worker of a different program. Thus, its pid is known.
    _fake_worker_key(app.redis, "request", "fitzflix-user-request", pid=1000)
    heal_env.status = {
        "fitzflix:fitzflix-user-request_00": ("RUNNING", 1000),
        "fitzflix:fitzflix-sql_00": ("RUNNING", 2000),  # the pid is not registered
    }

    actions = heal_env.heal()
    assert heal_env.calls == [("restart", "fitzflix:fitzflix-sql_00")]
    assert actions == [
        "restarted fitzflix:fitzflix-sql_00 (running, but not registered "
        "as an rq worker)"
    ]


def test_heal_defers_amnesiac_restart_while_queue_busy(app, heal_env):
    heal_env.status = {"fitzflix:fitzflix-sql_00": ("RUNNING", 2000)}
    app.redis.lpush("rq:queue:fitzflix-sql", "some-queued-job")

    assert heal_env.heal() == []
    assert heal_env.calls == []

    # When the queue is empty, the deferred restart occurs.
    app.redis.delete("rq:queue:fitzflix-sql")
    actions = heal_env.heal()
    assert heal_env.calls == [("restart", "fitzflix:fitzflix-sql_00")]
    assert len(actions) == 1


def test_heal_never_touches_non_worker_running_processes(app, heal_env):
    # web and rqscheduler have no rq registration. Do not touch RUNNING.
    heal_env.status = {
        "fitzflix:fitzflix-web_00": ("RUNNING", 3000),
        "fitzflix:fitzflix-rqscheduler_00": ("RUNNING", 3001),
    }
    assert heal_env.heal() == []
    assert heal_env.calls == []


def test_human_size():
    assert maintenance._human_size(512) == "512.0 B"
    assert maintenance._human_size(2048) == "2.0 KB"
    assert maintenance._human_size(3 * 1024**3) == "3.0 GB"
    assert maintenance._human_size(int(1.5 * 1024**4)) == "1.5 TB"


def test_backup_health_reads_newest_backup(app, tmp_path, monkeypatch):
    monkeypatch.setitem(app.config, "DB_BACKUP_DIR", str(tmp_path))
    assert maintenance.backup_health(app.config) == {"ok": False, "last": None}

    (tmp_path / "fitzflix-2026-01-01.sql.gz").write_bytes(b"old")
    fresh = tmp_path / "fitzflix-fresh.sql.gz"
    fresh.write_bytes(b"fresh")

    health = maintenance.backup_health(app.config)
    assert health["ok"]
    assert health["last"] is not None
