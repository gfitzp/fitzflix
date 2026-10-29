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


def _first_attempt_fails(app, monkeypatch, file_id):
    """Run attempt A, which fails with a transient fault. Return its retry job."""

    import app.videos as videos
    from app import tracks

    with monkeypatch.context() as patch:
        patch.setattr(tracks, "mkvpropedit_unlocked", _failing_unlocked(False, False))
        assert videos.mkvpropedit_task(file_id, "2", None, []) is False
    (retry,) = [
        job
        for job in scheduled_jobs(app.file_queue)
        if job.func_name.endswith("mkvpropedit_task") and job.args[0] == file_id
    ]
    return retry


def _write_rows(app, file_id, default_track, bump):
    """Write 2 audio rows with default_track as the default. Count an edit if bump."""

    from app import db
    from app.models import File
    from app.tracks import replace_track_rows

    with app.app_context():
        file = db.session.get(File, file_id)
        replace_track_rows(
            file,
            [
                {
                    "language": "eng",
                    "language_name": "English",
                    "default": default_track == 1,
                },
                {
                    "language": "fra",
                    "language_name": "French",
                    "default": default_track == 2,
                },
            ],
            [],
        )
        if bump:
            file.note_track_edit()
        db.session.commit()


def _edit_seq(app, file_id):
    from app import db
    from app.models import File

    with app.app_context():
        return db.session.get(File, file_id).track_edit_seq


def test_a_retry_after_a_newer_edit_with_the_same_rows_stops(app, monkeypatch):
    """The digest of the rows missed an edit back to the old rows (ABA, #589).

    Attempt A set track 2 as the default and failed after the write. The
    page still showed track 1, and edit B posted track 1. B committed the
    same rows as before A. The retry of A then set track 2 over B."""

    import app.videos as videos
    from app import tracks

    file_id, identifier, path = _library_row(app, "Stale Retry")
    _write_rows(app, file_id, 1, bump=False)
    retry = _first_attempt_fails(app, monkeypatch, file_id)
    kwargs = dict(retry.kwargs)
    retry.delete()
    assert kwargs["edit_seq"] == 0

    # Edit B commits the rows of before A and counts its edit.
    _write_rows(app, file_id, 1, bump=True)
    assert _edit_seq(app, file_id) == 1

    calls = []
    monkeypatch.setattr(
        tracks, "mkvpropedit_unlocked", lambda *args: calls.append(args) or True
    )
    try:
        assert videos.mkvpropedit_task(file_id, *retry.args[1:], **kwargs) is False
        assert calls == []
        assert _lock_is_free(app, identifier)

        # The current count lets the retry run.
        kwargs["edit_seq"] = 1
        assert videos.mkvpropedit_task(file_id, *retry.args[1:], **kwargs) is True
        assert len(calls) == 1
    finally:
        os.remove(path)


def test_a_scan_in_the_retry_delay_does_not_stop_the_retry(app, monkeypatch):
    """A scan changed the digest, and the rest of the edit was lost (#589).

    The scan rewrites the rows from the half-edited file. It does not
    count an edit. Thus, the retry still runs."""

    import app.videos as videos
    from app import tracks

    file_id, identifier, path = _library_row(app, "Scan In Delay")
    _write_rows(app, file_id, 1, bump=False)
    retry = _first_attempt_fails(app, monkeypatch, file_id)
    kwargs = dict(retry.kwargs)
    retry.delete()

    with app.app_context():
        assert tracks.save_track_metadata(
            file_id,
            {
                "video": {},
                "filesize_bytes": 3,
                "audio_tracks": [
                    {"language": "fra", "language_name": "French", "default": True},
                    {"language": "eng", "language_name": "English", "default": False},
                ],
                "subtitle_tracks": [],
            },
        )
    assert _edit_seq(app, file_id) is None

    calls = []
    monkeypatch.setattr(
        tracks, "mkvpropedit_unlocked", lambda *args: calls.append(args) or True
    )
    try:
        assert videos.mkvpropedit_task(file_id, *retry.args[1:], **kwargs) is True
        assert len(calls) == 1
    finally:
        os.remove(path)


def test_a_track_edit_counts_once_and_a_scan_does_not_count(app):
    """Each edit adds exactly 1 to the counter. A scan leaves it (#589)."""

    from app import db, tracks
    from app.models import File

    file_id, identifier, path = _library_row(app, "Edit Counter")
    try:
        with app.app_context():
            file = db.session.get(File, file_id)
            assert file.track_edit_seq is None
            file.note_track_edit()
            db.session.commit()
            assert db.session.get(File, file_id).track_edit_seq == 1
            file = db.session.get(File, file_id)
            file.note_track_edit()
            db.session.commit()
            assert db.session.get(File, file_id).track_edit_seq == 2
            details = {
                "video": {},
                "filesize_bytes": 3,
                "audio_tracks": [
                    {"language": "eng", "language_name": "English", "default": True}
                ],
                "subtitle_tracks": [],
            }
            assert tracks.save_track_metadata(file_id, details)
            db.session.expire_all()
            assert db.session.get(File, file_id).track_edit_seq == 2
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


