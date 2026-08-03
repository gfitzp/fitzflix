"""Deferred-retry scheduling: deterministic job ids that replace rather than
stack, scheduled jobs that actually bind to their target functions (the
enqueue_in kwarg-leak bug class), cron re-registration that preserves run
history, and the import-directory watchdog.
"""

import inspect
import json
import os
import threading
import time

from datetime import datetime

import pytest

from rq.exceptions import NoSuchJobError
from rq.job import Job

from app import register_cron
from app.videos import acquire_lock_or_defer, localization_task, sync_aws_s3_storage_task


def scheduled_ids(scheduler):
    return [job.id for job, _ in scheduler.get_jobs(with_times=True)]


def assert_binds(job):
    """The scheduled call must match the target function's signature."""

    inspect.signature(job.func).bind(*(job.args or ()), **(job.kwargs or {}))


@pytest.fixture
def held_lock(app):
    """Hold a redlock for the duration of a test."""

    locks = []

    def hold(resource, ttl_ms=60000):
        lock = app.lock_manager.lock(resource, ttl_ms)
        assert lock, f"could not take test lock on {resource}"
        locks.append(lock)
        return lock

    yield hold
    for lock in locks:
        app.lock_manager.unlock(lock)


def test_defer_uses_deterministic_id_and_replaces(app, held_lock):
    with app.app_context():
        held_lock("test-title-lock")

        for _ in range(3):
            result = acquire_lock_or_defer(
                "test-title-lock",
                30000,
                app.import_scheduler,
                "app.videos.localization_task",
                minutes=(45, 75),
                timeout=3600,
                description="'Jaws (1975) - [DVD].mkv'",
                kwargs={"file_path": "/incoming/Jaws (1975) - [DVD].mkv"},
            )
            assert result is None

        retries = [
            job_id
            for job_id in scheduled_ids(app.import_scheduler)
            if job_id.startswith("retry:")
        ]
        assert retries == ["retry:localization_task:'Jaws (1975) - [DVD].mkv'"]

        job = Job.fetch(retries[0], connection=app.redis)
        assert_binds(job)

        # Scheduling kwargs must not leak into the function call (the
        # enqueue_in bug class: unrecognized kwargs pass through to the task)

        assert job.kwargs == {"file_path": "/incoming/Jaws (1975) - [DVD].mkv"}
        assert not job.args
        assert job.description == "'Jaws (1975) - [DVD].mkv'"


def test_defer_with_positional_args_binds(app, held_lock):
    with app.app_context():
        held_lock("test-args-lock")

        acquire_lock_or_defer(
            "test-args-lock",
            30000,
            app.import_scheduler,
            "app.videos.localization_task",
            minutes=(5, 15),
            timeout=3600,
            description="'args-form.mkv'",
            args=("/incoming/args-form.mkv",),
        )

        job = Job.fetch(
            "retry:localization_task:'args-form.mkv'", connection=app.redis
        )
        assert_binds(job)
        assert job.args == ("/incoming/args-form.mkv",)


def test_acquire_returns_lock_when_free(app):
    with app.app_context():
        lock = acquire_lock_or_defer(
            "test-free-lock",
            30000,
            app.import_scheduler,
            "app.videos.localization_task",
            minutes=(5, 15),
            timeout=3600,
            description="'free.mkv'",
            kwargs={"file_path": "/incoming/free.mkv"},
        )
        assert lock
        app.lock_manager.unlock(lock)
        assert "retry:localization_task:'free.mkv'" not in scheduled_ids(
            app.import_scheduler
        )


def test_localization_defers_while_title_is_locked(app, held_lock, incoming_dir):
    """The real localization path: a held title lock schedules one retry."""

    basename = "Jaws (1975) - [DVD].mkv"
    file_path = os.path.join(incoming_dir, basename)
    with open(file_path, "wb") as f:
        f.write(b"not a real video")

    identifier = json.dumps(
        {
            "title": "Jaws",
            "year": 1975,
            "feature_type": None,
            "plex_title": "Jaws (1975)",
            "edition": None,
        }
    )
    try:
        with app.app_context():
            held_lock(identifier)
            for _ in range(2):
                localization_task(file_path)

            retries = [
                job_id
                for job_id in scheduled_ids(app.import_scheduler)
                if job_id.startswith("retry:")
            ]
            assert retries == [f"retry:localization_task:'{basename}'"]
            assert_binds(Job.fetch(retries[0], connection=app.redis))
    finally:
        os.remove(file_path)


