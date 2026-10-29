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
        lambda path, file_id: {"rewritten": False, "video": {}},
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
        lambda path, file_id: {"rewritten": False, "video": {}},
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


def test_a_retry_after_a_newer_edit_stops(app, monkeypatch):
    """The old track arguments do not fit the rows of a newer edit (#585)."""

    import app.videos as videos
    from app import db, tracks
    from app.models import FileAudioTrack

    file_id, identifier, path = _library_row(app, "Stale Retry")
    with app.app_context():
        old = tracks._track_layout(file_id)
        db.session.add(
            FileAudioTrack(
                file_id=file_id,
                track=1,
                language="fra",
                language_name="French",
                default=True,
            )
        )
        db.session.commit()
    calls = []
    monkeypatch.setattr(
        tracks, "mkvpropedit_unlocked", lambda *args: calls.append(args) or True
    )
    try:
        assert (
            videos.mkvpropedit_task(
                file_id, "2", None, [], None, transient_retries=1, layout=old
            )
            is False
        )
        assert calls == []
        assert _lock_is_free(app, identifier)

        # The same rows let the retry run.
        with app.app_context():
            current = tracks._track_layout(file_id)
        assert (
            videos.mkvpropedit_task(
                file_id, "2", None, [], None, transient_retries=1, layout=current
            )
            is True
        )
        assert len(calls) == 1
    finally:
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
    """The retry repairs the rows. A scan first would change the layout digest."""

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
