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


def _repair_booking(app, path):
    """Book the record repair of path and return the job under its id (#700)."""

    from app.importing import _book_replaced_file_repair

    with app.app_context():
        _book_replaced_file_repair(path, path, 1)
    return _repair_queue(app).fetch_job(_repair_job_id(path))


def _repair_job_id(path):
    """Return the id of try 1 of the record repair of path."""

    from app.tasks import requeue_job_id

    return requeue_job_id("app.importing.repair_replaced_file_task", path, {"tries": 1})


def _repair_queue(app):
    """Return the queue of the record repair task."""

    from app.tasks import TASKS, resolve_queue

    with app.app_context():
        return resolve_queue(TASKS["app.importing.repair_replaced_file_task"].queue)


def _delete_job_id(app, queue, job_id):
    """Delete the job under job_id and its registry entries, if it is there."""

    from rq.exceptions import NoSuchJobError
    from rq.job import Job
    from rq.registry import FailedJobRegistry, ScheduledJobRegistry

    from app.tasks import delete_job

    ScheduledJobRegistry(queue=queue).remove(job_id)
    FailedJobRegistry(queue=queue).remove(job_id)
    try:
        delete_job(Job.fetch(job_id, connection=queue.connection))
    except NoSuchJobError:
        pass


def test_a_retry_does_not_take_the_expiry_of_a_finished_job(app):
    """A new chain does not take the expiry of a finished job with its id (#700).

    rq writes a new job over the old hash with HSET. Redis then keeps
    the expiry of the old hash. The hash can go before the booked time.
    The scheduler then drops the booking with no log line."""

    from rq.registry import ScheduledJobRegistry

    path = "/nonexistent/Finished Repair (2021) - [DVD].mkv"
    queue = _repair_queue(app)
    job_id = _repair_job_id(path)
    try:
        old = _repair_booking(app, path)
        ScheduledJobRegistry(queue=queue).remove(job_id)
        old.set_status("finished")
        app.redis.expire(old.key, 120)

        _repair_booking(app, path)
        assert app.redis.ttl(old.key) == -1
        assert job_id in ScheduledJobRegistry(queue=queue).get_job_ids()
    finally:
        _delete_job_id(app, queue, job_id)


def test_a_retry_takes_the_id_of_a_failed_job_out_of_the_failed_registry(app):
    """A new chain under the id of a failed job is not a failure (#700)."""

    from rq.registry import FailedJobRegistry, ScheduledJobRegistry

    path = "/nonexistent/Failed Repair (2021) - [DVD].mkv"
    queue = _repair_queue(app)
    job_id = _repair_job_id(path)
    try:
        old = _repair_booking(app, path)
        ScheduledJobRegistry(queue=queue).remove(job_id)
        old.set_status("failed")
        FailedJobRegistry(queue=queue).add(old, ttl=600)
        app.redis.expire(old.key, 120)

        _repair_booking(app, path)
        assert app.redis.ttl(old.key) == -1
        assert job_id not in FailedJobRegistry(queue=queue).get_job_ids()
        assert job_id in ScheduledJobRegistry(queue=queue).get_job_ids()
    finally:
        _delete_job_id(app, queue, job_id)


def test_a_running_job_that_books_itself_under_its_own_id_keeps_the_booking(app):
    """The S3 sync books itself again under its own id while it runs (#700).

    The running job is live. requeue must not delete it, and the new
    booking must go to the ScheduledJobRegistry."""

    from rq.registry import ScheduledJobRegistry

    from app.tasks import TASKS, requeue, requeue_job_id, resolve_queue

    name = "app.videos.sync_aws_s3_storage_task"
    with app.app_context():
        queue = resolve_queue(TASKS[name].queue)
        job_id = requeue_job_id(name)
        try:
            requeue(name, key=None, delay=timedelta(minutes=5), reason="test")
            ScheduledJobRegistry(queue=queue).remove(job_id)
            running = queue.fetch_job(job_id)
            running.set_status("started")

            requeue(name, key=None, delay=timedelta(minutes=5), reason="test")
            assert job_id in ScheduledJobRegistry(queue=queue).get_job_ids()
            assert queue.fetch_job(job_id).get_status() == "scheduled"
        finally:
            _delete_job_id(app, queue, job_id)


