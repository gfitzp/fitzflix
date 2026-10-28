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


def test_a_dead_localization_releases_its_lock_and_its_work_files(app, tmp_path):
    from app.pipeline import PipelineWorker
    from app.worker_recovery import note_job_state, reclaim_dead_workers

    source = tmp_path / "Dead Import (2002) - [DVD].mkv"
    source.write_bytes(b"source")
    staged = tmp_path / "staged copy.mkv"
    staged.write_bytes(b"staged")
    with app.app_context():
        lock = app.lock_manager.lock('{"title": "Dead Import"}', 600000)
        _, job = _plant_dead_worker(
            app,
            app.import_queue,
            "app.videos.localization_task",
            (str(source),),
        )
        note_job_state(job.id, 600, lock=lock, strays=[str(staged)])
        reclaim_dead_workers(app.redis, PipelineWorker)
        assert app.redis.get('{"title": "Dead Import"}') is None
        assert source.exists() and not staged.exists()


def test_the_localization_notes_its_lock_when_it_takes_it(app, monkeypatch):
    """The take and the note are 1 call (#498 follow-up)."""

    import json
    from types import SimpleNamespace

    from app import importing
    from app.worker_recovery import JOB_LOCK_KEY

    with app.app_context():
        run = importing._LocalizationRun("/import/Noted (2001) - [DVD].mkv", {})
        run.job = SimpleNamespace(id="noted-job")
        lock = importing._take_title_lock(
            {"media_library": "Movies", "title": "Noted", "year": 2001}, run
        )
        note = json.loads(app.redis.get(JOB_LOCK_KEY.format(job_id=run.job.id)))
        assert note["lock"][0] == lock.resource
        run.strays.append("/staging/Noted.mkv")
        note = json.loads(app.redis.get(JOB_LOCK_KEY.format(job_id=run.job.id)))
        assert note["strays"] == ["/staging/Noted.mkv"]
        app.lock_manager.unlock(lock)


def _atmos_state(tmp_path, chain="c1"):
    workspace = tmp_path / "atmos-77"
    workspace.mkdir(exist_ok=True)
    return {
        "file_id": 77,
        "basename": "Dead Twin.mkv",
        "workspace": str(workspace),
        "chain": chain,
        "trail_job": None,
        "tracks": [],
    }


def test_a_dead_atmos_finish_releases_its_lock_and_chain(app, tmp_path):
    from app.atmos import CHAIN_KEY
    from app.pipeline import PipelineWorker
    from app.worker_recovery import reclaim_dead_workers

    with app.app_context():
        lock = app.lock_manager.lock('{"title": "Dead Twin"}', 600000)
        state = _atmos_state(tmp_path)
        app.redis.set(CHAIN_KEY.format(file_id=77), "c1")
        _plant_dead_worker(
            app, app.handoff_queue, "app.atmos.atmos_finish_task", (state, lock)
        )
        reclaim_dead_workers(app.redis, PipelineWorker)
        assert app.redis.get('{"title": "Dead Twin"}') is None
        assert not app.redis.exists(CHAIN_KEY.format(file_id=77))
        assert (tmp_path / "atmos-77").exists()


def test_a_dead_atmos_check_with_a_booked_next_check_releases_nothing(app, tmp_path):
    from datetime import timedelta

    from app.atmos import CHAIN_KEY
    from app.pipeline import PipelineWorker
    from app.worker_recovery import reclaim_dead_workers

    with app.app_context():
        lock = app.lock_manager.lock('{"title": "Dead Twin"}', 600000)
        state = _atmos_state(tmp_path)
        app.redis.set(CHAIN_KEY.format(file_id=77), "c1")
        app.handoff_queue.enqueue_in(
            timedelta(minutes=2), "app.atmos.atmos_check_task", state, lock
        )
        _plant_dead_worker(
            app, app.handoff_queue, "app.atmos.atmos_check_task", (state, lock)
        )
        reclaim_dead_workers(app.redis, PipelineWorker)
        assert app.redis.get('{"title": "Dead Twin"}') is not None
        assert app.redis.exists(CHAIN_KEY.format(file_id=77))
        app.lock_manager.unlock(lock)


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
