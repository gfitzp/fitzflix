"""End-to-end localization through local staging, and the mount-resilience
behaviors: pre-flight deferral, best-effort rejects, and dead-mount detection.

The end-to-end test builds a real (tiny) Matroska file with ffmpeg and
mkvmerge, runs localization_task and finalize_localization against it, and
checks the file lands in the library with its database records — with all
intermediate work done in the staging directory.
"""

import errno
import inspect
import json
import os
import subprocess

import pytest

import app.videos as videos
import app.maintenance as maintenance

from app.models import File
from app.videos import finalize_localization, localization_task, move_to_rejects

from tests.conftest import _TMP


@pytest.fixture(scope="module")
def sample_mkv(app):
    """A 1-second Matroska file with an English audio track."""

    base = os.path.join(_TMP, "sample-base.mp4")
    mkv = os.path.join(_TMP, "sample.mkv")
    if not os.path.exists(mkv):
        subprocess.run(
            [
                app.config["FFMPEG_BIN"],
                "-f",
                "lavfi",
                "-i",
                "testsrc=duration=1:size=320x240:rate=10",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=1",
                "-c:v",
                "libx264",
                "-c:a",
                "aac",
                "-shortest",
                "-y",
                base,
            ],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                app.config["MKVMERGE_BIN"],
                "-o",
                mkv,
                "--language",
                "1:eng",
                base,
            ],
            check=True,
            capture_output=True,
        )
    return mkv


def test_localization_end_to_end_through_staging(app, sample_mkv, incoming_dir):
    basename = "Staging Test (2021) - [DVD].mkv"
    source = os.path.join(incoming_dir, basename)
    with open(sample_mkv, "rb") as f_in, open(source, "wb") as f_out:
        f_out.write(f_in.read())

    with app.app_context():
        assert localization_task(source) is True

        # The processed hidden file was left in staging, and the library copy
        # was handed to the file-operation queue, not the sql queue

        move_jobs = [
            job
            for job in app.file_queue.jobs
            if job.func_name == "app.videos.move_localized_file"
        ]
        assert len(move_jobs) == 1
        move_args = move_jobs[0].args
        assert move_args[0] == source
        hidden = move_args[3]
        assert hidden.startswith(app.config["STAGING_DIR"])
        assert os.path.exists(hidden)

        # The staged source copy is already cleaned up after mkvmerge

        assert os.listdir(app.config["STAGING_DIR"]) == [os.path.basename(hidden)]

        # The move carries the file to a hidden destination name, empties
        # staging, and enqueues the finalize on the sql queue

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

        # The move already inspected the file, so finalize receives the
        # media details precomputed and never opens the file itself

        assert len(job_args) == 5
        inspection = job_args[4]
        assert inspection["audio_tracks"]
        assert inspection["filesize_bytes"] > 0
        assert inspection["video"].get("format")

        finalize_localization(*job_args)

        # The finished file is in the library, staging is empty, the source
        # is removed, and the database has the record

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

        # The video fields and track rows came from the inspection dict

        assert file.format == "AVC"
        assert [t.language for t in file.audiotrack] == ["eng"]

        # And the title lock was released

        lock = app.lock_manager.lock(file.file_identifier(), 1000)
        assert lock
        app.lock_manager.unlock(lock)

    os.remove(output)


@pytest.fixture(scope="module")
def sample_mp4(app, sample_mkv):
    """The MP4 intermediate built by the sample_mkv fixture."""

    return os.path.join(_TMP, "sample-base.mp4")


def run_full_chain(app, source):
    """Run localization -> move -> finalize, returning the enqueued names."""

    assert localization_task(source) is True
    move_jobs = [
        job
        for job in app.file_queue.jobs
        if job.func_name == "app.videos.move_localized_file"
    ]
    assert len(move_jobs) == 1
    assert videos.move_localized_file(*move_jobs[0].args) is True

    finalize_jobs = [
        job
        for job in app.sql_queue.jobs
        if job.func_name == "app.videos.finalize_localization"
    ]
    assert len(finalize_jobs) == 1
    finalize_localization(*finalize_jobs[0].args)
    return finalize_jobs[0].args


