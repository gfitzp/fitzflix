"""Test the lock, retry and staging gaps of the track tasks (#585, #583)."""

import errno
import os
from types import SimpleNamespace

import pytest

from tests.test_scheduling import requeue_id, scheduled_jobs


def _library_row(app, title):
    """Return (file_id, title lock id, library path) of a row with a file on disk."""

    from app import db
    from tests.factories import make_movie, make_movie_file

    with app.app_context():
        file = make_movie_file(make_movie(title, 2021), "Bluray-1080p")
        db.session.commit()
        path = os.path.join(app.config["LIBRARY_DIR"], file.file_path)
        identifier = file.file_identifier()
        file_id = file.id
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"mkv")
    return file_id, identifier, path


def _lock_is_free(app, identifier):
    lock = app.lock_manager.lock(identifier, 1000)
    if lock:
        app.lock_manager.unlock(lock)
    return bool(lock)


def test_a_scan_that_fails_before_the_hand_off_releases_the_lock(app, monkeypatch):
    """A Redis fault at the queue of the save left the title locked (#585)."""

    import app.tracks as tracks

    file_id, identifier, path = _library_row(app, "Scan Lock Leak")
    monkeypatch.setattr(
        tracks,
        "extract_track_metadata",
        lambda path, file_id, rewrite=True: {"rewritten": False, "video": {}},
    )

    def lost(*args, **kwargs):
        raise ConnectionError("Redis went away")

    monkeypatch.setattr(tracks, "enqueue_task", lost)
    try:
        with pytest.raises(ConnectionError):
            tracks.track_metadata_scan_task(file_id)
        assert _lock_is_free(app, identifier)
    finally:
        os.remove(path)


def test_a_handed_off_scan_keeps_the_lock_for_the_save(app, monkeypatch):
    import app.tracks as tracks

    file_id, identifier, path = _library_row(app, "Scan Hand Off")
    monkeypatch.setattr(
        tracks,
        "extract_track_metadata",
        lambda path, file_id, rewrite=True: {"rewritten": False, "video": {}},
    )
    monkeypatch.setattr(tracks, "enqueue_task", lambda *args, **kwargs: None)
    try:
        assert tracks.track_metadata_scan_task(file_id) is True
        assert not _lock_is_free(app, identifier)
    finally:
        # The clean_state fixture flushes the lock of the save hand-off.
        os.remove(path)


@pytest.mark.parametrize("in_run", [True, False])
def test_a_deferred_scan_of_a_library_run_keeps_the_run_prefix(
    app, monkeypatch, in_run
):
    """The lock-busy retry left the run. The scan button saw no run (#585)."""

    import app.tracks as tracks
    from app.tasks import bulk_job_id, bulk_run_pending

    file_id, identifier, path = _library_row(app, f"Scan Deferred {in_run}")
    job_id = bulk_job_id(tracks.TRACK_SCAN_FILE_RUN) if in_run else "manual-scan"
    monkeypatch.setattr(tracks, "get_current_job", lambda: SimpleNamespace(id=job_id))
    held = app.lock_manager.lock(identifier, 60000)
    try:
        assert tracks.track_metadata_scan_task(file_id) is True
        (retry,) = scheduled_jobs(app.file_queue)
        if in_run:
            assert retry.id.startswith(tracks.TRACK_SCAN_FILE_RUN)
            assert retry.id != job_id
        else:
            assert retry.id == requeue_id(
                "app.videos.track_metadata_scan_task", file_id
            )
        with app.app_context():
            assert (
                bulk_run_pending(
                    "bulk-track-scan-",
                    "app.videos.track_metadata_scan_library",
                    "app.videos.track_metadata_scan_task",
                )
                is in_run
            )
    finally:
        app.lock_manager.unlock(held)
        os.remove(path)


def _failing_unlocked(file_changed, unsafe):
    def run(*args, **kwargs):
        error = OSError(errno.EBADF, "Bad file descriptor")
        error.file_changed = file_changed
        error.retry_unsafe = unsafe
        raise error

    return run


def _scans(app, file_id):
    wanted = requeue_id("app.videos.track_metadata_scan_task", file_id)
    return [job for job in scheduled_jobs(app.file_queue) if job.id == wanted]


