"""Test the 1 retry path of the tasks: requeue in app/tasks.py.

Before requeue, each task wrote its own enqueue_in block and listed its
counters by hand. The move dropped 2 of its 4 counters (#351), and the
space wait of the move ignored a lost title lock (#353).
"""

import ast
import os
import pathlib

from datetime import timedelta

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _move_retries(app):
    from tests.test_scheduling import scheduled_jobs

    return [
        job
        for job in scheduled_jobs(app.handoff_queue)
        if job.func_name == "app.videos.move_localized_file"
    ]


def _clear_move_retries(app):
    from rq.registry import ScheduledJobRegistry

    registry = ScheduledJobRegistry(queue=app.handoff_queue)
    for job in _move_retries(app):
        registry.remove(job, delete_job=True)


def test_a_dead_volume_move_defer_keeps_every_counter(app, tmp_path, monkeypatch):
    """The move twin of test_a_dead_volume_defer_keeps_every_counter (#351).

    A move that waited 2 times for the title lock and 2 times for space
    meets a dead volume. Its retry keeps all 4 counters. Before the fix,
    the retry lost lock_waits and space_waits. Then a later lock wait
    reused the id of a finished job."""

    from app import videos
    from tests.test_localization import move_retry

    monkeypatch.setattr(videos, "_dead_volumes", lambda paths: ["/Volumes/Movies"])
    source = "/nonexistent/Dead Move Counters (2021) - [DVD].mkv"
    details = {
        "basename": "Dead Move Counters (2021) - [DVD].mkv",
        "dirname": "Movies/Dead Move Counters (2021)",
    }
    counters = {
        "transient_retries": 1,
        "space_waits": 2,
        "vanished_retries": 1,
        "lock_waits": 2,
    }
    try:
        with app.app_context():
            assert (
                videos.move_localized_file(
                    source, details, None, str(tmp_path / ".hidden.mkv"), **counters
                )
                is False
            )
        (job,) = _move_retries(app)
        assert job.kwargs == counters
        assert job.id == move_retry(source, **counters)
    finally:
        _clear_move_retries(app)


def test_a_space_wait_that_lost_the_title_lock_waits_for_the_lock(
    app, incoming_dir, monkeypatch
):
    """A space wait without the title lock defers as a lock wait (#353).

    The first hold succeeds. The hold inside the space wait fails,
    because another task took the lock. The move must not book a space
    wait under a lock that it does not hold."""

    from app import importing, videos
    from tests.test_localization import move_retry, space_move_fixture

    basename = "Lost Lock Space (2021) - [DVD].mkv"
    source, hidden, details = space_move_fixture(app, incoming_dir, basename)
    holds = []

    def hold(lock, name):
        holds.append(name)
        return len(holds) == 1

    monkeypatch.setattr(importing, "_hold_title_lock", hold)
    monkeypatch.setattr(importing, "_crosses_volumes", lambda *args: True)
    monkeypatch.setattr(importing, "_free_bytes", lambda path: 0)
    try:
        with app.app_context():
            assert (
                videos.move_localized_file(source, details, "lock-sentinel", hidden)
                is False
            )
        assert len(holds) == 2
        (job,) = _move_retries(app)
        assert job.kwargs["lock_waits"] == 1
        assert job.kwargs["space_waits"] == 0
        assert job.id == move_retry(source, lock_waits=1)
    finally:
        _clear_move_retries(app)
        os.remove(source)
        os.remove(hidden)


def test_requeue_merges_the_counters_and_takes_the_spec(app):
    """Test the counters, the id, the queue and the timeout of a retry."""

    from rq.registry import ScheduledJobRegistry

    from app.tasks import requeue, requeue_job_id

    with app.app_context():
        delay = requeue(
            "app.videos.download_task",
            key="untouched/Requeue Test.mkv",
            delay=(5, 15),
            reason="test",
            counters={"transient_retries": 2},
            args=("untouched/Requeue Test.mkv", "Requeue Test.mkv"),
            kwargs={"sqs_receipt_handle": "handle"},
            description="'Requeue Test.mkv'",
        )
        assert timedelta(minutes=5) <= delay <= timedelta(minutes=15)
        job_id = requeue_job_id(
            "app.videos.download_task",
            "untouched/Requeue Test.mkv",
            {"transient_retries": 2},
        )
        registry = ScheduledJobRegistry(queue=app.file_queue)
        try:
            assert job_id in registry.get_job_ids()
            job = app.file_queue.fetch_job(job_id)
            assert job.kwargs == {
                "sqs_receipt_handle": "handle",
                "transient_retries": 2,
            }
            assert job.timeout == app.config["TRANSCODE_TASK_TIMEOUT"]
            assert job.result_ttl == 86400
        finally:
            registry.remove(job_id, delete_job=True)

    # The counters go into the id in name order. A retry with no key and
    # no counters has a fixed id.

    with app.app_context():
        assert requeue_job_id("app.videos.upload_task", 7, {"b": 1, "a": 2}).endswith(
            "_a_2_b_1"
        )
        assert (
            requeue_job_id("app.videos.sync_aws_s3_storage_task")
            == "retry_sync_aws_s3_storage_task"
        )


def test_localization_pending_sees_a_requeued_retry(app, incoming_dir):
    """A sweep must not queue a file again next to its requeued retry."""

    from rq.registry import ScheduledJobRegistry

    from app.importing import localization_pending
    from app.tasks import requeue, requeue_job_id

    path = os.path.join(incoming_dir, "Pending Requeue (2021) - [DVD].mkv")
    counters = {
        "transient_retries": 0,
        "completeness_retries": 1,
        "space_waits": 0,
        "tmdb_retries": 0,
    }
    with app.app_context():
        assert not localization_pending(path)
        requeue(
            "app.videos.localization_task",
            key=path,
            delay=timedelta(minutes=1),
            reason="test",
            counters=counters,
            kwargs={"file_path": path},
        )
        try:
            assert localization_pending(path)

            # The retry of another file with the same basename is not
            # this file.

            other = os.path.join(incoming_dir, "sub", os.path.basename(path))
            assert not localization_pending(other)
        finally:
            ScheduledJobRegistry(queue=app.import_queue).remove(
                requeue_job_id("app.videos.localization_task", path, counters),
                delete_job=True,
            )


def test_no_producer_enqueues_a_task_name_by_hand():
    """Test that each enqueue of a task name goes through app/tasks.py.

    A raw queue.enqueue or enqueue_in picks its own queue and timeout.
    Those per-site choices are what the registry replaced."""

    raw = []
    for path in sorted((ROOT / "app").rglob("*.py")):
        if path.name == "tasks.py":
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("enqueue", "enqueue_in", "enqueue_at")
            ):
                raw.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not raw, raw
