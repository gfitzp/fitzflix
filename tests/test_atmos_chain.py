"""Test the 3-job Atmos run around the MediaConvert wait.

Before, 1 job held a worker for the whole run. Gandhi took 83 minutes,
and 58 of them were a wait on MediaConvert. Now the first job submits
the encode and ends. A short check job asks MediaConvert every 2
minutes. A finish job muxes the twin in. No worker waits, and more
than 1 film can encode at a time."""

import json
import os

from tests.factories import make_movie, make_movie_file

ATMOS = "Dolby TrueHD with Dolby Atmos"


class _FakeS3:
    """Stand in for the S3 client: uploads, the .ec3 listing, downloads."""

    def upload_file(self, path, bucket, key, Callback=None):
        if Callback:
            Callback(os.path.getsize(path))

    def list_objects_v2(self, Bucket, Prefix):
        return {"Contents": [{"Key": f"{Prefix}twin.ec3"}]}

    def download_file(self, bucket, key, path):
        with open(path, "wb") as f:
            f.write(b"ec3")

    def delete_objects(self, Bucket, Delete):
        pass


class _FakeMediaConvert:
    """Stand in for MediaConvert. The test sets the status of the encode."""

    def __init__(self):
        self.status = {"Status": "PROGRESSING", "JobPercentComplete": 40}
        self.fail_next_get = False

    def create_job(self, Role, Settings):
        return {"Job": {"Id": "mc-1"}}

    def get_job(self, Id):
        if self.fail_next_get:
            self.fail_next_get = False
            raise OSError("network down")
        return {"Job": dict(self.status, Id=Id)}


def _fake_run_step(command, job, basename, description, ok_returncodes=(0,)):
    """Make the files that mkvextract and truehdd would make."""

    if "Extracting" in description:
        path = command[-1].split(":", 1)[1]
        open(path, "wb").close()
    else:
        base = command[command.index("--output-path") + 1]
        for suffix in (".atmos", ".atmos.audio", ".atmos.metadata"):
            with open(base + suffix, "wb") as f:
                f.write(b"damf")


def _setup(app, monkeypatch, title):
    """Make an Atmos file in the library and fake the tools and AWS."""

    import rq

    import app.atmos as atmos
    import app.videos as videos
    from app import db
    from app.models import FileAudioTrack

    mediaconvert = _FakeMediaConvert()
    monkeypatch.setattr(atmos, "WORKSPACE_HEADROOM_BYTES", 0)
    monkeypatch.setattr(atmos, "_run_step", _fake_run_step)
    monkeypatch.setattr(atmos, "mediaconvert_client", lambda: mediaconvert)
    monkeypatch.setattr(videos, "aws_s3_client", lambda *a, **k: _FakeS3())
    monkeypatch.setattr(
        videos,
        "get_audio_tracks_from_file",
        lambda path: [{"codec": ATMOS, "language": "eng", "streamorder": 1}],
    )

    movie = make_movie(title, 2021)
    file = make_movie_file(movie, "Bluray-1080p")
    db.session.add(
        FileAudioTrack(
            file_id=file.id,
            track=1,
            codec=ATMOS,
            language="eng",
            language_name="English",
        )
    )
    db.session.commit()
    library = os.path.join(app.config["LIBRARY_DIR"], file.file_path)
    os.makedirs(os.path.dirname(library), exist_ok=True)
    with open(library, "wb") as f:
        f.write(b"x" * 4096)

    assert atmos.maybe_enqueue_atmos_supplement(file.id) is True
    job = next(j for j in app.import_queue.jobs if j.args == (file.id,))
    monkeypatch.setattr(atmos, "get_current_job", lambda: job)
    monkeypatch.setattr(rq, "get_current_job", lambda: job)
    return file, job, mediaconvert


def _chips(app, basename):
    from app.pipeline import FILE_KEY, _digest

    trail = json.loads(
        app.redis.hget(FILE_KEY.format(digest=_digest(basename)), "trail")
    )
    return [(entry["stage"], entry["status"]) for entry in trail]


def _scheduled(app, func):
    from rq.registry import ScheduledJobRegistry

    registry = ScheduledJobRegistry(queue=app.handoff_queue)
    jobs = [app.handoff_queue.fetch_job(job_id) for job_id in registry.get_job_ids()]
    return [job for job in jobs if job and job.func_name == func]