def test_an_unsafe_failure_after_a_change_queues_a_scan(app, monkeypatch):
    """No retry repairs the rows of a restructured file. A scan does (#583)."""

    import app.videos as videos
    from app import tracks

    file_id, identifier, path = _library_row(app, "Unsafe Rescan")
    monkeypatch.setattr(tracks, "mkvpropedit_unlocked", _failing_unlocked(True, True))
    try:
        with pytest.raises(OSError):
            videos.mkvpropedit_task(file_id, "2", None, [])
        (scan,) = _scans(app, file_id)
        assert scan.kwargs == {"file_id": file_id}
    finally:
        os.remove(path)


def test_a_safe_transient_retry_queues_no_scan(app, monkeypatch):
    """The retry repairs the rows. A scan is not needed before it."""

    import app.videos as videos
    from app import tracks

    file_id, identifier, path = _library_row(app, "Safe Retry No Scan")
    monkeypatch.setattr(tracks, "mkvpropedit_unlocked", _failing_unlocked(True, False))
    try:
        assert videos.mkvpropedit_task(file_id, "2", None, []) is False
        assert _scans(app, file_id) == []
    finally:
        os.remove(path)


def test_the_audio_plan_reserves_its_staging_space(app, monkeypatch):
    """The plan checked the free space only. Imports did not see it (#585)."""

    import shutil

    import app.videos as videos
    from app.importing import STAGING_RESERVED_KEY
    from app.tracks import _remux_audio_plan_unlocked

    file_id, identifier, path = _library_row(app, "Plan Reservation")
    seen = []

    def stop(src, dst, *args, **kwargs):
        seen.append(
            [
                field.decode()
                for field in app.redis.hkeys(STAGING_RESERVED_KEY)
                if field.startswith(b"audio-plan:")
            ]
        )
        raise RuntimeError("stop")

    monkeypatch.setattr(videos, "copy_with_progress", stop)
    monkeypatch.setattr(
        shutil, "disk_usage", lambda path: SimpleNamespace(total=2**60, free=2**60)
    )
    os.makedirs(app.config["STAGING_DIR"], exist_ok=True)
    try:
        with pytest.raises(RuntimeError):
            _remux_audio_plan_unlocked(file_id, [("copy", 0)])
        assert len(seen) == 1 and len(seen[0]) == 1
        assert not [
            field
            for field in app.redis.hkeys(STAGING_RESERVED_KEY)
            if field.startswith(b"audio-plan:")
        ]
    finally:
        os.remove(path)


def _fake_audio_plan(app, monkeypatch):
    """Fake the tools of the audio plan. The ffmpeg output is b"remuxed"."""

    import shutil

    import app.videos as videos
    from app import tracks

    audio = [{"format": "AC-3", "language": "eng", "language_name": "English"}]

    class General:
        track_type = "General"
        format = "Matroska"
        duration = None

    def fake_ffmpeg(command, **kwargs):
        with open(command[-1], "wb") as f:
            f.write(b"remuxed")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(
        videos, "copy_with_progress", lambda src, dst, *a, **k: shutil.copy(src, dst)
    )
    monkeypatch.setattr(
        shutil, "disk_usage", lambda path: SimpleNamespace(total=2**60, free=2**60)
    )
    monkeypatch.setattr(
        tracks.MediaInfo, "parse", lambda path: SimpleNamespace(tracks=[General()])
    )
    monkeypatch.setattr(tracks, "get_audio_tracks_from_file", lambda path: audio)
    monkeypatch.setattr(tracks, "get_subtitle_tracks_from_file", lambda path: [])
    monkeypatch.setattr(tracks.subprocess, "run", fake_ffmpeg)
    monkeypatch.setattr(tracks, "copy_image_attachments", lambda *args: None)
    monkeypatch.setattr(tracks, "enqueue_plex_analyze", lambda path: None)
    os.makedirs(app.config["STAGING_DIR"], exist_ok=True)