def test_non_matroska_is_converted_to_mkv(app, sample_mp4, incoming_dir):
    source = os.path.join(incoming_dir, "Converted (2021) - [DVD].mp4")
    with open(sample_mp4, "rb") as f_in, open(source, "wb") as f_out:
        f_out.write(f_in.read())

    with app.app_context():
        run_full_chain(app, source)

        # The library file and the database record both carry the .mkv name

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

        # The untouched name remembers the original container

        assert file.untouched_basename == "Converted (2021) - [DVD].mp4"

    os.remove(output)


def test_unconvertible_file_imports_as_is(app, incoming_dir):
    source = os.path.join(incoming_dir, "Garbage (2021) - [DVD].dat")
    with open(source, "wb") as f:
        f.write(b"this is not a video file at all")

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
            for job, _ in app.import_scheduler.get_jobs(with_times=True)
            if job.id.startswith("retry:")
        ]
        assert retries == [f"retry:localization_task:'{basename}'"]

        # The file was left untouched — not rejected, not staged
        assert os.path.exists(source)
        assert os.listdir(app.config["STAGING_DIR"]) == []
    finally:
        os.remove(source)


def test_staging_copy_transient_error_defers_and_retries(
    app, incoming_dir, monkeypatch
):
    """An smbfs handle revoked mid-copy (EBADF) is a mount hiccup, not a bad
    file: the partial staged copy is removed, the title lock is released,
    and the task is rescheduled instead of rejecting a healthy source."""

    basename = "Flaky (2021) - [DVD].mkv"
    source = os.path.join(incoming_dir, basename)
    with open(source, "wb") as f:
        f.write(b"video bytes")

    def revoked_handles(src, dst, job, name, activity="Copying to library"):
        with open(dst, "wb") as f:
            f.write(b"partial")
        raise OSError(errno.EBADF, "Bad file descriptor")

    monkeypatch.setattr(videos, "copy_with_progress", revoked_handles)
    try:
        with app.app_context():
            assert localization_task(source, ignore_etag=True) is True

        retries = [
            job
            for job, _ in app.import_scheduler.get_jobs(with_times=True)
            if job.id.startswith("retry:")
        ]
        assert [job.id for job in retries] == [f"retry:localization_task:'{basename}'"]

        # The retry carries the incremented attempt count and the original
        # flags, and binds to the task's signature

        job = retries[0]
        assert job.kwargs["transient_retries"] == 1
        assert job.kwargs["ignore_etag"] is True
        assert job.kwargs["file_path"] == source
        inspect.signature(localization_task).bind(
            *(job.args or ()), **(job.kwargs or {})
        )

        # Partial staged copy removed, source untouched, nothing rejected

        assert os.listdir(app.config["STAGING_DIR"]) == []
        assert os.path.exists(source)
        assert basename not in rejected_files(app)

        # The title lock was released, so the retry won't spin on it

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


def test_staging_copy_transient_error_rejects_after_max_retries(
    app, incoming_dir, monkeypatch
):
    """Once the retry budget is spent, a still-failing copy takes the
    normal reject path rather than deferring forever."""

    basename = "Hopeless (2021) - [DVD].mkv"
    source = os.path.join(incoming_dir, basename)
    with open(source, "wb") as f:
        f.write(b"video bytes")

    def revoked_handles(src, dst, job, name, activity="Copying to library"):
        raise OSError(errno.EBADF, "Bad file descriptor")

    monkeypatch.setattr(videos, "copy_with_progress", revoked_handles)

    with app.app_context():
        localization_task(source, transient_retries=videos.MAX_TRANSIENT_RETRIES)

    assert not any(
        job.id.startswith("retry:")
        for job, _ in app.import_scheduler.get_jobs(with_times=True)
    )
    assert basename in rejected_files(app)
    os.remove(os.path.join(app.config["REJECTS_DIR"], "exception", basename))


