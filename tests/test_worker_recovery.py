"""Test the reclaim of the job of a dead worker at the start of a worker (#498)."""

import os
import socket
import subprocess


def _dead_pid():
    """Return the pid of a process that ended."""

    process = subprocess.Popen(["true"])
    process.wait()
    return process.pid


def _plant_dead_worker(app, queue, func, args):
    """Make a worker hash with a dead pid that runs a started job of func."""

    from rq.executions import Execution
    from rq.job import JobStatus

    from app.pipeline import PipelineWorker

    worker = PipelineWorker([queue], connection=app.redis)
    worker.register_birth()
    app.redis.hset(
        worker.key, mapping={"pid": _dead_pid(), "hostname": socket.gethostname()}
    )
    job = queue.enqueue(func, *args, job_timeout=600)
    queue.remove(job)
    with app.redis.pipeline() as pipeline:
        job.prepare_for_execution(worker.name, pipeline)
        Execution.create(job, ttl=600, pipeline=pipeline)
        worker.set_current_job_id(job.id, pipeline=pipeline)
        pipeline.execute()
    assert job.get_status(refresh=True) == JobStatus.STARTED
    return worker, job


def test_a_dead_conversion_is_failed_and_its_lock_and_reservation_go(app, tmp_path):
    from rq.job import JobStatus
    from rq.registry import FailedJobRegistry, StartedJobRegistry

    from app.importing import STAGING_RESERVED_KEY
    from app.pipeline import PipelineWorker
    from app.worker_recovery import reclaim_dead_workers

    source = tmp_path / "Dead Worker (2001) - [Remux-2160p].mkv"
    source.write_bytes(b"source")
    output = tmp_path / ".Dead Worker (2001) - [Bluray-1080p].mkv"
    output.write_bytes(b"localized")
    with app.app_context():
        lock = app.lock_manager.lock('{"title": "Dead Worker"}', 600000)
        app.redis.hset(STAGING_RESERVED_KEY, source.name, "{}")
        worker, job = _plant_dead_worker(
            app,
            app.transcode_queue,
            "app.videos.convert_localized_file",
            (
                str(source),
                {
                    "basename": output.name.lstrip("."),
                    "untouched_basename": source.name,
                },
                lock,
                str(output),
                {},
            ),
        )

        assert reclaim_dead_workers(app.redis, PipelineWorker) == [worker.name]

        assert job.get_status(refresh=True) == JobStatus.FAILED
        assert job.id in FailedJobRegistry(queue=app.transcode_queue).get_job_ids()
        assert job.id not in StartedJobRegistry(queue=app.transcode_queue).get_job_ids()
        assert app.redis.get('{"title": "Dead Worker"}') is None
        assert not app.redis.hexists(STAGING_RESERVED_KEY, source.name)
        assert app.redis.hexists(worker.key, "death")
        assert source.exists() and not output.exists()

        # A second start finds nothing more to reclaim.
        assert reclaim_dead_workers(app.redis, PipelineWorker) == []


def test_a_dead_localization_releases_the_lock_from_its_meta(app, tmp_path):
    from app.pipeline import PipelineWorker
    from app.worker_recovery import note_job_lock, reclaim_dead_workers

    source = tmp_path / "Dead Import (2002) - [DVD].mkv"
    source.write_bytes(b"source")
    with app.app_context():
        lock = app.lock_manager.lock('{"title": "Dead Import"}', 600000)
        _, job = _plant_dead_worker(
            app,
            app.import_queue,
            "app.videos.localization_task",
            (str(source),),
        )
        note_job_lock(job.id, lock, 600)
        reclaim_dead_workers(app.redis, PipelineWorker)
        assert app.redis.get('{"title": "Dead Import"}') is None
        assert source.exists()


def test_a_live_worker_is_not_reclaimed(app):
    from app.pipeline import PipelineWorker
    from app.worker_recovery import reclaim_dead_workers

    with app.app_context():
        worker = PipelineWorker([app.import_queue], connection=app.redis)
        worker.register_birth()
        app.redis.hset(
            worker.key, mapping={"pid": os.getppid(), "hostname": socket.gethostname()}
        )
        assert reclaim_dead_workers(app.redis, PipelineWorker) == []
        assert not app.redis.hexists(worker.key, "death")
        worker.register_death()