def test_a_failed_audio_plan_commit_after_the_replace_queues_a_scan(app, monkeypatch):
    """The plan replaced the file, and the row commit failed (#594).

    The function had only a finally. The rows kept the old layout, and
    no scan was queued."""

    from app import tracks

    file_id, identifier, path = _library_row(app, "Plan Commit Fails")
    _fake_audio_plan(app, monkeypatch)

    def lost_commit(*args, **kwargs):
        raise RuntimeError("(2013, 'Lost connection to MySQL server')")

    monkeypatch.setattr(tracks, "replace_track_rows", lost_commit)
    try:
        with pytest.raises(RuntimeError, match="Lost connection"):
            tracks._remux_audio_plan_unlocked(file_id, [("copy", 0)])
        assert open(path, "rb").read() == b"remuxed"
        (scan,) = _scans(app, file_id)
        assert scan.kwargs == {"file_id": file_id}
    finally:
        os.remove(path)


def test_a_failed_reservation_release_does_not_fail_a_committed_plan(app, monkeypatch):
    """A Redis fault in the release of the finally failed a done plan (#596)."""

    import app.importing as importing
    from app import tracks

    file_id, identifier, path = _library_row(app, "Plan Release Fails")
    _fake_audio_plan(app, monkeypatch)

    def lost(reservation):
        raise ConnectionError("Redis went away")

    monkeypatch.setattr(importing, "release_staging_reservation", lost)
    try:
        assert tracks._remux_audio_plan_unlocked(file_id, [("copy", 0)]) is True
    finally:
        os.remove(path)


def test_an_audio_plan_with_no_reserved_space_stops_before_the_copy(app, monkeypatch):
    import app.importing as importing
    import app.videos as videos
    from app.tracks import _remux_audio_plan_unlocked

    file_id, identifier, path = _library_row(app, "Plan No Space")
    copies = []
    monkeypatch.setattr(importing, "reserve_staging", lambda *args: "wait")
    monkeypatch.setattr(videos, "copy_with_progress", lambda *a, **k: copies.append(a))
    try:
        with pytest.raises(RuntimeError, match="staging space"):
            _remux_audio_plan_unlocked(file_id, [("copy", 0)])
        assert copies == []
    finally:
        os.remove(path)


def test_a_retry_that_stops_before_the_tool_still_queues_the_scan(app, monkeypatch):
    """The retry forgot that the first attempt wrote the file (#583 follow-up).

    Attempt 1 wrote the flags and failed on a transient read. Its safe
    retry failed before the tool started. Nothing queued the scan, and
    the rows kept the old values for good."""

    import app.videos as videos
    from app import tracks

    file_id, identifier, path = _library_row(app, "Forgotten Write")

    def no_file(*args, **kwargs):
        raise OSError(errno.ENOENT, "No such file")

    monkeypatch.setattr(tracks, "mkvpropedit_unlocked", no_file)
    try:
        with pytest.raises(OSError):
            videos.mkvpropedit_task(
                file_id, "2", None, [], None, transient_retries=1, file_changed=True
            )
        (scan,) = _scans(app, file_id)
        assert scan.kwargs == {"file_id": file_id}
        assert _lock_is_free(app, identifier)
    finally:
        os.remove(path)


def test_a_safe_retry_carries_the_earlier_write(app, monkeypatch):
    """The transient retry remembers that the file changed (#583 follow-up)."""

    import app.videos as videos
    from app import tracks

    file_id, identifier, path = _library_row(app, "Carried Write")
    monkeypatch.setattr(tracks, "mkvpropedit_unlocked", _failing_unlocked(True, False))
    try:
        assert videos.mkvpropedit_task(file_id, "2", None, []) is False
        retries = [
            job
            for job in scheduled_jobs(app.file_queue)
            if job.func_name.endswith("mkvpropedit_task") and job.args[0] == file_id
        ]
        assert len(retries) == 1
        assert retries[0].kwargs["file_changed"] is True
        for job in retries:
            job.delete()
    finally:
        os.remove(path)


