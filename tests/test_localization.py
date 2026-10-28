"""Test end-to-end localization through local staging and mount resilience.

The mount-resilience behaviors are the pre-flight deferral, the
best-effort rejects, and the dead-mount detection.

The end-to-end test builds a real (tiny) Matroska file with ffmpeg and
mkvmerge. It runs localization_task and finalize_localization against
the file. Then it checks that the file arrives in the library with its
database records. All intermediate work occurs in the staging directory.
"""

import errno
import inspect
import json
import os
import time

import pytest

from tests.test_scheduling import scheduled_jobs
from app import safe_job_id

import app.videos as videos
import app.maintenance as maintenance

from app.models import File
from app.videos import finalize_localization, localization_task, move_to_rejects

from tests.localization_helpers import (
    rejected_files,
    run_full_chain,
    separate_library_volume,
    settle,
)


def loc_retry(source, **changes):
    """Return the requeue id of a localization retry with these counters."""

    from app.tasks import requeue_job_id

    counters = {
        "transient_retries": 0,
        "completeness_retries": 0,
        "space_waits": 0,
        "tmdb_retries": 0,
        **changes,
    }
    return requeue_job_id("app.videos.localization_task", source, counters)


def move_retry(source, **changes):
    """Return the requeue id of a move retry with these counters."""

    from app.tasks import requeue_job_id

    counters = {
        "transient_retries": 0,
        "space_waits": 0,
        "vanished_retries": 0,
        "lock_waits": 0,
        **changes,
    }
    return requeue_job_id("app.videos.move_localized_file", source, counters)


def requeue_job_id(*args, **kwargs):
    """Return a requeue id. The import of app.tasks waits for the test app."""

    from app.tasks import requeue_job_id as build

    return build(*args, **kwargs)


def test_localization_end_to_end_through_staging(app, sample_mkv, incoming_dir):
    basename = "Staging Test (2021) - [DVD].mkv"
    source = os.path.join(incoming_dir, basename)
    with open(sample_mkv, "rb") as f_in, open(source, "wb") as f_out:
        f_out.write(f_in.read())

    with app.app_context():
        assert localization_task(source) is True

        # The task left the processed hidden file in staging. It gave the
        # library copy to the file-operation queue, not to the sql queue

        move_jobs = [
            job
            for job in app.handoff_queue.jobs
            if job.func_name == "app.videos.move_localized_file"
        ]
        assert len(move_jobs) == 1
        move_args = move_jobs[0].args
        assert move_args[0] == source
        hidden = move_args[3]
        assert hidden.startswith(app.config["STAGING_DIR"])
        assert os.path.exists(hidden)

        # The task already removed the staged source copy after mkvmerge

        assert os.listdir(app.config["STAGING_DIR"]) == [os.path.basename(hidden)]

        # The move puts the file at a hidden destination name. It empties
        # staging. It enqueues the finalize on the sql queue

        assert videos.move_localized_file(*move_args) is True
        assert os.listdir(app.config["STAGING_DIR"]) == []

        finalize_jobs = [
            job
            for job in app.sql_queue.jobs
            if job.func_name == "app.videos.finalize_localization"
        ]
        assert len(finalize_jobs) == 1
        job_args = finalize_jobs[0].args
        assert job_args[0] == source
        destination_hidden = job_args[3]
        assert destination_hidden.startswith(app.config["LIBRARY_DIR"])
        assert os.path.exists(destination_hidden)

        # The move already inspected the file. Thus, finalize receives the
        # precomputed media details. It never opens the file itself

        assert len(job_args) == 5
        inspection = job_args[4]
        assert inspection["audio_tracks"]
        assert inspection["filesize_bytes"] > 0
        assert inspection["video"].get("format")

        finalize_localization(*job_args)

        # The finished file is in the library. Staging is empty. The source
        # is removed. The database has the record

        output = os.path.join(
            app.config["LIBRARY_DIR"],
            "Movies/Staging Test (2021)/Staging Test (2021) - [DVD].mkv",
        )
        assert os.path.exists(output)
        assert os.listdir(app.config["STAGING_DIR"]) == []
        assert not os.path.exists(source)

        file = File.query.filter_by(basename=basename).one()
        assert file.movie.title == "Staging Test"
        assert file.movie.year == 2021
        assert file.quality.quality_title == "DVD"

        # The video fields and the track rows came from the inspection dict

        assert file.format == "AVC"
        assert [t.language for t in file.audiotrack] == ["eng"]

        # The task released the title lock

        lock = app.lock_manager.lock(file.file_identifier(), 1000)
        assert lock
        app.lock_manager.unlock(lock)

    os.remove(output)


def test_non_matroska_is_converted_to_mkv(app, sample_mp4, incoming_dir):
    source = os.path.join(incoming_dir, "Converted (2021) - [DVD].mp4")
    with open(sample_mp4, "rb") as f_in, open(source, "wb") as f_out:
        f_out.write(f_in.read())

    with app.app_context():
        run_full_chain(app, source)

        # The library file and the database record both have the .mkv name

        output = os.path.join(
            app.config["LIBRARY_DIR"],
            "Movies/Converted (2021)/Converted (2021) - [DVD].mkv",
        )
        assert os.path.exists(output)
        assert not os.path.exists(source)
        assert os.listdir(app.config["STAGING_DIR"]) == []

        file = File.query.filter_by(plex_title="Converted (2021)").one()
        assert file.basename == "Converted (2021) - [DVD].mkv"
        assert file.container == "Matroska"

        # The untouched name keeps the original container

        assert file.untouched_basename == "Converted (2021) - [DVD].mp4"

    os.remove(output)


def test_unconvertible_file_imports_as_is(app, incoming_dir):
    source = os.path.join(incoming_dir, "Garbage (2021) - [DVD].dat")
    with open(source, "wb") as f:
        f.write(b"this is not a video file at all")
    settle(source)

    with app.app_context():
        run_full_chain(app, source)

        output = os.path.join(
            app.config["LIBRARY_DIR"],
            "Movies/Garbage (2021)/Garbage (2021) - [DVD].dat",
        )
        assert os.path.exists(output)
        assert not os.path.exists(source)
        assert os.listdir(app.config["STAGING_DIR"]) == []

        file = File.query.filter_by(plex_title="Garbage (2021)").one()
        assert file.basename == "Garbage (2021) - [DVD].dat"

    os.remove(output)


def test_a_dead_volume_defer_keeps_every_counter(app, incoming_dir, monkeypatch):
    """A defer must not reset the TMDb or space counters of the chain."""

    basename = "Dead Counters (2021) - [DVD].mkv"
    source = os.path.join(incoming_dir, basename)
    with open(source, "wb") as f:
        f.write(b"video")

    monkeypatch.setattr(videos, "_dead_volumes", lambda paths: ["/Volumes/Movies"])
    try:
        with app.app_context():
            assert localization_task(source, space_waits=2, tmdb_retries=3) is True

        (job,) = [
            job
            for job in scheduled_jobs(app.import_queue)
            if job.id.startswith("retry_")
        ]
        assert job.kwargs["space_waits"] == 2
        assert job.kwargs["tmdb_retries"] == 3
        assert job.id == loc_retry(source, space_waits=2, tmdb_retries=3)
    finally:
        os.remove(source)


def test_localization_defers_when_volumes_dead(app, incoming_dir, monkeypatch):
    basename = "Dead Mount (2021) - [DVD].mkv"
    source = os.path.join(incoming_dir, basename)
    with open(source, "wb") as f:
        f.write(b"video")

    monkeypatch.setattr(videos, "_dead_volumes", lambda paths: ["/Volumes/Movies"])
    try:
        with app.app_context():
            assert localization_task(source) is True

        retries = [
            job.id
            for job in scheduled_jobs(app.import_queue)
            if job.id.startswith("retry_")
        ]
        assert retries == [loc_retry(source)]

        # The task left the file untouched. It did not reject or stage it
        assert os.path.exists(source)
        assert os.listdir(app.config["STAGING_DIR"]) == []
    finally:
        os.remove(source)


def test_staging_copy_transient_error_defers_and_retries(
    app, incoming_dir, monkeypatch
):
    """Defer and retry when smbfs revokes a handle during the copy.

    The EBADF error is a mount problem, not a bad file. The task removes
    the partial staged copy. It releases the title lock. It reschedules
    itself. It does not reject a healthy source."""

    basename = "Flaky (2021) - [DVD].mkv"
    source = os.path.join(incoming_dir, basename)
    with open(source, "wb") as f:
        f.write(b"video bytes")
    settle(source)

    def revoked_handles(src, dst, job, name, activity="Moving into the library"):
        with open(dst, "wb") as f:
            f.write(b"partial")
        raise OSError(errno.EBADF, "Bad file descriptor")

    monkeypatch.setattr(videos, "copy_with_progress", revoked_handles)
    try:
        with app.app_context():
            assert localization_task(source) is True

        retries = [
            job
            for job in scheduled_jobs(app.import_queue)
            if job.id.startswith("retry_")
        ]
        assert [job.id for job in retries] == [loc_retry(source, transient_retries=1)]

        # The retry carries the increased attempt count and the original
        # flags. It binds to the signature of the task

        job = retries[0]
        assert job.kwargs["transient_retries"] == 1
        assert job.kwargs["file_path"] == source
        inspect.signature(localization_task).bind(
            *(job.args or ()), **(job.kwargs or {})
        )

        # The partial staged copy is removed. The source is untouched.
        # Nothing is rejected

        assert os.listdir(app.config["STAGING_DIR"]) == []
        assert os.path.exists(source)
        assert basename not in rejected_files(app)

        # The task released the title lock. Thus, the retry does not spin
        # on it

        identifier = json.dumps(
            {
                "title": "Flaky",
                "year": 2021,
                "feature_type": None,
                "plex_title": "Flaky (2021)",
                "edition": None,
            }
        )
        lock = app.lock_manager.lock(identifier, 1000)
        assert lock, "title lock was not released by the deferred task"
        app.lock_manager.unlock(lock)
    finally:
        os.remove(source)