def test_staging_copy_permanent_error_rejects_immediately(
    app, incoming_dir, monkeypatch
):
    """A non-transient error (e.g. disk full) doesn't burn retries."""

    basename = "Truly Broken (2021) - [DVD].mkv"
    source = os.path.join(incoming_dir, basename)
    with open(source, "wb") as f:
        f.write(b"video bytes")

    def no_space(src, dst, job, name, activity="Copying to library"):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(videos, "copy_with_progress", no_space)

    with app.app_context():
        localization_task(source)

    assert not any(
        job.id.startswith("retry:")
        for job, _ in app.import_scheduler.get_jobs(with_times=True)
    )
    assert basename in rejected_files(app)
    os.remove(os.path.join(app.config["REJECTS_DIR"], "exception", basename))


def test_library_copy_transient_error_defers_and_keeps_lock(
    app, incoming_dir, monkeypatch
):
    """A transient mount error during the library copy retries just the
    copy: the localized output stays on staging, the title lock stays held,
    and nothing is rejected — no need to redo the whole import."""

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
            for job, _ in app.file_scheduler.get_jobs(with_times=True)
            if job.id.startswith("retry:")
        ]
        assert [job.id for job in retries] == [
            f"retry:move_localized_file:'{basename}'"
        ]

        # The retry carries the original chain — including the lock — plus
        # the incremented attempt count, and binds to the task's signature

        job = retries[0]
        assert list(job.args) == [source, file_details, "lock-sentinel", hidden]
        assert job.kwargs == {"transient_retries": 1}
        inspect.signature(videos.move_localized_file).bind(
            *(job.args or ()), **(job.kwargs or {})
        )

        # The localized output survives on staging for the retry, nothing
        # was rejected, and finalize was not enqueued

        assert os.path.exists(hidden)
        assert os.path.exists(source)
        assert basename not in rejected_files(app)
        assert len(app.sql_queue) == 0
    finally:
        os.remove(source)
        os.remove(hidden)


def test_library_copy_rejects_after_max_retries(app, incoming_dir, monkeypatch):
    """Once the retry budget is spent, the existing reject path takes over."""

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
        job.id.startswith("retry:")
        for job, _ in app.file_scheduler.get_jobs(with_times=True)
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
            "/nonexistent/source.mkv", file_details, None, "/tmp/.hidden.mkv"
        )
    assert result is False

    retries = [
        job.id
        for job, _ in app.sql_scheduler.get_jobs(with_times=True)
        if job.id.startswith("retry:finalize")
    ]
    assert retries == ["retry:finalize_localization:'Deferred (2021) - [DVD].mkv'"]


def test_move_to_rejects_survives_dead_volume(app, incoming_dir, monkeypatch):
    source = os.path.join(incoming_dir, "reject-me.mkv")
    with open(source, "wb") as f:
        f.write(b"video")

    def dead(*args, **kwargs):
        raise OSError(57, "Socket is not connected")

    monkeypatch.setattr(videos.os, "rename", dead)
    monkeypatch.setattr(videos.shutil, "copy2", dead)
    try:
        with app.app_context():
            assert move_to_rejects(source, "exception") is False
        assert os.path.exists(source)
    finally:
        os.remove(source)


def rejected_files(app):
    """All real files anywhere under the rejects directory."""

    return [
        name
        for _, _, files in os.walk(app.config["REJECTS_DIR"])
        for name in files
        if name != ".DS_Store"
    ]


def test_move_to_rejects_cross_volume_move_is_staged_atomically(
    app, incoming_dir, monkeypatch
):
    """When rename fails, the copy is staged through a hidden name and the
    source is only removed after the copy is promoted."""

    basename = "cross-volume.mkv"
    source = os.path.join(incoming_dir, basename)
    content = b"the complete file contents"
    with open(source, "wb") as f:
        f.write(content)

    def refuse_rename(src, dst):
        raise OSError(errno.EXDEV, "Cross-device link")

    monkeypatch.setattr(videos.os, "rename", refuse_rename)

    with app.app_context():
        assert move_to_rejects(source, "exception") is True

    destination = os.path.join(app.config["REJECTS_DIR"], "exception", basename)
    with open(destination, "rb") as f:
        assert f.read() == content
    assert not os.path.exists(source)
    assert not any(name.endswith(".partial") for name in rejected_files(app))
    os.remove(destination)