def test_localization_defers_while_file_is_growing(app, incoming_dir):
    """A file still being copied is rescheduled, not processed or rejected."""

    basename = "Growing (2020) - [DVD].mkv"
    file_path = os.path.join(incoming_dir, basename)
    with open(file_path, "wb") as f:
        f.write(b"start")

    stop = threading.Event()

    def writer():
        while not stop.is_set():
            with open(file_path, "ab") as f:
                f.write(b"more bytes")
            time.sleep(0.5)

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    try:
        with app.app_context():
            localization_task(file_path)
            retries = [
                job_id
                for job_id in scheduled_ids(app.import_scheduler)
                if job_id.startswith("retry:")
            ]
            assert retries == [f"retry:localization_task:'{basename}'"]
            assert_binds(Job.fetch(retries[0], connection=app.redis))
    finally:
        stop.set()
        thread.join()
        os.remove(file_path)

    # The file must not have been moved to rejects
    assert not os.listdir(app.config["REJECTS_DIR"])


def test_sync_defers_while_queues_are_busy(app):
    with app.app_context():
        marker = app.file_queue.enqueue(
            "app.videos.localization_task",
            args=("/nonexistent",),
            description="busy marker",
        )
        try:
            sync_aws_s3_storage_task()

            job = Job.fetch("retry:sync_aws_s3_storage_task", connection=app.redis)
            assert_binds(job)
            assert not job.args and not job.kwargs
        finally:
            marker.delete()


def test_register_cron_preserves_run_history(app):
    with app.app_context():
        def register(cron_string):
            register_cron(
                app.maintenance_scheduler,
                cron_string,
                func="app.maintenance.rotate_logs",
                job_id="test-cron",
                timeout=600,
                description="Test cron",
            )

        register("0 0 * * *")
        job = Job.fetch("test-cron", connection=app.redis)
        last_run = datetime(2026, 1, 1, 0, 0, 0)
        job.ended_at = last_run
        job.save()

        # Identical registration: run history survives
        register("0 0 * * *")
        assert Job.fetch("test-cron", connection=app.redis).ended_at == last_run

        # Changed schedule: the job is rewritten
        register("30 0 * * *")
        rewritten = Job.fetch("test-cron", connection=app.redis)
        assert rewritten.ended_at is None
        assert rewritten.meta.get("cron_string") == "30 0 * * *"


def test_watchdog_enqueues_new_import_files(app):
    """A file appearing in IMPORT_DIR gets a localization job automatically."""

    basename = "Watched (2021) - [DVD].mkv"
    file_path = os.path.join(app.config["IMPORT_DIR"], basename)
    with open(file_path, "wb") as f:
        f.write(b"not a real video")

    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if basename in app.import_queue.job_ids:
                break
            time.sleep(0.5)
        else:
            pytest.fail("watchdog never enqueued the new file")

        job = app.import_queue.fetch_job(basename)
        assert job.args == (file_path,)
        assert_binds(job)
    finally:
        os.remove(file_path)


def test_every_scheduled_job_binds(app, held_lock, incoming_dir):
    """Catch-all for the enqueue_in bug class across every defer produced above."""

    with app.app_context():
        held_lock("catchall-lock")
        acquire_lock_or_defer(
            "catchall-lock",
            30000,
            app.file_scheduler,
            "app.videos.localization_task",
            minutes=(5, 15),
            timeout=3600,
            description="'catchall.mkv'",
            kwargs={"file_path": "/incoming/catchall.mkv"},
        )

        checked = 0
        for job, _ in app.file_scheduler.get_jobs(with_times=True):
            try:
                assert_binds(job)
            except NoSuchJobError:
                continue
            checked += 1
        assert checked >= 1