def test_staging_copy_retries_advance_toward_the_budget(app, incoming_dir, monkeypatch):
    """Schedule each next attempt under its own job id.

    rq rewrites the stored payload of a job when the job returns. A
    retry used to be scheduled under the id of the job that scheduled
    it. Thus, its attempt count was reset the moment that job finished.
    The same attempt repeated forever. The budget never ran out.
    """

    basename = "Stuck (2021) - [DVD].mkv"
    source = os.path.join(incoming_dir, basename)
    with open(source, "wb") as f:
        f.write(b"video bytes")
    settle(source)

    def revoked_handles(src, dst, job, name, activity="Moving into the library"):
        raise OSError(errno.EBADF, "Bad file descriptor")

    monkeypatch.setattr(videos, "copy_with_progress", revoked_handles)
    try:
        with app.app_context():
            for attempt in range(videos.MAX_TRANSIENT_RETRIES):
                assert localization_task(source, transient_retries=attempt) is True
                scheduled = {
                    job.id: job.kwargs["transient_retries"]
                    for job in scheduled_jobs(app.import_queue)
                    if job.id.startswith("retry_")
                }
                assert scheduled == {
                    loc_retry(source, transient_retries=attempt + 1): attempt + 1
                }, f"attempt {attempt} did not schedule attempt {attempt + 1}"

                from rq.registry import ScheduledJobRegistry

                registry = ScheduledJobRegistry(queue=app.import_queue)
                for job_id in registry.get_job_ids():
                    registry.remove(job_id, delete_job=True)

            # The attempt after the last one gives up. It does not defer

            localization_task(source, transient_retries=videos.MAX_TRANSIENT_RETRIES)
            assert not any(
                job.id.startswith("retry_") for job in scheduled_jobs(app.import_queue)
            )
            assert basename in rejected_files(app)
            os.remove(os.path.join(app.config["REJECTS_DIR"], "exception", basename))
    finally:
        if os.path.exists(source):
            os.remove(source)


def test_staging_copy_transient_error_rejects_after_max_retries(
    app, incoming_dir, monkeypatch
):
    """Reject a copy that still fails after the retry budget is spent.

    The copy takes the normal reject path. It does not defer forever."""

    basename = "Hopeless (2021) - [DVD].mkv"
    source = os.path.join(incoming_dir, basename)
    with open(source, "wb") as f:
        f.write(b"video bytes")
    settle(source)

    def revoked_handles(src, dst, job, name, activity="Moving into the library"):
        raise OSError(errno.EBADF, "Bad file descriptor")

    monkeypatch.setattr(videos, "copy_with_progress", revoked_handles)

    with app.app_context():
        localization_task(source, transient_retries=videos.MAX_TRANSIENT_RETRIES)

    assert not any(
        job.id.startswith("retry_") for job in scheduled_jobs(app.import_queue)
    )
    assert basename in rejected_files(app)
    os.remove(os.path.join(app.config["REJECTS_DIR"], "exception", basename))


def test_staging_copy_permanent_error_rejects_immediately(
    app, incoming_dir, monkeypatch
):
    """Make sure a permanent error (for example, disk full) uses no retries."""

    basename = "Truly Broken (2021) - [DVD].mkv"
    source = os.path.join(incoming_dir, basename)
    with open(source, "wb") as f:
        f.write(b"video bytes")
    settle(source)

    def no_space(src, dst, job, name, activity="Moving into the library"):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(videos, "copy_with_progress", no_space)

    with app.app_context():
        localization_task(source)

    assert not any(
        job.id.startswith("retry_") for job in scheduled_jobs(app.import_queue)
    )
    assert basename in rejected_files(app)
    os.remove(os.path.join(app.config["REJECTS_DIR"], "exception", basename))


def test_library_copy_transient_error_defers_and_keeps_lock(
    app, incoming_dir, monkeypatch
):
    """Retry only the copy after a transient mount error during the library copy.

    The localized output stays in staging. The task keeps the title lock.
    It rejects nothing. There is no need to redo the whole import."""

    basename = "Flaky Move (2021) - [DVD].mkv"
    source = os.path.join(incoming_dir, basename)
    with open(source, "wb") as f:
        f.write(b"untouched source")
    hidden = os.path.join(app.config["STAGING_DIR"], f".{basename}")
    with open(hidden, "wb") as f:
        f.write(b"localized output")

    file_details = {
        "basename": basename,
        "dirname": "Movies/Flaky Move (2021)",
        "container": "Matroska",
    }

    monkeypatch.setattr(
        videos, "inspect_localized_file", lambda *args, **kwargs: {"filesize_bytes": 16}
    )

    def flaky_rename(src, dst, **kwargs):
        raise OSError(errno.ENOTCONN, "Socket is not connected")

    monkeypatch.setattr(videos, "_rename_with_retries", flaky_rename)
    try:
        with app.app_context():
            result = videos.move_localized_file(
                source, file_details, "lock-sentinel", hidden
            )
        assert result is False

        retries = [
            job
            for job in scheduled_jobs(app.handoff_queue)
            if job.id.startswith("retry_")
        ]
        assert [job.id for job in retries] == [move_retry(source, transient_retries=1)]

        # The retry carries the original chain, including the lock. It also
        # carries the increased attempt count. It binds to the signature of
        # the task

        job = retries[0]
        assert list(job.args) == [source, file_details, "lock-sentinel", hidden]
        assert job.kwargs == {
            "transient_retries": 1,
            "space_waits": 0,
            "vanished_retries": 0,
            "lock_waits": 0,
        }
        inspect.signature(videos.move_localized_file).bind(
            *(job.args or ()), **(job.kwargs or {})
        )

        # The localized output stays in staging for the retry. Nothing was
        # rejected. The finalize was not enqueued

        assert os.path.exists(hidden)
        assert os.path.exists(source)
        assert basename not in rejected_files(app)
        assert len(app.sql_queue) == 0
    finally:
        os.remove(source)
        os.remove(hidden)


def test_library_copy_rejects_after_max_retries(app, incoming_dir, monkeypatch):
    """Use the existing reject path after the retry budget is spent."""

    basename = "Hopeless Move (2021) - [DVD].mkv"
    source = os.path.join(incoming_dir, basename)
    with open(source, "wb") as f:
        f.write(b"untouched source")
    hidden = os.path.join(app.config["STAGING_DIR"], f".{basename}")
    with open(hidden, "wb") as f:
        f.write(b"localized output")

    file_details = {
        "basename": basename,
        "dirname": "Movies/Hopeless Move (2021)",
        "container": "Matroska",
    }

    monkeypatch.setattr(
        videos, "inspect_localized_file", lambda *args, **kwargs: {"filesize_bytes": 16}
    )

    def flaky_rename(src, dst, **kwargs):
        raise OSError(errno.ENOTCONN, "Socket is not connected")

    monkeypatch.setattr(videos, "_rename_with_retries", flaky_rename)

    with app.app_context():
        videos.move_localized_file(
            source,
            file_details,
            None,
            hidden,
            transient_retries=videos.MAX_TRANSIENT_RETRIES,
        )

    assert not any(
        job.id.startswith("retry_") for job in scheduled_jobs(app.handoff_queue)
    )
    assert basename in rejected_files(app)
    assert not os.path.exists(hidden)
    os.remove(os.path.join(app.config["REJECTS_DIR"], "exception", basename))


def test_finalize_defers_when_volumes_dead(app, monkeypatch):
    monkeypatch.setattr(videos, "_dead_volumes", lambda paths: ["/Volumes/Movies"])

    file_details = {
        "basename": "Deferred (2021) - [DVD].mkv",
        "dirname": "Movies/Deferred (2021)",
        "file_path": "Movies/Deferred (2021)/Deferred (2021) - [DVD].mkv",
    }
    with app.app_context():
        result = finalize_localization(
            "/nonexistent/source.mkv",
            file_details,
            None,
            "/tmp/.hidden.mkv",
            inspection={"filesize_bytes": 5},
        )
    assert result is False

    retries = [
        job
        for job in scheduled_jobs(app.sql_queue)
        if job.id.startswith("retry_finalize")
    ]
    assert [job.id for job in retries] == [
        requeue_job_id(
            "app.videos.finalize_localization",
            "/nonexistent/source.mkv",
            {"vanished_retries": 0, "sonarr_waits": 0, "lock_waits": 0},
        )
    ]
    # The retry keeps the inspection. It must not inspect again on the sql
    # worker.
    assert retries[0].kwargs["inspection"] == {"filesize_bytes": 5}