def test_a_retry_stopped_by_a_newer_edit_queues_the_scan_for_its_write(
    app, monkeypatch
):
    """A newer edit stops the retry. The earlier write still needs the scan."""

    import app.videos as videos
    from app import db, tracks
    from app.models import File

    file_id, identifier, path = _library_row(app, "Stopped After Write")
    with app.app_context():
        file = db.session.get(File, file_id)
        file.note_track_edit()
        db.session.commit()
    calls = []
    monkeypatch.setattr(
        tracks, "mkvpropedit_unlocked", lambda *args: calls.append(args) or True
    )
    try:
        assert (
            videos.mkvpropedit_task(
                file_id,
                "2",
                None,
                [],
                None,
                transient_retries=1,
                edit_seq=0,
                file_changed=True,
            )
            is False
        )
        assert calls == []
        (scan,) = _scans(app, file_id)
        assert scan.kwargs == {"file_id": file_id}
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
        assert retries[0].kwargs["edit_seq"] == 0
        for job in retries:
            job.delete()
    finally:
        os.remove(path)


def test_a_failed_edit_that_changed_the_file_counts_the_edit(app, monkeypatch):
    """The file changed and the rows did not commit. The counter still moves (#592)."""

    import app.videos as videos
    from app import tracks

    file_id, identifier, path = _library_row(app, "Counted Failure")
    monkeypatch.setattr(tracks, "mkvpropedit_unlocked", _failing_unlocked(True, True))
    try:
        with pytest.raises(OSError):
            videos.mkvpropedit_task(file_id, "2", None, [])
        assert _edit_seq(app, file_id) == 1
    finally:
        os.remove(path)


def test_a_safe_transient_retry_counts_no_edit(app, monkeypatch):
    """The retry carries the counter. A count here would stop the retry itself."""

    import app.videos as videos
    from app import tracks

    file_id, identifier, path = _library_row(app, "Uncounted Retry")
    monkeypatch.setattr(tracks, "mkvpropedit_unlocked", _failing_unlocked(True, False))
    try:
        assert videos.mkvpropedit_task(file_id, "2", None, []) is False
        assert not _edit_seq(app, file_id)
    finally:
        os.remove(path)


def test_a_retry_after_a_failed_remux_that_changed_the_file_stops(app, monkeypatch):
    """Edit B replaced the file and failed its commit. The retry of A stops (#592).

    The retry of A has the counter of the old layout. The remux of B
    renumbered the tracks. Before, B counted nothing, and A set its
    default flag on the wrong track."""

    import app.videos as videos
    from app import tracks

    file_id, identifier, path = _library_row(app, "Remux Then Retry")
    retry = _first_attempt_fails(app, monkeypatch, file_id)
    assert retry.kwargs["edit_seq"] == 0

    class Track:
        def __init__(self, kind, order):
            self.track_type = kind
            self.order = order

        def to_data(self):
            return {"streamorder": self.order}

    media = SimpleNamespace(tracks=[Track("Video", 0), Track("Audio", 1)])
    calls = []

    def lost_read(*args, **kwargs):
        raise RuntimeError("MediaInfo read failed")

    with monkeypatch.context() as patch:
        patch.setattr(tracks.MediaInfo, "parse", lambda path: media)
        patch.setattr(tracks, "remux_file", lambda *args, **kwargs: None)
        patch.setattr(tracks, "remove_empty_subtitle_tracks", lost_read)
        with pytest.raises(RuntimeError):
            tracks.mkvmerge_unlocked(file_id, [1], [])
    assert _edit_seq(app, file_id) == 1

    monkeypatch.setattr(
        tracks, "mkvpropedit_unlocked", lambda *args: calls.append(args) or True
    )
    try:
        assert videos.mkvpropedit_task(*retry.args, **retry.kwargs) is False
        assert calls == []
    finally:
        retry.delete()
        os.remove(path)