def test_the_run_hands_off_around_the_mediaconvert_wait(app, admin_client, monkeypatch):
    """Submit, check, and finish in 3 jobs, on 1 row of chips."""

    import app.atmos as atmos
    from app.pipeline import (
        pipeline_trails,
        queue_rows,
        record_job_event,
        trail_continues,
    )

    with app.app_context():
        file, job, mediaconvert = _setup(app, monkeypatch, "Chain Run")
        record_job_event(app.redis, job, "started")
        assert atmos.atmos_supplement_task(file.id) is True

        # The first job ended. The worker must leave its chip open. The
        # title lock stays with the run. The encode chip runs.

        assert trail_continues(job)
        assert not app.lock_manager.lock(file.file_identifier(), 1000)
        assert app.redis.exists(atmos.CHAIN_KEY.format(file_id=file.id))
        assert _chips(app, file.basename) == [
            ("Copying to staging", "done"),
            ("Decoding TrueHD", "done"),
            ("Encoding E-AC-3 Atmos", "started"),
            ("Adding Atmos twin", "queued"),
        ]
        (check,) = _scheduled(app, "app.atmos.atmos_check_task")
        assert check.kwargs == {"checks": 0}
        assert check.meta["trail_job"] == job.id

        # The queue page lists the booked check on the row of the run.

        payload = admin_client.get("/api/queue-details").get_json()
        (row,) = [task for task in payload["all"] if task["id"] == check.id]
        assert row["scheduled_word"] == "checks again"
        rows = queue_rows(payload["all"], pipeline_trails(app.redis, limit=100))
        (match,) = [r for r in rows if r["task"] and r["task"]["id"] == check.id]
        assert match["trail"]["basename"] == file.basename

        # MediaConvert still encodes. The check books the next check.

        assert atmos.atmos_check_task(*check.args, **check.kwargs) is True
        (second,) = [
            j for j in _scheduled(app, "app.atmos.atmos_check_task") if j.id != check.id
        ]
        assert second.kwargs == {"checks": 1}
        assert "(40%)" in second.description

        # A network error is not a failed encode. It books the next check.

        mediaconvert.fail_next_get = True
        assert atmos.atmos_check_task(*second.args, **second.kwargs) is True
        assert len(_scheduled(app, "app.atmos.atmos_check_task")) == 3

        # The encode is complete. The check queues the finish job.

        mediaconvert.status = {"Status": "COMPLETE"}
        assert atmos.atmos_check_task(*second.args, **second.kwargs) is True
        (finish,) = [
            j
            for j in app.handoff_queue.jobs
            if j.func_name == "app.atmos.atmos_finish_task"
        ]
        assert finish.meta["trail_job"] == job.id

        def fake_finish(state, phases):
            phases.enter("Remuxing")
            phases.enter("Moving into the library")
            return True

        monkeypatch.setattr(atmos, "_atmos_finish", fake_finish)
        assert atmos.atmos_finish_task(*finish.args) is True

        assert _chips(app, file.basename) == [
            ("Copying to staging", "done"),
            ("Decoding TrueHD", "done"),
            ("Encoding E-AC-3 Atmos", "done"),
            ("Remuxing", "done"),
            ("Moving into the library", "done"),
            ("Adding Atmos twin", "done"),
        ]
        lock = app.lock_manager.lock(file.file_identifier(), 1000)
        assert lock
        app.lock_manager.unlock(lock)
        assert not app.redis.exists(atmos.CHAIN_KEY.format(file_id=file.id))
        assert not os.path.exists(finish.args[0]["workspace"])


def test_a_failed_encode_ends_the_run_and_releases_everything(app, monkeypatch):
    """Turn the encode chip red, release the lock, and clean up."""

    import pytest

    import app.atmos as atmos
    from app.pipeline import record_job_event

    with app.app_context():
        file, job, mediaconvert = _setup(app, monkeypatch, "Chain Fail")
        record_job_event(app.redis, job, "started")
        assert atmos.atmos_supplement_task(file.id) is True
        (check,) = _scheduled(app, "app.atmos.atmos_check_task")

        mediaconvert.status = {"Status": "ERROR", "ErrorCode": 1030}
        with pytest.raises(RuntimeError, match="ERROR"):
            atmos.atmos_check_task(*check.args, **check.kwargs)

        assert _chips(app, file.basename)[-2:] == [
            ("Encoding E-AC-3 Atmos", "failed"),
            ("Adding Atmos twin", "failed"),
        ]
        lock = app.lock_manager.lock(file.file_identifier(), 1000)
        assert lock
        app.lock_manager.unlock(lock)
        assert not app.redis.exists(atmos.CHAIN_KEY.format(file_id=file.id))
        assert not os.path.exists(check.args[0]["workspace"])


def test_a_run_between_its_jobs_is_not_queued_again(app, monkeypatch):
    """Do not start a second run for a file while its encode is out."""

    import app.atmos as atmos

    with app.app_context():
        file, job, _ = _setup(app, monkeypatch, "Chain Once")
        assert atmos.atmos_supplement_task(file.id) is True
        app.import_queue.remove(job.id)
        assert atmos.maybe_enqueue_atmos_supplement(file.id) is False