def test_finalize_waits_when_another_task_took_its_title_lock(app, tmp_path):
    """The lock expired during a long defer, and a new import took it (#520).

    The old cataloging must not write the rows or rename the copy. It
    waits for the lock with the same token."""

    from app.models import File

    file_details = {
        "basename": "Retaken (2021) - [DVD].mkv",
        "dirname": "Movies/Retaken (2021)",
        "file_path": "Movies/Retaken (2021)/Retaken (2021) - [DVD].mkv",
    }
    hidden_copy = tmp_path / ".Retaken (2021) - [DVD].mkv"
    hidden_copy.write_bytes(b"video")
    source = "/nonexistent/retaken.mkv"
    with app.app_context():
        lock = app.lock_manager.lock("test-520-title", 60000)
        app.redis.set(lock[1], "token-of-the-new-import", px=60000)
        try:
            result = finalize_localization(
                source,
                file_details,
                lock,
                str(hidden_copy),
                inspection={"filesize_bytes": 5},
            )
            assert result is False
            assert (
                File.query.filter_by(file_path=file_details["file_path"]).count() == 0
            )
            assert app.redis.get(lock[1]) == b"token-of-the-new-import"
        finally:
            app.redis.delete(lock[1])

    assert hidden_copy.exists()
    (retry,) = [
        job
        for job in scheduled_jobs(app.sql_queue)
        if job.id.startswith("retry_finalize") and job.args and job.args[0] == source
    ]
    assert retry.kwargs["lock_waits"] == 1
    assert retry.args[2] == lock and retry.args[4] == {"filesize_bytes": 5}


def test_move_to_rejects_survives_dead_volume(app, incoming_dir, monkeypatch):

    from app import importing

    source = os.path.join(incoming_dir, "reject-me.mkv")
    with open(source, "wb") as f:
        f.write(b"video")

    def dead(*args, **kwargs):
        raise OSError(57, "Socket is not connected")

    monkeypatch.setattr(importing.os, "rename", dead)
    monkeypatch.setattr(importing.shutil, "copy2", dead)
    try:
        with app.app_context():
            assert move_to_rejects(source, "exception") is False
        assert os.path.exists(source)
    finally:
        os.remove(source)


def test_move_to_rejects_cross_volume_move_is_staged_atomically(
    app, incoming_dir, monkeypatch
):
    """Stage a cross-volume move through a hidden name.

    When the rename fails, the function copies the file to a hidden
    name. It removes the source only after it promotes the copy."""

    from app import importing

    basename = "cross-volume.mkv"
    source = os.path.join(incoming_dir, basename)
    content = b"the complete file contents"
    with open(source, "wb") as f:
        f.write(content)

    def refuse_rename(src, dst):
        raise OSError(errno.EXDEV, "Cross-device link")

    monkeypatch.setattr(importing.os, "rename", refuse_rename)

    with app.app_context():
        assert move_to_rejects(source, "exception") is True

    destination = os.path.join(app.config["REJECTS_DIR"], "exception", basename)
    with open(destination, "rb") as f:
        assert f.read() == content
    assert not os.path.exists(source)
    assert not any(name.endswith(".partial") for name in rejected_files(app))
    os.remove(destination)


def test_move_to_rejects_failed_copy_leaves_no_partial(app, incoming_dir, monkeypatch):
    """Leave nothing in rejects, hidden or not, when a copy fails partway."""

    from app import importing

    basename = "half-copied.mkv"
    source = os.path.join(incoming_dir, basename)
    with open(source, "wb") as f:
        f.write(b"the complete file contents")

    def refuse_rename(src, dst):
        raise OSError(errno.EXDEV, "Cross-device link")

    def partial_copy(src, dst, **kwargs):
        with open(dst, "wb") as f:
            f.write(b"half")
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(importing.os, "rename", refuse_rename)
    monkeypatch.setattr(importing.shutil, "copy2", partial_copy)
    try:
        with app.app_context():
            assert move_to_rejects(source, "exception") is False
        assert os.path.exists(source)
        assert basename not in rejected_files(app)
        assert not any(name.endswith(".partial") for name in rejected_files(app))
    finally:
        os.remove(source)


def test_move_to_rejects_unremovable_source_discards_the_copy(
    app, incoming_dir, monkeypatch
):
    """Discard the copy when the source cannot be removed.

    This is the incident of 2026-08-04. The copy completes, but the
    function cannot delete the source (revoked SMB handle). The state
    must return to 'the source stays where it is'. No duplicate stays in
    rejects."""

    from app import importing

    basename = "undeletable-source.mkv"
    source = os.path.join(incoming_dir, basename)
    with open(source, "wb") as f:
        f.write(b"the complete file contents")

    real_remove = os.remove

    def refuse_rename(src, dst):
        raise OSError(errno.EXDEV, "Cross-device link")

    def refuse_source_remove(path, *args, **kwargs):
        if path == source:
            raise OSError(errno.EBADF, "Bad file descriptor")
        return real_remove(path, *args, **kwargs)

    monkeypatch.setattr(importing.os, "rename", refuse_rename)
    monkeypatch.setattr(importing.os, "remove", refuse_source_remove)
    try:
        with app.app_context():
            assert move_to_rejects(source, "exception") is False
        assert os.path.exists(source)
        assert basename not in rejected_files(app)
        assert not any(name.endswith(".partial") for name in rejected_files(app))
    finally:
        real_remove(source)


def test_dead_volumes_only_checks_network_mounts(app, monkeypatch):
    calls = []

    def fake_alive(mount, timeout=10):
        calls.append(mount)
        return mount != "/Volumes/DeadShare"

    monkeypatch.setattr(videos, "volume_alive", fake_alive)

    dead = videos._dead_volumes(
        [
            "/Volumes/DeadShare/subdir/file.mkv",
            "/Volumes/LiveShare/other",
            "/Users/server/local/path",
            None,
        ]
    )
    assert dead == ["/Volumes/DeadShare"]
    assert sorted(calls) == ["/Volumes/DeadShare", "/Volumes/LiveShare"]


def test_missing_volumes_reports_dead_mounts(app, monkeypatch):
    monkeypatch.setitem(app.config, "MOVIE_LIBRARY", "/Volumes/GhostShare/Movies")
    monkeypatch.setattr(maintenance, "volume_alive", lambda mount, timeout=10: False)
    assert maintenance.missing_volumes(app.config) == ["/Volumes/GhostShare"]


def test_heal_mounts_remounts_with_cooldown(app, monkeypatch):
    commands = []

    class FakeResult:
        returncode = 0
        stderr = ""
        # The healer reads `mount` for duplicate mountpoints now (#233). An
        # empty table means that there is no duplicate to free
        stdout = ""

    def fake_run(command, **kwargs):
        commands.append(command)
        return FakeResult()

    monkeypatch.setattr(maintenance.subprocess, "run", fake_run)
    # The volume is dead at the heal. It is alive after the remount.
    monkeypatch.setattr(
        maintenance,
        "volume_alive",
        lambda mount, timeout=10: any(c[0] == "osascript" for c in commands),
    )
    monkeypatch.setitem(
        app.config, "MOUNT_URLS", {"Movies": "smb://user@nas.local/Movies"}
    )

    with app.app_context():
        actions = maintenance.heal_mounts(["/Volumes/Movies"], app.redis, app.config)

    assert actions == ["remounted /Volumes/Movies"]
    assert commands[-1] == [
        "osascript",
        "-e",
        'mount volume "smb://user@nas.local/Movies"',
    ]

    # The cooldown prevents a remount loop
    with app.app_context():
        assert maintenance.heal_mounts(["/Volumes/Movies"], app.redis, app.config) == []


def test_heal_mounts_alert_only_without_url(app):
    with app.app_context():
        assert maintenance.heal_mounts(["/Volumes/Movies"], app.redis, app.config) == []


def test_copy_with_progress_reports_percentages(app, tmp_path):
    src = tmp_path / "big.mkv"
    dst = tmp_path / "copy.mkv"
    src.write_bytes(os.urandom(96 * 1024 * 1024))  # 3 chunks

    from tests.fakes import Job

    job = Job()
    with app.app_context():
        videos.copy_with_progress(str(src), str(dst), job, "big.mkv")

    assert dst.read_bytes() == src.read_bytes()
    percents = [u["progress"] for u in job.saved]
    assert percents == sorted(percents)
    assert percents[-1] == 100
    assert all(
        u["description"] == "'big.mkv' — Moving into the library" for u in job.saved
    )


def test_copy_with_progress_keeps_copy_when_source_close_fails(
    app, tmp_path, monkeypatch
):
    """Keep the copy when the close() of the source fails.

    An SMB server that has lost its handle for a file serves each read.
    Then it fails close() with EBADF forever. All the bytes are there.
    Thus, the copy stands. Fitzflix does not retry it until the share
    recovers."""

    src = tmp_path / "unclosable.mkv"
    dst = tmp_path / "copy.mkv"
    payload = os.urandom(1024)
    src.write_bytes(payload)

    real_open = open

    class UnclosableSource:
        def __init__(self, wrapped):
            self._wrapped = wrapped

        def read(self, size):
            return self._wrapped.read(size)

        def close(self):
            self._wrapped.close()
            raise OSError(errno.EBADF, "Bad file descriptor")

    def flaky_open(path, mode="r", *args, **kwargs):
        handle = real_open(path, mode, *args, **kwargs)
        if path == str(src):
            return UnclosableSource(handle)
        return handle

    monkeypatch.setattr(videos, "open", flaky_open, raising=False)
    with app.app_context():
        videos.copy_with_progress(str(src), str(dst), None, "unclosable.mkv")

    assert dst.read_bytes() == payload