def test_a_job_that_books_itself_keeps_the_booking_after_it_ends(app):
    """The end of a job must not take away the retry that it booked under its own id.

    The Scan button of a file page queues the scan under the id of its
    lock-busy retry, with the default result ttl of rq. The scan finds
    the lock busy and books itself again under that id, 15 minutes from
    now. Then the job ends: rq writes "finished" over the hash of the
    new booking, with the result ttl of the job that ends. The booking
    must have a hash when it is due, and its id is not free."""

    from rq.job import _job_stack
    from rq.registry import FinishedJobRegistry, ScheduledJobRegistry

    from app.tasks import (
        TASKS,
        enqueue_task,
        free_job_id,
        requeue,
        requeue_job_id,
        resolve_queue,
    )

    name = "app.videos.track_metadata_scan_task"
    with app.app_context():
        queue = resolve_queue(TASKS[name].queue)
        job_id = requeue_job_id(name, 77)
        try:
            running = enqueue_task(name, 77, job_id=job_id)
            queue.remove(job_id)
            running.set_status("started")
            _job_stack.push(running)
            try:
                requeue(
                    name,
                    key=77,
                    delay=timedelta(minutes=15),
                    reason="Lock exists",
                    kwargs={"file_id": 77},
                )
            finally:
                _job_stack.pop()

            # The worker ends the job as rq does in handle_job_success.

            result_ttl = running.get_result_ttl(500)
            with queue.connection.pipeline() as pipeline:
                running._handle_success(result_ttl, pipeline=pipeline)
                running.cleanup(result_ttl, pipeline=pipeline, remove_from_queue=False)
                pipeline.execute()

            assert queue.connection.ttl(running.key) > 15 * 60
            assert queue.fetch_job(job_id).get_status() == "finished"
            assert free_job_id(name, job_id) is False
            assert job_id in ScheduledJobRegistry(queue=queue).get_job_ids()
            assert queue.fetch_job(job_id) is not None
        finally:
            FinishedJobRegistry(queue=queue).remove(job_id)
            _delete_job_id(app, queue, job_id)


def test_a_fixed_job_id_does_not_take_the_expiry_of_a_finished_job(app):
    """Each enqueue under a fixed id frees the id first (#744).

    A finished job keeps its hash for 500 seconds. A new job under the
    same id took that hash with its expiry. If no worker took the job in
    that time, rq dropped it with no message."""

    from app.tasks import enqueue_task

    name = "app.plex_library.analyze_plex_media"
    job_id = "plex_analyze_fixed-id-test"
    queue = app.maintenance_queue
    with app.app_context():
        try:
            old = enqueue_task(name, ["/x.mkv"], job_id=job_id)
            queue.remove(old)
            old.set_status("finished")
            app.redis.expire(old.key, 120)

            new = enqueue_task(name, ["/y.mkv"], job_id=job_id)
            assert new.id == job_id
            assert app.redis.ttl(new.key) == -1
            assert queue.fetch_job(job_id).args == (["/y.mkv"],)
        finally:
            _delete_job_id(app, queue, job_id)


def test_an_analyze_beside_a_running_one_gets_its_own_job_id(app, monkeypatch):
    """A job that runs marks its hash as finished at its end (#744).

    A second analyze under the same id would lose its hash then. It is
    not a duplicate: the first job can have read the file before the
    second edit."""

    from app.plex_library import enqueue_plex_analyze

    monkeypatch.setitem(app.config, "PLEX_URL", "http://plex.test")
    monkeypatch.setitem(app.config, "PLEX_TOKEN", "token")
    queue = app.maintenance_queue
    path = "/library/Movies/Twice (2021)/Twice (2021) - [DVD].mkv"
    with app.app_context():
        assert enqueue_plex_analyze(path) is True
        (first,) = queue.jobs
        assert enqueue_plex_analyze(path) is False

        queue.remove(first)
        first.set_status("started")
        assert enqueue_plex_analyze(path) is True
        (second,) = queue.jobs
        assert second.id != first.id
        assert second.id.startswith(first.id)
        for job in (first, second):
            _delete_job_id(app, queue, job.id)
