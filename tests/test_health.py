"""System health: the probe task's alert state machine (entry alert after two
consecutive failures, debounce, daily reminder, recovery notice), the disk
free-space floor, and the worker-liveness summarizer.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import app.maintenance as maintenance


@pytest.fixture
def health_env(app, monkeypatch):
    """A controlled environment for health_probe runs.

    Fakes the scheduler and observer heartbeats (nothing real runs against
    Redis DB 9), blanks the worker roster, captures email instead of sending,
    and makes the HTTP probes fail on demand.
    """

    app.redis.set("rq:scheduler_instance:test", "1", ex=600)
    app.redis.set("fitzflix:observer:test", "1", ex=600)
    monkeypatch.setattr(maintenance, "EXPECTED_WORKERS", {})

    # A fresh backup file, so the backup-staleness check starts healthy

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
            ("TMDb", "tmdb"),
            ("Sonarr", "sonarr"),
            ("Radarr", "radarr"),
        ):
            if service in failing and marker in url.lower():
                raise ConnectionError(f"simulated {service} outage")

    monkeypatch.setattr(maintenance, "_probe_http", fake_probe_http)

    # The configured URLs are all 127.0.0.1:1; make them distinguishable so
    # fake_probe_http can fail one service at a time

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
    # Healthy: results recorded, no email
    assert health_env.run() == []
    probes = {p["service"]: p for p in maintenance.probe_health(health_env.redis)}
    assert set(probes) == {"TMDb", "Sonarr", "Radarr"}
    assert all(p["ok"] for p in probes.values())

    # One failure is gated, two consecutive failures alert
    health_env.failing.add("TMDb")
    assert health_env.run() == []
    emails = health_env.run()
    assert len(emails) == 1
    assert "TMDb has failed 2 consecutive probes" in emails[0]["body"]

    # While alerted: silent
    assert health_env.run() == []

    # After the daily reminder key expires: re-alert
    health_env.redis.delete("fitzflix:health:alerted:probe:TMDb")
    reminders = health_env.run()
    assert len(reminders) == 1
    assert "consecutive probes" in reminders[0]["body"]

    # Recovery: one notice, then all state cleared
    health_env.failing.clear()
    recoveries = health_env.run()
    assert len(recoveries) == 1
    assert "Recovered:" in recoveries[0]["body"]
    assert "all clear" in recoveries[0]["subject"]
    assert not health_env.redis.hgetall("fitzflix:health:issues")
    assert not health_env.redis.exists("fitzflix:health:alerted:probe:TMDb")

    # And a healthy run after recovery is silent again
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
    health_env.redis.delete("rq:scheduler_instance:test")
    health_env.redis.delete("fitzflix:observer:test")
    emails = health_env.run()
    assert len(emails) == 1
    assert "No process is watching the import directory" in emails[0]["body"]
    assert "scheduler is not running" in emails[0]["body"]


def _worker(state, queues, heartbeat_age_seconds=0, job=None):
    return SimpleNamespace(
        state=state,
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
        # Stale idle worker: a leftover registration, not counted
        _worker("idle", ["fitzflix-sql"], heartbeat_age_seconds=900),
        # Busy workers are never stale, however old the heartbeat
        _worker("busy", ["fitzflix-transcode"], heartbeat_age_seconds=900),
    ]
    monkeypatch.setattr(
        maintenance, "Worker", SimpleNamespace(all=lambda connection: workers)
    )
    monkeypatch.setattr(
        maintenance,
        "EXPECTED_WORKERS",
        {"fitzflix-import": 2, "fitzflix-sql": 1, "fitzflix-transcode": 1},
    )

    entries = {e["queue"]: e for e in maintenance.worker_health(app.redis)}

    assert entries["fitzflix-import"]["live"] == 2
    assert entries["fitzflix-import"]["ok"]
    # The busy job shows only under its origin queue
    assert entries["fitzflix-import"]["busy"] == ["'Jaws (1975) - [DVD].mkv'"]
    assert entries["fitzflix-file-operation"]["busy"] == []
    assert entries["fitzflix-sql"]["live"] == 0
    assert not entries["fitzflix-sql"]["ok"]
    assert entries["fitzflix-transcode"]["live"] == 1
    assert entries["fitzflix-transcode"]["ok"]


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