def test_copy_with_progress_raises_when_short_copy_fails_to_close(
    app, tmp_path, monkeypatch
):
    """Raise when a short copy fails to close.

    A close error on a short copy is a real failure. It still reaches the
    transient-error handling of the caller."""

    src = tmp_path / "truncated.mkv"
    dst = tmp_path / "copy.mkv"
    src.write_bytes(os.urandom(4096))

    real_open = open

    class ShortSource:
        def __init__(self, wrapped):
            self._wrapped = wrapped
            self._served = False

        def read(self, size):
            if self._served:
                return b""
            self._served = True
            return self._wrapped.read(16)

        def close(self):
            self._wrapped.close()
            raise OSError(errno.EBADF, "Bad file descriptor")

    def flaky_open(path, mode="r", *args, **kwargs):
        handle = real_open(path, mode, *args, **kwargs)
        if path == str(src):
            return ShortSource(handle)
        return handle

    monkeypatch.setattr(videos, "open", flaky_open, raising=False)
    with app.app_context():
        with pytest.raises(OSError) as caught:
            videos.copy_with_progress(str(src), str(dst), None, "truncated.mkv")

    assert caught.value.errno == errno.EBADF


def test_save_track_metadata_writes_rows_and_releases_lock(app):
    from app import db
    from tests.factories import make_movie, make_movie_file

    with app.app_context():
        movie = make_movie("Metadata Test", 2021)
        file = make_movie_file(movie, "DVD")

        # Commit here. Then the session of save_track_metadata can see the
        # rows

        db.session.commit()
        details = {
            "video": {"format": "AVC", "codec": "V_MPEG4/ISO/AVC"},
            "audio_tracks": [
                {
                    "language": "eng",
                    "language_name": "English",
                    "streamorder": 1,
                    "format": "AC-3",
                    "channels": "5.1",
                    "default": True,
                }
            ],
            "subtitle_tracks": [
                {
                    "language": "eng",
                    "language_name": "English",
                    "streamorder": 2,
                    "elements": 100,
                    "default": False,
                    "forced": False,
                    "format": "PGS",
                }
            ],
            "filesize_bytes": 4 * 1024**3,
        }

        lock = app.lock_manager.lock("save-metadata-test", 30000)
        assert lock
        assert videos.save_track_metadata(file.id, details, lock=lock) is True

        # The task wrote through its own session. Refresh the view of this
        # session

        db.session.expire_all()

        assert file.format == "AVC"
        assert file.filesize_bytes == 4 * 1024**3
        assert [t.language for t in file.audiotrack] == ["eng"]
        assert len(file.subtrack) == 1

        # The task released the passed lock
        relock = app.lock_manager.lock("save-metadata-test", 1000)
        assert relock
        app.lock_manager.unlock(relock)


def test_upload_progress_callback_dedupes_by_percent(app, tmp_path, log_capture):
    source = tmp_path / "upload.bin"
    source.write_bytes(b"x" * 1000)

    with app.app_context():
        callback = videos.UploadProgressPercentage(str(source))
        for _ in range(200):
            callback(5)  # 0.5% per call, 2 calls per whole percent

    messages = [
        r.getMessage() for r in log_capture if "Uploading to AWS" in r.getMessage()
    ]
    # The 200 callbacks collapse to 1 line per distinct percentage
    assert len(messages) == len(set(messages))
    assert messages[-1].endswith("100%")
    assert 90 <= len(messages) <= 101


def test_move_localized_file_defers_when_volumes_dead(app, tmp_path, monkeypatch):
    monkeypatch.setattr(videos, "_dead_volumes", lambda paths: ["/Volumes/Movies"])

    file_details = {
        "basename": "Move Defer (2021) - [DVD].mkv",
        "dirname": "Movies/Move Defer (2021)",
    }
    with app.app_context():
        result = videos.move_localized_file(
            "/nonexistent/source.mkv", file_details, None, str(tmp_path / ".hidden.mkv")
        )
    assert result is False

    retries = [
        job.id
        for job in scheduled_jobs(app.handoff_queue)
        if job.id.startswith("retry_move_localized_file")
    ]
    assert retries == [move_retry("/nonexistent/source.mkv")]


def test_same_named_extras_of_two_films_keep_their_own_move_retries(
    app, tmp_path, monkeypatch
):
    """Test 2 films with an extra named "Theatrical Trailer.mkv" (#338).

    Both moves defer on a dead volume. The retry of film B must not
    replace the retry of film A. Each retry keeps its own source."""

    monkeypatch.setattr(videos, "_dead_volumes", lambda paths: ["/Volumes/Movies"])

    sources = ["/import/a/Theatrical Trailer.mkv", "/import/b/Theatrical Trailer.mkv"]
    with app.app_context():
        for source, film in zip(sources, ("Film A (2001)", "Film B (2002)")):
            videos.move_localized_file(
                source,
                {"basename": "Theatrical Trailer.mkv", "dirname": f"Movies/{film}"},
                None,
                str(tmp_path / f".{film}.mkv"),
            )

    retries = [
        job
        for job in scheduled_jobs(app.handoff_queue)
        if job.id.startswith("retry_move_localized_file") and job.args[0] in sources
    ]
    assert sorted(job.args[0] for job in retries) == sources


def test_same_named_files_keep_their_own_lock_retries(app, monkeypatch):
    """Test schedule_retry for 2 files with the same basename (#338)."""

    from app.videos import schedule_retry

    with app.app_context():
        for file_id in (101, 202):
            schedule_retry(
                app.file_queue,
                "app.videos.mkvpropedit_task",
                (5, 15),
                60,
                "'Deleted Scenes.mkv'",
                "Lock exists",
                args=(file_id, None, None, [], {}),
                key=file_id,
            )

    retries = [
        job
        for job in scheduled_jobs(app.file_queue)
        if job.id.startswith("retry_mkvpropedit_task") and "Deleted_Scenes" in job.id
    ]
    assert sorted(job.args[0] for job in retries) == [101, 202]


def test_rename_with_retries_rides_out_transient_errors(app, tmp_path, monkeypatch):

    from app import importing

    src = tmp_path / "source.mkv"
    dst = tmp_path / "final.mkv"
    src.write_bytes(b"video")

    real_rename = os.rename
    failures = iter([OSError(2, "spurious ENOENT"), OSError(57, "socket")])

    def flaky_rename(a, b):
        try:
            raise next(failures)
        except StopIteration:
            real_rename(a, b)

    monkeypatch.setattr(importing.os, "rename", flaky_rename)
    with app.app_context():
        videos._rename_with_retries(str(src), str(dst), attempts=5, delay=0)

    assert dst.exists() and not src.exists()


def test_rename_with_retries_raises_after_exhaustion(app, tmp_path, monkeypatch):
    monkeypatch.setattr(
        videos.os,
        "rename",
        lambda a, b: (_ for _ in ()).throw(OSError(2, "gone")),
    )
    with app.app_context():
        with pytest.raises(OSError):
            videos._rename_with_retries("/a", "/b", attempts=3, delay=0)