def test_move_to_rejects_failed_copy_leaves_no_partial(app, incoming_dir, monkeypatch):
    """A copy that dies partway leaves nothing in rejects, hidden or not."""

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

    monkeypatch.setattr(videos.os, "rename", refuse_rename)
    monkeypatch.setattr(videos.shutil, "copy2", partial_copy)
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
    """The August 4th incident: the copy completes but the source can't be
    deleted (revoked SMB handle). The state must collapse back to 'the
    source stays where it is' — no duplicate left in rejects."""

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

    monkeypatch.setattr(videos.os, "rename", refuse_rename)
    monkeypatch.setattr(videos.os, "remove", refuse_source_remove)
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

    def fake_run(command, **kwargs):
        commands.append(command)
        return FakeResult()

    monkeypatch.setattr(maintenance.subprocess, "run", fake_run)
    monkeypatch.setattr(maintenance, "volume_alive", lambda mount, timeout=10: True)
    monkeypatch.setitem(app.config, "SMB_URL_PREFIX", "smb://user@nas.local")

    with app.app_context():
        actions = maintenance.heal_mounts(["/Volumes/Movies"], app.redis, app.config)

    assert actions == ["remounted /Volumes/Movies"]
    assert commands[-1] == [
        "osascript",
        "-e",
        'mount volume "smb://user@nas.local/Movies"',
    ]

    # Cooldown prevents a remount loop
    with app.app_context():
        assert maintenance.heal_mounts(["/Volumes/Movies"], app.redis, app.config) == []


def test_heal_mounts_alert_only_without_url(app):
    with app.app_context():
        assert maintenance.heal_mounts(["/Volumes/Movies"], app.redis, app.config) == []


def test_copy_with_progress_reports_percentages(app, tmp_path):
    src = tmp_path / "big.mkv"
    dst = tmp_path / "copy.mkv"
    src.write_bytes(os.urandom(96 * 1024 * 1024))  # three chunks

    class FakeJob:
        meta = {}
        updates = []

        def save_meta(self):
            self.updates.append(dict(self.meta))

    job = FakeJob()
    with app.app_context():
        videos.copy_with_progress(str(src), str(dst), job, "big.mkv")

    assert dst.read_bytes() == src.read_bytes()
    percents = [u["progress"] for u in job.updates]
    assert percents == sorted(percents)
    assert percents[-1] == 100
    assert all(
        u["description"] == "'big.mkv' — Copying to library" for u in job.updates
    )


def test_save_track_metadata_writes_rows_and_releases_lock(app):
    from app import db
    from tests.factories import make_movie, make_movie_file

    with app.app_context():
        movie = make_movie("Metadata Test", 2021)
        file = make_movie_file(movie, "DVD")

        # Commit so save_track_metadata's own session can see the rows

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

        # The task wrote through its own session; refresh this one's view

        db.session.expire_all()

        assert file.format == "AVC"
        assert float(file.filesize_gigabytes) == 4.0
        assert [t.language for t in file.audiotrack] == ["eng"]
        assert len(file.subtrack) == 1

        # The passed lock was released
        relock = app.lock_manager.lock("save-metadata-test", 1000)
        assert relock
        app.lock_manager.unlock(relock)


def test_upload_progress_callback_dedupes_by_percent(app, tmp_path, log_capture):
    source = tmp_path / "upload.bin"
    source.write_bytes(b"x" * 1000)

    with app.app_context():
        callback = videos.UploadProgressPercentage(str(source))
        for _ in range(200):
            callback(5)  # 0.5% per call: two calls per whole percent

    messages = [
        r.getMessage() for r in log_capture if "Uploading to AWS" in r.getMessage()
    ]
    # 200 callbacks collapse to one line per distinct percentage
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
        for job, _ in app.file_scheduler.get_jobs(with_times=True)
        if job.id.startswith("retry:move_localized_file")
    ]
    assert retries == ["retry:move_localized_file:'Move Defer (2021) - [DVD].mkv'"]


def test_rename_with_retries_rides_out_transient_errors(app, tmp_path, monkeypatch):
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

    monkeypatch.setattr(videos.os, "rename", flaky_rename)
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