def test_a_failed_reimport_commit_over_a_replaced_file_queues_a_scan(
    app, sample_mkv, incoming_dir, monkeypatch
):
    """The row kept the tracks, the size and the archive of the old file (#592 follow-up).

    The new file replaced the file of the row, and the commit failed. No
    scan was queued, and the archive was not flagged stale."""

    from app import db, importing
    from app.models import File
    from tests.factories import make_movie, make_movie_file
    from tests.localization_helpers import run_full_chain

    with app.app_context():
        row = make_movie_file(make_movie("Replaced Commit Scan", 2021), "DVD")
        db.session.commit()
        row_id = row.id
        library = os.path.join(app.config["LIBRARY_DIR"], row.file_path)
    os.makedirs(os.path.dirname(library), exist_ok=True)
    with open(library, "wb") as f:
        f.write(b"old copy")

    class Failing:
        """A HeldImport stand-in whose cleanup delete fails, as a commit can."""

        class query:
            @staticmethod
            def filter_by(**kwargs):
                raise RuntimeError("simulated failure before the commit")

    source = os.path.join(incoming_dir, "Replaced Commit Scan (2021) - [DVD].mkv")
    with open(sample_mkv, "rb") as f_in, open(source, "wb") as f_out:
        f_out.write(f_in.read())
    monkeypatch.setattr(importing, "HeldImport", Failing)
    try:
        with app.app_context():
            run_full_chain(app, source)
            db.session.expire_all()
            fresh = db.session.get(File, row_id)
            assert fresh.aws_stale is True
        scans = [
            job
            for job in scheduled_jobs(app.file_queue)
            if job.func_name.endswith("track_metadata_scan_task")
            and job.kwargs.get("file_id") == row_id
        ]
        assert len(scans) == 1
    finally:
        for path in (library, source):
            if os.path.exists(path):
                os.remove(path)
        rejected = os.path.join(
            app.config["REJECTS_DIR"], "exception", os.path.basename(source)
        )
        if os.path.exists(rejected):
            os.remove(rejected)


# 2 different edits of 1 file wait apart (#595, #611 follow-up). The
# lock wait id and the transient retry id hold a digest of the edit
# arguments. Under a fixed id, the second edit replaced the first.

DEFERRED_EDITS = [
    (
        "mkvpropedit_task",
        "file_queue",
        [("111", "0", []), ("222", "0", ["333"])],
    ),
    ("mkvmerge_task", "import_queue", [(["1"], []), (["2"], [])]),
    (
        "remux_audio_plan_task",
        "transcode_queue",
        [([["copy", 1]],), ([["flac", 1], ["copy", 1]],)],
    ),
]


@pytest.mark.parametrize("task, queue, edits", DEFERRED_EDITS)
def test_two_different_deferred_edits_both_wait(app, held_lock, task, queue, edits):
    from app import tracks

    file_id, identifier, path = _library_row(app, f"Two Edits {task[:9]}")
    held_lock(identifier)
    try:
        with app.app_context():
            for edit in edits:
                assert getattr(tracks, task)(file_id, *edit) is True
        jobs = [
            job
            for job in scheduled_jobs(getattr(app, queue))
            if job.func_name == f"app.videos.{task}"
        ]
        posted = sorted(repr(list(job.args[1 : 1 + len(edits[0])])) for job in jobs)
        assert posted == sorted(repr(list(edit)) for edit in edits)
    finally:
        os.remove(path)


def test_a_repeat_deferral_of_one_edit_still_replaces_its_wait(app, held_lock):
    from app import tracks

    file_id, identifier, path = _library_row(app, "Repeat Edit")
    held_lock(identifier)
    try:
        with app.app_context():
            tracks.mkvpropedit_task(file_id, "111", "0", [])
            tracks.mkvpropedit_task(file_id, "111", "0", [])
        jobs = [
            job
            for job in scheduled_jobs(app.file_queue)
            if job.func_name == "app.videos.mkvpropedit_task"
        ]
        assert len(jobs) == 1
    finally:
        os.remove(path)


def test_two_transient_retries_of_different_edits_both_wait(app, monkeypatch):
    from app import tracks

    def flaky(*args, **kwargs):
        raise OSError(errno.EIO, "simulated share fault")

    monkeypatch.setattr(tracks, "mkvpropedit_unlocked", flaky)
    monkeypatch.setattr(tracks, "probe_and_record", lambda *args, **kwargs: None)
    file_id, _, path = _library_row(app, "Two Retries")
    try:
        with app.app_context():
            assert tracks.mkvpropedit_task(file_id, "111", "0", []) is False
            assert tracks.mkvpropedit_task(file_id, "222", "0", []) is False
        jobs = [
            job
            for job in scheduled_jobs(app.file_queue)
            if job.func_name == "app.videos.mkvpropedit_task"
        ]
        assert sorted(job.args[1] for job in jobs) == ["111", "222"]
        assert all(job.kwargs == {"transient_retries": 1} for job in jobs)
    finally:
        os.remove(path)