def test_truncated_matroska_defers_even_when_size_is_stable(
    app, sample_mkv, incoming_dir
):
    """Defer a truncated MKV even when its size is stable.

    This is the completeness gate. A partial MKV proves its own
    incompleteness structurally. Thus, even an old, size-stable file
    defers. This is the stalled-network-copy case. The size check cannot
    see it."""

    basename = "Halfway (2021) - [DVD].mkv"
    source = os.path.join(incoming_dir, basename)
    with open(sample_mkv, "rb") as f:
        data = f.read()
    with open(source, "wb") as f:
        f.write(data[: len(data) * 6 // 10])
    settle(source)

    try:
        with app.app_context():
            assert localization_task(source) is True

        retries = [
            job
            for job in scheduled_jobs(app.import_queue)
            if job.id.startswith("retry_")
        ]
        assert [job.id for job in retries] == [
            loc_retry(source, completeness_retries=1)
        ]
        job = retries[0]
        assert job.kwargs["completeness_retries"] == 1
        inspect.signature(localization_task).bind(
            *(job.args or ()), **(job.kwargs or {})
        )

        # The file is untouched. It is not staged and not rejected

        assert os.path.exists(source)
        assert os.listdir(app.config["STAGING_DIR"]) == []
        assert basename not in rejected_files(app)
    finally:
        os.remove(source)


def test_unprobeable_fresh_file_waits_out_the_quiet_period(app, incoming_dir):
    """Make a new unprobeable file wait for the quiet period.

    A format with no declared length cannot prove its completeness.
    Thus, a recently modified file waits for the mtime quiet period."""

    basename = "Fresh (2021) - [DVD].dat"
    source = os.path.join(incoming_dir, basename)
    with open(source, "wb") as f:
        f.write(b"unidentifiable bytes")

    try:
        with app.app_context():
            assert localization_task(source) is True

        retries = [
            job
            for job in scheduled_jobs(app.import_queue)
            if job.id.startswith("retry_")
        ]
        assert [job.id for job in retries] == [
            loc_retry(source, completeness_retries=1)
        ]
        assert retries[0].kwargs["completeness_retries"] == 1
        assert os.path.exists(source)
    finally:
        os.remove(source)


def test_completeness_budget_exhausted_imports_anyway(app, incoming_dir):
    """Import a file that never proves its completeness within the budget.

    The file goes through the normal pipeline. By design, a corrupt but
    complete file imports as it is."""

    basename = "Stubborn (2021) - [DVD].dat"
    source = os.path.join(incoming_dir, basename)
    with open(source, "wb") as f:
        f.write(b"unidentifiable bytes")

    with app.app_context():
        assert (
            localization_task(
                source, completeness_retries=videos.MAX_COMPLETENESS_RETRIES
            )
            is True
        )

        # The file went into the pipeline. It did not defer again

        assert not any(
            job.id.startswith("retry_") for job in scheduled_jobs(app.import_queue)
        )
        move_jobs = [
            job
            for job in app.handoff_queue.jobs
            if job.func_name == "app.videos.move_localized_file"
        ]
        assert len(move_jobs) == 1

        # Remove the staged working copy and the hidden output of the aborted
        # chain

        hidden = f".{os.path.splitext(basename)[0]}.localized.dat"
        assert move_jobs[0].args[3] == os.path.join(app.config["STAGING_DIR"], hidden)
        for _, _, files in os.walk(app.config["STAGING_DIR"]):
            assert files == [hidden] or files == []
        for stray in move_jobs[0].args[3], source:
            if os.path.exists(stray):
                os.remove(stray)


def test_localization_waits_for_library_space(app, sample_mkv, incoming_dir, caplog):
    """Wait for free space on the library volume. Never reject for space (#262).

    The wait comes before staging and the S3 upload. It releases the
    title lock. It books the task again with a new id for each wait. The
    first wait logs a warning. When space is back, the import goes on."""

    from app import importing

    basename = "Space Wait (2021) - [DVD].mkv"
    source = os.path.join(incoming_dir, basename)
    with open(sample_mkv, "rb") as f_in, open(source, "wb") as f_out:
        f_out.write(f_in.read())

    real_free = importing._free_bytes
    importing._free_bytes = lambda path: 0
    try:
        with app.app_context():
            assert localization_task(source) is True

            waits = [
                job
                for job in scheduled_jobs(app.import_queue)
                if job.id.startswith("retry_")
            ]
            assert [job.id for job in waits] == [loc_retry(source, space_waits=1)]
            assert waits[0].kwargs["space_waits"] == 1
            inspect.signature(videos.localization_task).bind(
                *(waits[0].args or ()), **(waits[0].kwargs or {})
            )
            assert "waiting for free space" in caplog.text

            # Nothing was staged, moved, or rejected. The source is intact.
            assert os.listdir(app.config["STAGING_DIR"]) == []
            assert len(app.handoff_queue) == 0
            assert basename not in rejected_files(app)
            assert os.path.exists(source)

            # The title lock is free while the file waits.
            details = importing.evaluate_filename(source)
            identifier = json.dumps(
                {
                    "title": details.get("title"),
                    "year": details.get("year"),
                    "feature_type": details.get("feature_type_name"),
                    "plex_title": details.get("plex_title"),
                    "edition": details.get("edition"),
                }
            )
            lock = app.lock_manager.lock(identifier, 1000)
            assert lock
            app.lock_manager.unlock(lock)

            # A second wait gets its own id and logs at info level.
            caplog.clear()
            assert localization_task(source, space_waits=1) is True
            assert loc_retry(source, space_waits=2) in [
                job.id for job in scheduled_jobs(app.import_queue)
            ]
            assert "waiting for free space" not in caplog.text

        # With space again, the import goes on to the library copy.
        importing._free_bytes = real_free
        with app.app_context():
            assert localization_task(source, space_waits=2) is True
            move_jobs = [
                job
                for job in app.handoff_queue.jobs
                if job.func_name == "app.videos.move_localized_file"
            ]
            assert len(move_jobs) == 1
            os.remove(move_jobs[0].args[3])
    finally:
        importing._free_bytes = real_free
        if os.path.exists(source):
            os.remove(source)


def space_move_fixture(app, incoming_dir, basename):
    """Make a source and a staged output. Return (source, hidden, details)."""

    source = os.path.join(incoming_dir, basename)
    with open(source, "wb") as f:
        f.write(b"untouched source")
    hidden = os.path.join(app.config["STAGING_DIR"], f".{basename}")
    with open(hidden, "wb") as f:
        f.write(b"localized output")
    details = {
        "basename": basename,
        "dirname": f"Movies/{basename.split(' - ')[0]}",
        "container": "Matroska",
    }
    return source, hidden, details


def test_library_copy_waits_for_space_before_copying(app, incoming_dir, monkeypatch):
    """Check the space before the inspection and the copy.

    A full library volume makes the copy wait. The output stays on
    staging. The task keeps the title lock. Nothing is rejected."""

    from app import importing

    basename = "Full Volume (2021) - [DVD].mkv"
    source, hidden, details = space_move_fixture(app, incoming_dir, basename)

    # The output is 8 days old. The weekly sweep would delete it.
    old = time.time() - 8 * 86400
    os.utime(hidden, (old, old))

    inspected = []
    monkeypatch.setattr(
        videos, "inspect_localized_file", lambda *a, **k: inspected.append(a) or {}
    )
    monkeypatch.setattr(importing, "_crosses_volumes", lambda *args: True)
    monkeypatch.setattr(importing, "_free_bytes", lambda path: 0)
    try:
        with app.app_context():
            result = videos.move_localized_file(
                source, details, "lock-sentinel", hidden
            )
        assert result is False
        assert inspected == []

        waits = [
            job
            for job in scheduled_jobs(app.handoff_queue)
            if job.id.startswith("retry_")
        ]
        assert [job.id for job in waits] == [move_retry(source, space_waits=1)]
        assert list(waits[0].args) == [source, details, "lock-sentinel", hidden]
        assert waits[0].kwargs == {
            "transient_retries": 0,
            "space_waits": 1,
            "vanished_retries": 0,
            "lock_waits": 0,
        }
        inspect.signature(videos.move_localized_file).bind(
            *(waits[0].args or ()), **(waits[0].kwargs or {})
        )
        assert os.path.exists(hidden) and os.path.exists(source)
        assert os.path.getmtime(hidden) > old + 86400
        assert basename not in rejected_files(app)
        assert len(app.sql_queue) == 0
    finally:
        os.remove(source)
        os.remove(hidden)


def test_library_copy_out_of_space_mid_copy_waits(app, incoming_dir, monkeypatch):
    """Wait, and keep the output, when the volume fills during the copy.

    Another import can take the space between the check and the copy.
    The partial copy goes away. The output on staging stays. The copy
    tries again later. Nothing is rejected."""

    basename = "Filled Midway (2021) - [DVD].mkv"
    source, hidden, details = space_move_fixture(app, incoming_dir, basename)
    destination_hidden = os.path.join(
        app.config["LIBRARY_DIR"], details["dirname"], f".{basename}"
    )

    monkeypatch.setattr(videos, "inspect_localized_file", lambda *a, **k: {})

    def full_during_copy(src, dst, **kwargs):
        with open(dst, "wb") as f:
            f.write(b"partial")
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(videos, "_rename_with_retries", full_during_copy)
    try:
        with app.app_context():
            result = videos.move_localized_file(
                source, details, "lock-sentinel", hidden
            )
        assert result is False
        assert not os.path.exists(destination_hidden)
        assert os.path.exists(hidden) and os.path.exists(source)
        assert basename not in rejected_files(app)
        assert len(app.sql_queue) == 0
        assert move_retry(source, space_waits=1) in [
            job.id for job in scheduled_jobs(app.handoff_queue)
        ]
    finally:
        os.remove(source)
        os.remove(hidden)


def test_sweep_skips_a_file_that_waits_for_space(app):
    """Test that the hourly sweep starts no second chain for a waiting file.

    A space wait is a scheduled job with its own id. The sweep saw only
    running and queued jobs by basename. Thus, each sweep started 1 more
    wait chain. The wait now marks the file, and the sweep skips it."""

    from app import importing

    basename = "Swept Film (2001) - [DVD].mkv"
    path = os.path.join(app.config["IMPORT_DIR"], basename)
    os.makedirs(app.config["IMPORT_DIR"], exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"waiting")
    try:
        with app.app_context():
            importing.mark_space_wait(basename)
            importing.manual_import_task()
            assert safe_job_id(basename) not in app.import_queue.job_ids

            # When the wait ends, the marker goes, and the sweep takes the file.
            importing.clear_space_wait(basename)
            importing.manual_import_task()
            assert safe_job_id(basename) in app.import_queue.job_ids
    finally:
        os.remove(path)
        app.import_queue.empty()


def test_localization_wait_marks_the_file(app, sample_mkv, incoming_dir):
    from app import importing

    basename = "Marked Wait (2021) - [DVD].mkv"
    source = os.path.join(incoming_dir, basename)
    with open(sample_mkv, "rb") as f_in, open(source, "wb") as f_out:
        f_out.write(f_in.read())
    real_free = importing._free_bytes
    importing._free_bytes = lambda path: 0
    try:
        with app.app_context():
            assert localization_task(source) is True
            assert importing.space_wait_pending(app.redis, basename)
            ttl = app.redis.ttl(importing.SPACE_WAIT_KEY.format(basename))
            assert 0 < ttl <= importing.SPACE_WAIT_MARK.total_seconds()
    finally:
        importing._free_bytes = real_free
        os.remove(source)


def test_waiting_copy_keeps_its_title_lock(app, incoming_dir, monkeypatch):
    """Test that a copy that waits for space keeps its title lock alive.

    The lock lasts 1 day. A wait can last longer. Each wait refreshes the
    lock while the token matches, and takes it again if it expired. A
    lock that another task holds is left alone."""

    from app import importing

    basename = "Long Wait (2021) - [DVD].mkv"
    source, hidden, details = space_move_fixture(app, incoming_dir, basename)
    monkeypatch.setattr(importing, "_crosses_volumes", lambda *args: True)
    monkeypatch.setattr(importing, "_free_bytes", lambda path: 0)
    day_ms = app.config["LOCALIZATION_TASK_TIMEOUT"] * 1000
    try:
        with app.app_context():
            lock = app.lock_manager.lock("long-wait-title", 60000)
            assert app.redis.pttl("long-wait-title") <= 60000
            videos.move_localized_file(source, details, lock, hidden)
            assert app.redis.pttl("long-wait-title") > day_ms - 60000
            assert importing.space_wait_pending(app.redis, basename)

            # An expired lock is taken again with the same token.
            app.redis.delete("long-wait-title")
            videos.move_localized_file(source, details, lock, hidden, space_waits=1)
            assert app.redis.get("long-wait-title") == lock.key

            # A lock that another task holds stays as it is.
            app.redis.set("long-wait-title", "someone-else", px=5000)
            videos.move_localized_file(source, details, lock, hidden, space_waits=2)
            assert app.redis.get("long-wait-title") == b"someone-else"
    finally:
        os.remove(source)
        os.remove(hidden)


def test_trail_names_a_retry_that_passes_the_path_by_keyword(app):
    """Test that a space-wait retry shows on the pipeline trail."""

    from types import SimpleNamespace

    from app.pipeline import _stage_for

    job = SimpleNamespace(
        func_name="app.videos.localization_task",
        args=(),
        kwargs={"file_path": "/import/Waiting (2021) - [DVD].mkv", "space_waits": 1},
    )
    assert _stage_for(job) == ("Waiting (2021) - [DVD].mkv", "Localizing")


def _finalize_jobs(app, vanished_retries):
    return [
        job
        for job in app.sql_queue.jobs
        if job.func_name == "app.videos.finalize_localization"
        and job.kwargs.get("vanished_retries") == vanished_retries
    ]


def test_a_vanished_library_copy_is_copied_again_from_staging(
    app, sample_mkv, incoming_dir, monkeypatch
):
    """Copy the output again when the mount loses the hidden library copy.

    The Movies NFS mount can drop at the end of a large write. macOS
    remounts it, and the just-written hidden copy is gone. Before, the
    rename failed, the import went to rejects, and an error email went
    out (Fort Apache, 2026-10-15). Now the output stays on staging until
    the rename. finalize gives the copy back to the handoff queue."""

    separate_library_volume(app, monkeypatch)
    basename = "Vanishing Copy (2021) - [DVD].mkv"
    source = os.path.join(incoming_dir, basename)
    with open(sample_mkv, "rb") as f_in, open(source, "wb") as f_out:
        f_out.write(f_in.read())

    with app.app_context():
        assert localization_task(source) is True
        move = next(
            job
            for job in app.handoff_queue.jobs
            if job.func_name == "app.videos.move_localized_file"
        )
        staged = move.args[3]
        assert videos.move_localized_file(*move.args) is True

        # The copy crossed volumes. The output stays on staging.

        assert os.path.exists(staged)
        (first,) = _finalize_jobs(app, 0)
        assert first.kwargs["staged_output"] == staged
        hidden = first.args[3]
        assert hidden.startswith(app.config["LIBRARY_DIR"])

        # The mount drops and loses the hidden copy.

        os.remove(hidden)
        assert finalize_localization(*first.args, **first.kwargs) is False
        assert File.query.filter_by(basename=basename).first() is None
        assert os.path.exists(source)
        assert basename not in rejected_files(app)

        (retry,) = [
            job
            for job in scheduled_jobs(app.handoff_queue)
            if job.id == move_retry(source, vanished_retries=1)
        ]
        assert retry.args[3] == staged
        assert retry.kwargs["vanished_retries"] == 1

        # The retry copies again, and the import completes. Staging is
        # empty after the rename.

        assert videos.move_localized_file(*retry.args, **retry.kwargs) is True
        (second,) = _finalize_jobs(app, 1)
        finalize_localization(*second.args, **second.kwargs)

        output = os.path.join(
            app.config["LIBRARY_DIR"],
            "Movies/Vanishing Copy (2021)/Vanishing Copy (2021) - [DVD].mkv",
        )
        assert os.path.exists(output)
        assert not os.path.exists(staged)
        file = File.query.filter_by(basename=basename).one()
        assert app.lock_manager.lock(file.file_identifier(), 1000)


def test_a_library_copy_that_keeps_vanishing_fails_as_before(
    app, sample_mkv, incoming_dir, monkeypatch
):
    """Stop copying again after MAX_VANISHED_RETRIES.

    A mount that loses every copy must not loop for ever. The import
    goes to rejects with the error, as before. Staging is cleaned up."""

    from app.importing import MAX_VANISHED_RETRIES

    separate_library_volume(app, monkeypatch)
    basename = "Always Vanishing (2021) - [DVD].mkv"
    source = os.path.join(incoming_dir, basename)
    with open(sample_mkv, "rb") as f_in, open(source, "wb") as f_out:
        f_out.write(f_in.read())

    with app.app_context():
        assert localization_task(source) is True
        move = next(
            job
            for job in app.handoff_queue.jobs
            if job.func_name == "app.videos.move_localized_file"
        )
        staged = move.args[3]
        assert videos.move_localized_file(*move.args) is True
        (job,) = _finalize_jobs(app, 0)
        os.remove(job.args[3])

        kwargs = dict(job.kwargs, vanished_retries=MAX_VANISHED_RETRIES)
        finalize_localization(*job.args, **kwargs)

        assert basename in rejected_files(app)
        assert not os.path.exists(staged)
        assert not any(
            "vanished" in job.id for job in scheduled_jobs(app.handoff_queue)
        )
        assert File.query.filter_by(basename=basename).first() is None
    os.remove(os.path.join(app.config["REJECTS_DIR"], "exception", basename))


@pytest.mark.parametrize(
    "reserve, queued, kept, offered",
    [
        (0, True, True, True),
        (0, False, False, True),
        (10**18, True, False, False),
    ],
)
def test_finalize_gives_the_staged_copy_to_the_atmos_job(
    app, sample_mkv, incoming_dir, monkeypatch, reserve, queued, kept, offered
):
    """Keep the staged copy for the Atmos job of the file, or remove it.

    The Atmos job read the just-copied file back from the NAS (Gandhi:
    8 min 41 s). Now finalize offers the staged copy. It stays only if
    the job was queued. A staging volume short of space keeps nothing,
    and the job then copies from the library."""

    import app.atmos as atmos

    separate_library_volume(app, monkeypatch)
    monkeypatch.setattr(atmos, "STAGED_HANDOFF_RESERVE_BYTES", reserve)
    offers = []
    queues = []

    def fake_enqueue(file_id, staged_source=None, queue=None):
        offers.append(staged_source)
        queues.append(queue.name if queue else None)
        return queued

    monkeypatch.setattr(atmos, "maybe_enqueue_atmos_supplement", fake_enqueue)

    basename = f"Atmos Handoff {reserve} {queued} (2021) - [DVD].mkv"
    source = os.path.join(incoming_dir, basename)
    with open(sample_mkv, "rb") as f_in, open(source, "wb") as f_out:
        f_out.write(f_in.read())

    with app.app_context():
        assert localization_task(source) is True
        move = next(
            job
            for job in app.handoff_queue.jobs
            if job.func_name == "app.videos.move_localized_file"
            and job.args[0] == source
        )
        staged = move.args[3]
        assert videos.move_localized_file(*move.args) is True
        job = next(
            job
            for job in app.sql_queue.jobs
            if job.func_name == "app.videos.finalize_localization"
            and job.args[0] == source
        )
        finalize_localization(*job.args, **job.kwargs)

        output = os.path.join(app.config["LIBRARY_DIR"], job.args[1]["file_path"])
        assert os.path.exists(output)
        assert len(offers) == 1
        assert queues == ["fitzflix-handoff"]
        if offered:
            assert offers[0]["path"] == staged
            assert offers[0]["size"] == os.path.getsize(output)
            assert offers[0]["mtime"] == os.stat(output).st_mtime
        else:
            assert offers[0] is None
        assert os.path.exists(staged) is kept
    if kept:
        os.remove(staged)


@pytest.mark.parametrize("create", [True, False])
def test_create_flac_twins_off_turns_off_the_flac_step(
    app, sample_mkv, incoming_dir, monkeypatch, create
):
    """CREATE_FLAC_TWINS off stops the import from adding FLAC twins.

    A passthrough player plays the lossless original. Thus, the FLAC
    twins have no use for it."""

    import app.importing as importing

    monkeypatch.setitem(app.config, "CREATE_FLAC_TWINS", create)
    calls = []
    monkeypatch.setattr(
        importing, "supplement_lossless_tracks", lambda path: calls.append(path)
    )

    basename = f"Flac Create {create} (2021) - [DVD].mkv"
    source = os.path.join(incoming_dir, basename)
    with open(sample_mkv, "rb") as f_in, open(source, "wb") as f_out:
        f_out.write(f_in.read())

    with app.app_context():
        assert localization_task(source) is True
        move = next(
            job
            for job in app.handoff_queue.jobs
            if job.func_name == "app.videos.move_localized_file"
            and job.args[0] == source
        )
        staged = move.args[3]

    assert len(calls) == (1 if create else 0)
    move.delete()
    os.remove(staged)
    if os.path.exists(source):
        os.remove(source)


def test_create_atmos_twins_off_turns_off_the_atmos_step(
    app, sample_mkv, incoming_dir, monkeypatch
):
    """CREATE_ATMOS_TWINS off stops the import from queueing the Atmos job.

    Finalize does not ask for the job, and it does not keep the staged
    copy for it."""

    import app.atmos as atmos

    separate_library_volume(app, monkeypatch)
    monkeypatch.setitem(app.config, "CREATE_ATMOS_TWINS", False)
    monkeypatch.setattr(atmos, "STAGED_HANDOFF_RESERVE_BYTES", 0)
    offers = []

    def fake_enqueue(file_id, staged_source=None, queue=None):
        offers.append(staged_source)
        return True

    monkeypatch.setattr(atmos, "maybe_enqueue_atmos_supplement", fake_enqueue)

    basename = "Atmos Skip (2021) - [DVD].mkv"
    source = os.path.join(incoming_dir, basename)
    with open(sample_mkv, "rb") as f_in, open(source, "wb") as f_out:
        f_out.write(f_in.read())

    with app.app_context():
        assert localization_task(source) is True
        move = next(
            job
            for job in app.handoff_queue.jobs
            if job.func_name == "app.videos.move_localized_file"
            and job.args[0] == source
        )
        staged = move.args[3]
        assert videos.move_localized_file(*move.args) is True
        job = next(
            job
            for job in app.sql_queue.jobs
            if job.func_name == "app.videos.finalize_localization"
            and job.args[0] == source
        )
        finalize_localization(*job.args, **job.kwargs)

        output = os.path.join(app.config["LIBRARY_DIR"], job.args[1]["file_path"])
        assert os.path.exists(output)
        assert offers == []
        assert not os.path.exists(staged)


def test_a_replaced_files_archive_is_deleted_from_the_handoff_queue(
    app, sample_mkv, incoming_dir
):
    """Queue the S3 delete of a replaced file on the handoff queue.

    The delete is the last step of a file in the pipeline. On the
    file-operation queue, it waited in line behind long file work. On
    the sql queue, the single sql worker would wait on the network."""

    from datetime import datetime, timezone

    from app import db
    from tests.factories import make_movie, make_movie_file

    key = "untouched/Replace Archive (2021) - [SDTV].mkv"
    with app.app_context():
        movie = make_movie("Replace Archive", 2021)
        make_movie_file(
            movie,
            "SDTV",
            aws_key=key,
            aws_date_uploaded=datetime.now(timezone.utc),
        )
        db.session.commit()

    basename = "Replace Archive (2021) - [DVD].mkv"
    source = os.path.join(incoming_dir, basename)
    with open(sample_mkv, "rb") as f_in, open(source, "wb") as f_out:
        f_out.write(f_in.read())

    with app.app_context():
        assert localization_task(source) is True
        move = next(
            job
            for job in app.handoff_queue.jobs
            if job.func_name == "app.videos.move_localized_file"
            and job.args[0] == source
        )
        assert videos.move_localized_file(*move.args) is True
        finalize = next(
            job
            for job in app.sql_queue.jobs
            if job.func_name == "app.videos.finalize_localization"
            and job.args[0] == source
        )
        finalize_localization(*finalize.args, **finalize.kwargs)

        def deletes(queue):
            return [
                job.args
                for job in queue.jobs
                if job.func_name == "app.videos.aws_delete"
            ]

        assert deletes(app.handoff_queue) == [(key,)]
        assert deletes(app.file_queue) == []


def test_a_reused_records_old_archive_key_is_deleted(
    app, sample_mkv, incoming_dir, monkeypatch
):
    """Retire the old archive key when an import reuses a File record.

    The new file has the same library path. Thus, the import updates the
    record, and the record claims the new key. No record claims the old
    key after the commit. A later restore of it would import the old file
    under its old name (Sapphire, 2026-10-20)."""

    from datetime import datetime, timezone

    from app import aws_storage, db
    from app.models import File
    from tests.factories import make_movie, make_movie_file

    old_key = "untouched/Reuse Archive (2021) - [WEBDL-1080p].mkv"
    new_key = "untouched/Reuse Archive (2021) - [DVD].mkv"
    with app.app_context():
        movie = make_movie("Reuse Archive", 2021)
        row = make_movie_file(
            movie,
            "DVD",
            aws_key=old_key,
            aws_date_uploaded=datetime.now(timezone.utc),
        )
        db.session.commit()
        row_id = row.id

    monkeypatch.setitem(app.config, "ARCHIVE_ORIGINAL_MEDIA", True)
    monkeypatch.setattr(
        aws_storage,
        "aws_upload",
        lambda *a, **k: (new_key, datetime.now(timezone.utc), 1),
    )

    basename = "Reuse Archive (2021) - [DVD].mkv"
    source = os.path.join(incoming_dir, basename)
    with open(sample_mkv, "rb") as f_in, open(source, "wb") as f_out:
        f_out.write(f_in.read())

    with app.app_context():
        assert localization_task(source) is True
        move = next(
            job
            for job in app.handoff_queue.jobs
            if job.func_name == "app.videos.move_localized_file"
            and job.args[0] == source
        )
        assert videos.move_localized_file(*move.args) is True
        finalize = next(
            job
            for job in app.sql_queue.jobs
            if job.func_name == "app.videos.finalize_localization"
            and job.args[0] == source
        )
        finalize_localization(*finalize.args, **finalize.kwargs)

        assert db.session.get(File, row_id).aws_key == new_key
        deletes = [
            job.args
            for job in app.handoff_queue.jobs
            if job.func_name == "app.videos.aws_delete"
        ]
        assert deletes == [(old_key,)]


def test_a_lost_copy_keeps_the_file_of_the_worse_record(
    app, sample_mkv, incoming_dir, monkeypatch
):
    """A hand-off rolls back. The worse record stays, and so must its file.

    Before, the worse file was deleted before the rename (the Fort Apache
    shape). The title could end with no library file."""

    from app import db
    from tests.factories import make_movie, make_movie_file

    separate_library_volume(app, monkeypatch)
    with app.app_context():
        movie = make_movie("Kept Worse", 2021)
        worse = make_movie_file(movie, "SDTV")
        db.session.commit()
        worse_path = os.path.join(app.config["LIBRARY_DIR"], worse.file_path)
    os.makedirs(os.path.dirname(worse_path), exist_ok=True)
    with open(worse_path, "wb") as f:
        f.write(b"old copy")

    basename = "Kept Worse (2021) - [DVD].mkv"
    source = os.path.join(incoming_dir, basename)
    with open(sample_mkv, "rb") as f_in, open(source, "wb") as f_out:
        f_out.write(f_in.read())

    with app.app_context():
        assert localization_task(source) is True
        move = next(
            job
            for job in app.handoff_queue.jobs
            if job.func_name == "app.videos.move_localized_file"
        )
        assert videos.move_localized_file(*move.args) is True
        (first,) = _finalize_jobs(app, 0)
        os.remove(first.args[3])
        assert finalize_localization(*first.args, **first.kwargs) is False

    assert os.path.exists(worse_path)
    os.remove(worse_path)
    for path in (source, first.kwargs["staged_output"]):
        if path and os.path.exists(path):
            os.remove(path)


def test_a_failure_after_the_commit_keeps_the_imported_file(
    app, sample_mkv, incoming_dir, monkeypatch
):
    """The record is committed. A follow-up failure must not reject the file."""

    import app.atmos as atmos

    def broken(*args, **kwargs):
        raise RuntimeError("Redis is not reachable")

    # The first follow-up step after the commit. The source still exists.

    monkeypatch.setattr(atmos, "staged_handoff", broken)
    basename = "Late Failure (2021) - [DVD].mkv"
    source = os.path.join(incoming_dir, basename)
    with open(sample_mkv, "rb") as f_in, open(source, "wb") as f_out:
        f_out.write(f_in.read())

    with app.app_context():
        assert localization_task(source) is True
        move = next(
            job
            for job in app.handoff_queue.jobs
            if job.func_name == "app.videos.move_localized_file"
        )
        videos.move_localized_file(*move.args)
        (finalize,) = _finalize_jobs(app, 0)
        finalize_localization(*finalize.args, **finalize.kwargs)
        record = File.query.filter_by(basename=basename).one()
        output = os.path.join(app.config["LIBRARY_DIR"], record.file_path)

    assert os.path.exists(output)
    assert basename not in rejected_files(app)
    assert not os.path.exists(source)
    os.remove(output)


def test_a_staging_remux_short_of_space_waits_instead_of_rejecting(
    app, incoming_dir, monkeypatch
):
    """The inspection remux needs a second copy on staging. A shortage waits (#262)."""

    import subprocess
    from types import SimpleNamespace

    from app import importing

    basename = "Short Staging (2021) - [DVD].mkv"
    source, hidden, details = space_move_fixture(app, incoming_dir, basename)

    def failing_inspection(*args, **kwargs):
        raise subprocess.CalledProcessError(2, "mkvmerge")

    monkeypatch.setattr(videos, "inspect_localized_file", failing_inspection)
    monkeypatch.setattr(importing, "inspect_localized_file", failing_inspection)
    monkeypatch.setattr(
        importing.shutil, "disk_usage", lambda path: SimpleNamespace(free=0)
    )
    try:
        with app.app_context():
            result = videos.move_localized_file(
                source, details, "lock-sentinel", hidden
            )
        assert result is False
        waits = [
            job.id
            for job in scheduled_jobs(app.handoff_queue)
            if job.id.startswith("retry_")
        ]
        assert waits == [move_retry(source, space_waits=1)]
        assert os.path.exists(hidden) and os.path.exists(source)
        assert basename not in rejected_files(app)
    finally:
        os.remove(source)
        os.remove(hidden)


def test_a_tmdb_outage_retries_instead_of_rejecting(
    app, sample_mkv, incoming_dir, monkeypatch
):
    from app import importing
    from app.importing import localization_pending

    monkeypatch.setattr(
        importing,
        "evaluate_filename",
        lambda path, **kw: importing.FilenameRejection(importing.TMDB_UNREACHABLE),
    )
    basename = "Unseen Film {tmdb-999123} - [DVD].mkv"
    source = os.path.join(incoming_dir, basename)
    with open(sample_mkv, "rb") as f_in, open(source, "wb") as f_out:
        f_out.write(f_in.read())
    try:
        with app.app_context():
            assert localization_task(source) is True
            assert localization_pending(app.import_queue, source)

            # The TMDB tries have their own count. The transient count of
            # the staging copy stays at 0.

            (retry,) = [
                job
                for job in scheduled_jobs(app.import_queue)
                if job.func_name == "app.videos.localization_task"
            ]
            assert retry.kwargs["tmdb_retries"] == 1
            assert retry.kwargs["transient_retries"] == 0
        assert os.path.exists(source)
        assert basename not in rejected_files(app)
    finally:
        os.remove(source)


def test_a_name_too_long_for_the_work_files_is_rejected_clearly(app, incoming_dir):
    """The work files add bytes to the name. A name system allows 255 bytes."""

    from app import importing

    basename = ("Very Long Title " * 14).strip() + " (2001) - [DVD].mkv"
    assert importing.MAX_NAME_BYTES < len(basename.encode()) <= 255
    source = os.path.join(incoming_dir, basename)
    with open(source, "wb") as f:
        f.write(b"x")
    with app.app_context():
        assert localization_task(source) is False
    assert basename in rejected_files(app)


def test_a_failed_finalize_commit_leaves_no_library_copy(
    app, sample_mkv, incoming_dir, monkeypatch
):
    """The rename runs before the commit. A failed commit must remove it (#316)."""

    from app import importing

    basename = "Commit Fail (2021) - [DVD].mkv"
    source = os.path.join(incoming_dir, basename)
    with open(sample_mkv, "rb") as f_in, open(source, "wb") as f_out:
        f_out.write(f_in.read())

    class Failing:
        """A HeldImport stand-in whose cleanup delete fails, as a commit can."""

        class query:
            @staticmethod
            def filter_by(**kwargs):
                raise RuntimeError("simulated failure before the commit")

    with app.app_context():
        assert localization_task(source) is True
        (move,) = [
            job
            for job in app.handoff_queue.jobs
            if job.func_name == "app.videos.move_localized_file"
        ]
        assert videos.move_localized_file(*move.args) is True
        (finalize,) = [
            job
            for job in app.sql_queue.jobs
            if job.func_name == "app.videos.finalize_localization"
        ]
        # The localization reserved the space of its output. The failed
        # cataloging frees the reservation (abandon_import).

        assert app.redis.hexists(importing.STAGING_RESERVED_KEY, basename)
        monkeypatch.setattr(importing, "HeldImport", Failing)
        finalize_localization(*finalize.args)
        assert not app.redis.hexists(importing.STAGING_RESERVED_KEY, basename)

        output = os.path.join(
            app.config["LIBRARY_DIR"],
            "Movies/Commit Fail (2021)/Commit Fail (2021) - [DVD].mkv",
        )
        assert not os.path.exists(output)
        assert not os.path.exists(finalize.args[3])
        assert File.query.filter_by(basename=basename).first() is None
        assert not os.path.exists(source)
        assert os.listdir(app.config["STAGING_DIR"]) == []
        rejected = os.path.join(app.config["REJECTS_DIR"], "exception", basename)
        assert os.path.exists(rejected)
        os.remove(rejected)


def test_rejects_never_create_a_directory_on_an_unmounted_volume(
    app, monkeypatch, tmp_path
):
    """Test move_to_rejects with an SSD that dropped during the task (#341).

    makedirs would create the rejects tree on the boot disk. The file
    stays in place, and nothing is created."""

    made = []
    monkeypatch.setitem(app.config, "REJECTS_DIR", "/Volumes/fitzflix-absent/rejects")
    monkeypatch.setattr(os, "makedirs", lambda *a, **k: made.append(a))
    source = tmp_path / "Dropped (2001) - [DVD].mkv"
    source.write_bytes(b"movie")

    with app.app_context():
        assert move_to_rejects(str(source), "exception") is False

    assert made == []
    assert source.exists()


def test_a_hold_never_creates_a_directory_on_an_unmounted_volume(
    app, monkeypatch, tmp_path
):
    from app import sdr_conversion

    made = []
    monkeypatch.setitem(app.config, "HELD_DIR", "/Volumes/fitzflix-absent/held")
    monkeypatch.setattr(os, "makedirs", lambda *a, **k: made.append(a))
    source = tmp_path / "Held (2001) - [Remux-2160p].mkv"
    source.write_bytes(b"movie")

    with app.app_context():
        with pytest.raises(OSError):
            sdr_conversion.hold_import(str(source), {}, {"source_format": "HDR10"}, [])

    assert made == []
    assert source.exists()


def test_a_failed_handoff_releases_the_title_lock(
    app, sample_mkv, incoming_dir, monkeypatch
):
    """Test a localization whose move enqueue fails (Redis away, #340).

    The title lock must not stay for its 1-day TTL. The source stays in
    the import directory for the sweep. The staged output goes."""

    basename = "Handoff Fault (2021) - [DVD].mkv"
    source = os.path.join(incoming_dir, basename)
    with open(sample_mkv, "rb") as f_in, open(source, "wb") as f_out:
        f_out.write(f_in.read())

    unlocked = []
    real_unlock = app.lock_manager.unlock
    monkeypatch.setattr(
        app.lock_manager,
        "unlock",
        lambda lock: (unlocked.append(lock), real_unlock(lock))[1],
    )

    def refuse(*args, **kwargs):
        raise ConnectionError("Redis is away")

    monkeypatch.setattr(app.handoff_queue, "enqueue", refuse)
    try:
        with app.app_context():
            assert localization_task(source) is False
        title_locks = [lock for lock in unlocked if "Handoff Fault" in lock.resource]
        assert len(title_locks) == 1
        assert app.redis.get(title_locks[0].resource) is None
        assert os.path.exists(source)
        staging = app.config["STAGING_DIR"]
        assert not [n for n in os.listdir(staging) if "Handoff Fault" in n]
    finally:
        if os.path.exists(source):
            os.remove(source)


def test_a_library_path_never_creates_a_directory_on_an_unmounted_volume(
    app, sample_mkv, incoming_dir, monkeypatch
):
    """Test the import with a library volume that dropped during the task.

    5 sites of importing.py called makedirs on a library path. With the
    volume absent, makedirs built the library tree on the boot disk
    (#227, #341). Each site now goes through makedirs_on_mounted_volume.
    The task rejects the file as an exception, so the source is kept in
    the rejects directory, and the absent volume gets no directory."""

    basename = "Absent Library (2021) - [DVD].mkv"
    source = os.path.join(incoming_dir, basename)
    with open(sample_mkv, "rb") as f_in, open(source, "wb") as f_out:
        f_out.write(f_in.read())

    made = []
    real_makedirs = os.makedirs

    def record(path, *args, **kwargs):
        made.append(path)
        return real_makedirs(path, *args, **kwargs)

    monkeypatch.setitem(app.config, "LIBRARY_DIR", "/Volumes/fitzflix-absent/library")
    # The liveness probe of the task must not defer first. The makedirs
    # guard is the subject here.
    monkeypatch.setattr(videos, "_dead_volumes", lambda paths: [])
    monkeypatch.setattr(os, "makedirs", record)
    try:
        with app.app_context():
            localization_task(source)
        assert [
            path for path in made if path.startswith("/Volumes/fitzflix-absent")
        ] == []
        assert not os.path.exists("/Volumes/fitzflix-absent")
        rejected = os.path.join(app.config["REJECTS_DIR"], "exception", basename)
        assert os.path.exists(rejected)
        staging = app.config["STAGING_DIR"]
        assert not [n for n in os.listdir(staging) if "Absent Library" in n]
    finally:
        for path in (
            source,
            os.path.join(app.config["REJECTS_DIR"], "exception", basename),
        ):
            if os.path.exists(path):
                os.remove(path)
