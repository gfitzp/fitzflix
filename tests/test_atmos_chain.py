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

    def get_paginator(self, name):
        assert name == "list_object_versions"
        return _FakePaginator()


class _FakePaginator:
    def paginate(self, Bucket, Prefix):
        return [{"Versions": [], "DeleteMarkers": []}]


class _FakeMediaConvert:
    """Stand in for MediaConvert. The test sets the status of the encode."""

    def __init__(self):
        self.status = {"Status": "PROGRESSING", "JobPercentComplete": 40}
        self.fail_next_get = False

    def create_job(self, Role, Settings):
        return {"Job": {"Id": "mc-1"}}

    def cancel_job(self, Id):
        self.canceled = getattr(self, "canceled", []) + [Id]

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
        CONTINUES_KEY,
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

        # rq removes the hash of the first job after its result TTL. The
        # trail healing took the open chips for orphans and removed them
        # (Incredibles 2, 2026-10-15). The run keeps them open.

        app.redis.delete(f"rq:job:{job.id}")
        (trail,) = [
            t
            for t in pipeline_trails(app.redis, limit=100)
            if t["basename"] == file.basename
        ]
        assert [(e["stage"], e["status"]) for e in trail["entries"]][-2:] == [
            ("Encoding E-AC-3 Atmos", "started"),
            ("Adding Atmos twin", "queued"),
        ]

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
            phases.enter("Replacing the library file")
            return True

        monkeypatch.setattr(atmos, "_atmos_finish", fake_finish)
        assert atmos.atmos_finish_task(*finish.args) is True

        assert _chips(app, file.basename) == [
            ("Copying to staging", "done"),
            ("Decoding TrueHD", "done"),
            ("Encoding E-AC-3 Atmos", "done"),
            ("Remuxing", "done"),
            ("Replacing the library file", "done"),
            ("Adding Atmos twin", "done"),
        ]

        # The stored trail keeps the own chip. The painters do not show
        # it, because the phase chips tell the full story.

        (trail,) = [
            t
            for t in pipeline_trails(app.redis, limit=100)
            if t["basename"] == file.basename
        ]
        assert trail["entries"][-1]["stage"] == "Replacing the library file"
        lock = app.lock_manager.lock(file.file_identifier(), 1000)
        assert lock
        app.lock_manager.unlock(lock)
        assert not app.redis.exists(atmos.CHAIN_KEY.format(file_id=file.id))
        assert not app.redis.exists(CONTINUES_KEY.format(job_id=job.id))
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


def test_the_import_queues_the_twin_on_the_handoff_queue(app, monkeypatch):
    """Put the twin of a new import on the handoff queue, and a sweep on the import queue.

    The file of a new import is already in the pipeline. On the import
    queue, its twin waited behind each new import (High Society: 5
    hours). A library sweep stays on the import queue. A job on either
    queue stops a second job for the same file."""

    import app.atmos as atmos

    with app.app_context():
        file, job, _ = _setup(app, monkeypatch, "Chain Queue")
        assert job.origin == "fitzflix-import"
        app.import_queue.remove(job.id)

        assert (
            atmos.maybe_enqueue_atmos_supplement(file.id, queue=app.handoff_queue)
            is True
        )
        (handoff,) = [j for j in app.handoff_queue.jobs if j.args == (file.id,)]
        assert handoff.func_name == "app.atmos.atmos_supplement_task"
        assert atmos.maybe_enqueue_atmos_supplement(file.id) is False
        assert not [j for j in app.import_queue.jobs if j.args == (file.id,)]
        handoff.delete()


def test_the_scratch_cleanup_deletes_every_version(app):
    """The bucket keeps versions. A plain delete only adds a delete marker."""

    import app.atmos as atmos

    class VersionedS3:
        def __init__(self):
            self.deleted = []

        def get_paginator(self, name):
            assert name == "list_object_versions"

            class Pages:
                def paginate(self, Bucket, Prefix):
                    assert Prefix == "mediaconvert-scratch/7/"
                    return [
                        {
                            "Versions": [
                                {"Key": f"{Prefix}x.atmos", "VersionId": "v1"},
                                {"Key": f"{Prefix}out1/twin.ec3", "VersionId": "v2"},
                            ],
                            "DeleteMarkers": [
                                {"Key": f"{Prefix}old.atmos", "VersionId": "m1"}
                            ],
                        }
                    ]

            return Pages()

        def delete_objects(self, Bucket, Delete):
            self.deleted.extend(Delete["Objects"])

    s3 = VersionedS3()
    with app.app_context():
        atmos._cleanup_s3_prefix(s3, "bucket", "mediaconvert-scratch/7")
    assert {entry["VersionId"] for entry in s3.deleted} == {"v1", "v2", "m1"}


def test_a_failed_encode_cancels_the_other_encodes(app, monkeypatch):
    """The other encodes of a failed run must not continue to bill."""

    import app.atmos as atmos

    class TwoJobs:
        canceled = []

        def get_job(self, Id):
            status = "ERROR" if Id == "mc-1" else "PROGRESSING"
            return {"Job": {"Id": Id, "Status": status}}

        def cancel_job(self, Id):
            self.canceled.append(Id)

    mediaconvert = TwoJobs()
    monkeypatch.setattr(atmos, "mediaconvert_client", lambda: mediaconvert)
    monkeypatch.setattr(atmos, "_close_run", lambda state, lock: None)
    monkeypatch.setattr(atmos, "_trail_phases", lambda state, current: _NoPhases())
    state = {
        "basename": "Two Tracks (2001) - [Bluray-1080p].mkv",
        "tracks": [{"mc_job_id": "mc-1"}, {"mc_job_id": "mc-2"}],
        "file_id": 1,
        "chain": "c",
    }
    try:
        atmos.atmos_check_task(state, None)
    except RuntimeError:
        pass
    assert mediaconvert.canceled == ["mc-2"]


class _NoPhases:
    def fail(self):
        pass

    def close(self, status):
        pass
