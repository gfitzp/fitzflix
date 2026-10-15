"""Test the staged copy that an import gives to its Atmos job.

The import copies a file from staging into the library. Before, the
Atmos job then copied the same file back from the NAS (Gandhi: 8 min
41 s). Now the job starts from the staged copy, if the library file did
not change since the import."""

import os

from tests.factories import make_movie, make_movie_file


class _Phases:
    """Record the phases that the Atmos job enters."""

    def __init__(self):
        self.entered = []

    def enter(self, stage):
        self.entered.append(stage)

    def finish(self):
        pass

    def fail(self):
        pass


def _library_and_staged(app, title):
    """Make a File row, its library file, and a staged copy with the same bytes."""

    from app import db

    movie = make_movie(title, 2021)
    file = make_movie_file(movie, "Bluray-1080p")
    db.session.commit()
    library = os.path.join(app.config["LIBRARY_DIR"], file.file_path)
    os.makedirs(os.path.dirname(library), exist_ok=True)
    with open(library, "wb") as f:
        f.write(b"x" * 4096)
    os.makedirs(app.config["STAGING_DIR"], exist_ok=True)
    staged = os.path.join(app.config["STAGING_DIR"], f".{file.basename}")
    with open(staged, "wb") as f:
        f.write(b"x" * 4096)
    return file, library, staged


def _no_network(monkeypatch):
    """Stop the Atmos job after its staging step, with no AWS calls."""

    import app.atmos as atmos
    import app.videos as videos

    monkeypatch.setattr(atmos, "STAGED_HANDOFF_RESERVE_BYTES", 0)
    monkeypatch.setattr(atmos, "WORKSPACE_HEADROOM_BYTES", 0)
    monkeypatch.setattr(atmos, "_cleanup_s3_prefix", lambda *args: None)
    monkeypatch.setattr(videos, "aws_s3_client", lambda *args, **kwargs: None)

    # The staged file has no Atmos track. Thus, the job ends right after
    # its staging step, which is the step under test.

    monkeypatch.setattr(videos, "get_audio_tracks_from_file", lambda path: [])


def test_a_staged_copy_matches_until_the_library_file_changes(app, monkeypatch):
    """Use the staged copy only while the library file is the same.

    A track flag edit between the import and the Atmos run changes the
    library file. A start from the old staged copy would undo the edit."""

    import app.atmos as atmos

    monkeypatch.setattr(atmos, "STAGED_HANDOFF_RESERVE_BYTES", 0)
    with app.app_context():
        file, library, staged = _library_and_staged(app, "Handoff Match")
        handoff = atmos.staged_handoff(staged, library)
        assert handoff["path"] == staged
        assert atmos._usable_staged_source(handoff, library)

        stat = os.stat(library)
        os.utime(library, (stat.st_atime, stat.st_mtime + 60))
        assert not atmos._usable_staged_source(handoff, library)
    os.remove(staged)


def test_the_atmos_job_starts_from_the_staged_copy(app, monkeypatch):
    """Move the staged copy into the workspace. Do not read the NAS."""

    import app.atmos as atmos
    import app.videos as videos

    _no_network(monkeypatch)

    def no_copy(*args, **kwargs):
        raise AssertionError("the job copied the file from the library")

    monkeypatch.setattr(videos, "copy_with_progress", no_copy)
    phases = _Phases()
    with app.app_context():
        file, library, staged = _library_and_staged(app, "Handoff Use")
        handoff = atmos.staged_handoff(staged, library)
        assert atmos._atmos_prepare(file.id, phases, handoff) is True

    # The rename moved the copy into the workspace. The workspace is gone.
    # The library file is untouched. No "Copying to staging" chip shows.

    assert not os.path.exists(staged)
    assert os.path.exists(library)
    assert "Copying to staging" not in phases.entered


def test_a_changed_library_file_is_copied_again(app, monkeypatch):
    """Remove a stale staged copy and copy the library file as before."""

    import shutil

    import app.atmos as atmos
    import app.videos as videos

    _no_network(monkeypatch)
    copied = []

    def fake_copy(src, dst, job, name, activity="Moving to library"):
        copied.append(src)
        shutil.copyfile(src, dst)

    monkeypatch.setattr(videos, "copy_with_progress", fake_copy)
    phases = _Phases()
    with app.app_context():
        file, library, staged = _library_and_staged(app, "Handoff Stale")
        handoff = atmos.staged_handoff(staged, library)
        stat = os.stat(library)
        os.utime(library, (stat.st_atime, stat.st_mtime + 60))
        assert atmos._atmos_prepare(file.id, phases, handoff) is True

    assert copied == [library]
    assert not os.path.exists(staged)
    assert phases.entered == ["Copying to staging"]


def test_a_deferred_atmos_job_keeps_its_staged_copy(app):
    """Carry the staged copy through a deferral. Do not remove it."""

    from rq.registry import ScheduledJobRegistry

    import app.atmos as atmos

    with app.app_context():
        file, library, staged = _library_and_staged(app, "Handoff Defer")
        handoff = {
            "path": staged,
            "size": os.path.getsize(library),
            "mtime": os.stat(library).st_mtime,
        }
        app.request_queue.enqueue(
            "app.videos.refresh_tmdb_info",
            args=("Movies", file.movie_id, None),
            description="Refreshing TMDB data for 'Handoff Defer (2021)'",
        )
        assert atmos.atmos_supplement_task(file.id, staged_source=handoff) is True

        registry = ScheduledJobRegistry(queue=app.import_queue)
        (job_id,) = registry.get_job_ids()
        job = app.import_queue.fetch_job(job_id)
        assert job.kwargs == {"staged_source": handoff}
    assert os.path.exists(staged)
    os.remove(staged)


def test_a_gone_file_row_removes_the_staged_copy(app):
    """Remove the staged copy when the File row of the job is gone."""

    import app.atmos as atmos

    with app.app_context():
        os.makedirs(app.config["STAGING_DIR"], exist_ok=True)
        staged = os.path.join(app.config["STAGING_DIR"], ".Gone (2021).mkv")
        with open(staged, "wb") as f:
            f.write(b"x")
        handoff = {"path": staged, "size": 1, "mtime": 0}
        assert atmos.atmos_supplement_task(999999, staged_source=handoff) is True
    assert not os.path.exists(staged)