def test_a_failed_reimport_commit_over_a_replaced_file_counts_the_edit(
    app, sample_mkv, incoming_dir, monkeypatch
):
    """The new file replaced the file of the row, and the commit failed (#592)."""

    from app import db, importing
    from app.models import File
    from tests.factories import make_movie, make_movie_file
    from tests.localization_helpers import run_full_chain

    with app.app_context():
        row = make_movie_file(make_movie("Replaced Commit Fail", 2021), "DVD")
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

    source = os.path.join(incoming_dir, "Replaced Commit Fail (2021) - [DVD].mkv")
    with open(sample_mkv, "rb") as f_in, open(source, "wb") as f_out:
        f_out.write(f_in.read())
    monkeypatch.setattr(importing, "HeldImport", Failing)
    try:
        with app.app_context():
            run_full_chain(app, source)
            db.session.expire_all()
            assert db.session.get(File, row_id).track_edit_seq == 1
    finally:
        for path in (library, source):
            if os.path.exists(path):
                os.remove(path)
        rejected = os.path.join(
            app.config["REJECTS_DIR"], "exception", os.path.basename(source)
        )
        if os.path.exists(rejected):
            os.remove(rejected)


# Each function that commits new track rows of an existing file outside a
# scan, with the count of its note_track_edit calls (#589). _atmos_finish
# has a second commit for a first commit that failed. A scan, the Atmos
# rebuild of the rows from the file, and the restore placement count no
# edit.

TRACK_EDIT_SITES = [
    ("app/tracks.py", "mkvpropedit_unlocked", 1),
    ("app/tracks.py", "mkvmerge_unlocked", 1),
    ("app/tracks.py", "_remux_audio_plan_unlocked", 1),
    ("app/atmos.py", "_atmos_finish", 2),
    ("app/importing.py", "_build_records", 1),
    ("app/tracks.py", "save_track_metadata", 0),
    ("app/atmos.py", "_atmos_prepare", 0),
    ("app/aws_storage.py", "place_in_library", 0),
]


# Each function whose handlers know that the file changed with no row
# commit, with the count of its calls that count the edit apart (#592).
# _rescan_after_a_failed_edit counts the edit, and count_failed_track_edit
# is the separate transaction. A scan and the restore placement have none.

FAILED_EDIT_SITES = [
    ("app/tracks.py", "_rescan_after_a_failed_edit", 1),
    ("app/tracks.py", "mkvpropedit_task", 2),
    ("app/tracks.py", "mkvpropedit_unlocked", 1),
    ("app/tracks.py", "mkvmerge_unlocked", 2),
    ("app/atmos.py", "_atmos_finish", 1),
    ("app/importing.py", "finalize_localization", 1),
    ("app/tracks.py", "save_track_metadata", 0),
    ("app/atmos.py", "_atmos_prepare", 0),
    ("app/aws_storage.py", "place_in_library", 0),
]

FAILED_EDIT_CALLS = ("_rescan_after_a_failed_edit", "count_failed_track_edit")


def _calls(path, function, names):
    """Return the count of the calls to names in one function."""

    import ast

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tree = ast.parse(open(os.path.join(root, path)).read())
    (node,) = [
        child
        for child in ast.walk(tree)
        if isinstance(child, ast.FunctionDef) and child.name == function
    ]

    def name_of(func):
        if isinstance(func, ast.Attribute):
            return func.attr
        if isinstance(func, ast.Name):
            return func.id
        return None

    return sum(
        1
        for call in ast.walk(node)
        if isinstance(call, ast.Call) and name_of(call.func) in names
    )


def _note_calls(path, function):
    """Return the count of the note_track_edit calls in one function."""

    return _calls(path, function, ("note_track_edit",))


@pytest.mark.parametrize("path, function, count", TRACK_EDIT_SITES)
def test_each_track_edit_site_counts_its_edit(path, function, count):
    assert (
        _note_calls(path, function) == count
    ), f"{path}:{function} must call note_track_edit {count} times (#589)"


@pytest.mark.parametrize("path, function, count", FAILED_EDIT_SITES)
def test_each_failed_edit_handler_counts_the_edit_apart(path, function, count):
    assert (
        _calls(path, function, FAILED_EDIT_CALLS) == count
    ), f"{path}:{function} must count a failed edit {count} times (#592)"


def test_a_real_flag_edit_adds_exactly_1_to_the_counter(app, tmp_path):
    """The commit of mkvpropedit_unlocked counts the edit (#589)."""

    import shutil

    from app import db
    from app.models import File
    from app.tracks import mkvpropedit_unlocked
    from tests.media import make_sample_mkv

    sample = make_sample_mkv(
        str(tmp_path / "two-audio.mkv"), audio=(("eng", True), ("fra", False))
    )
    file_id, identifier, path = _library_row(app, "Real Flag Edit")
    shutil.copy(sample, path)
    try:
        with app.app_context():
            assert mkvpropedit_unlocked(file_id, 1, None, None) is True
            db.session.expire_all()
            assert db.session.get(File, file_id).track_edit_seq == 1
    finally:
        os.remove(path)
