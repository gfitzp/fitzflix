"""Test the real Atmos finish job: its retries (#502) and its row repair (#503).

The tools and AWS are fakes. The remux writes a stand-in output. The
track scan of that output shows the twin."""

import os

import pytest

from tests.test_atmos_chain import ATMOS, _scheduled, _setup

TWIN = "Dolby Digital Plus with Dolby Atmos"


class _Track:
    def __init__(self, track_type, order):
        self.track_type = track_type
        self._order = order

    def to_data(self):
        return {"streamorder": self._order}


class _MediaInfo:
    tracks = [_Track("Video", 0), _Track("Audio", 1)]


class _Process:
    returncode = 0
    args = ["mkvmerge"]

    def wait(self):
        return 0


def _finish_fakes(monkeypatch):
    """Fake the remux tools. The output of mkvmerge is its -o argument."""

    import app.atmos as atmos
    import app.videos as videos

    def fake_popen(command, **kwargs):
        with open(command[command.index("-o") + 1], "wb") as f:
            f.write(b"twinned")
        return _Process()

    def tracks_of(path):
        if os.path.basename(path).startswith(".") or open(path, "rb").read() == (
            b"twinned"
        ):
            return [
                {"codec": TWIN, "language": "eng", "language_name": "English"},
                {"codec": ATMOS, "language": "eng", "language_name": "English"},
            ]
        return [{"codec": ATMOS, "language": "eng", "streamorder": 1}]

    monkeypatch.setattr(atmos.MediaInfo, "parse", lambda path: _MediaInfo())
    monkeypatch.setattr(atmos.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(videos, "watch_mkvmerge_progress", lambda *a, **k: None)
    monkeypatch.setattr(videos, "remove_empty_subtitle_tracks", lambda path: None)
    monkeypatch.setattr(videos, "get_audio_tracks_from_file", tracks_of)
    monkeypatch.setattr(videos, "get_subtitle_tracks_from_file", lambda path: [])
    monkeypatch.setattr(atmos, "_run_step", lambda *a, **k: None)
    monkeypatch.setattr(atmos, "enqueue_plex_analyze", lambda path: None)
    monkeypatch.setattr(atmos, "get_setting", lambda name: False)


def _finish_job(app, monkeypatch, title):
    """Run the first job and a complete check. Return (file, finish job)."""

    import app.atmos as atmos

    file, job, mediaconvert = _setup(app, monkeypatch, title)
    assert atmos.atmos_supplement_task(file.id) is True
    (check,) = _scheduled(app, "app.atmos.atmos_check_task")
    mediaconvert.status = {"Status": "COMPLETE"}
    assert atmos.atmos_check_task(*check.args, **check.kwargs) is True
    (finish,) = [
        j
        for j in app.handoff_queue.jobs
        if j.func_name == "app.atmos.atmos_finish_task"
    ]
    _finish_fakes(monkeypatch)
    return file, finish


def test_a_copy_that_fails_once_books_the_finish_again(app, monkeypatch):
    import app.atmos as atmos
    import app.videos as videos
    from app import db
    from app.models import FileAudioTrack

    with app.app_context():
        file, finish = _finish_job(app, monkeypatch, "Finish Retry")
        real_copy = videos.copy_with_progress
        calls = []

        def copy_once(src, dst, *args, **kwargs):
            calls.append(src)
            if len(calls) == 1:
                raise OSError(6, "Device not configured")
            return real_copy(src, dst, *args, **kwargs)

        monkeypatch.setattr(videos, "copy_with_progress", copy_once)

        assert atmos.atmos_finish_task(*finish.args) is False

        # The paid encode, the workspace, the lock and the chain stay.
        state = finish.args[0]
        assert os.path.exists(state["workspace"])
        assert app.redis.exists(atmos.CHAIN_KEY.format(file_id=file.id))
        assert not app.lock_manager.lock(file.file_identifier(), 1000)
        (retry,) = _scheduled(app, "app.atmos.atmos_finish_task")
        assert retry.kwargs == {"finish_retries": 1}
        assert retry.args[0]["remuxed"] is True

        # The retry copies the verified output. It does not remux again.
        assert atmos.atmos_finish_task(*retry.args, **retry.kwargs) is True
        library = os.path.join(app.config["LIBRARY_DIR"], file.file_path)
        assert open(library, "rb").read() == b"twinned"
        codecs = [
            row.codec
            for row in FileAudioTrack.query.filter_by(file_id=file.id).order_by(
                FileAudioTrack.track
            )
        ]
        assert codecs == [TWIN, ATMOS]
        assert not os.path.exists(state["workspace"])
        assert not app.redis.exists(atmos.CHAIN_KEY.format(file_id=file.id))
        db.session.rollback()


def test_the_last_failed_retry_ends_the_run(app, monkeypatch):
    import app.atmos as atmos
    import app.videos as videos

    with app.app_context():
        file, finish = _finish_job(app, monkeypatch, "Finish Give Up")

        def broken(*args, **kwargs):
            raise OSError(6, "Device not configured")

        monkeypatch.setattr(videos, "copy_with_progress", broken)
        with pytest.raises(atmos.FinishInterrupted):
            atmos.atmos_finish_task(
                *finish.args, finish_retries=atmos.MAX_FINISH_RETRIES
            )
        assert not os.path.exists(finish.args[0]["workspace"])
        assert not app.redis.exists(atmos.CHAIN_KEY.format(file_id=file.id))


def test_a_db_fault_after_the_rename_marks_the_archive_stale_and_fixes_the_rows(
    app, monkeypatch
):
    """The rows follow the new library file, and the archive is stale (#503)."""

    import app.atmos as atmos
    import app.tracks as tracks
    from app import db
    from app.models import File, FileAudioTrack

    with app.app_context():
        file, finish = _finish_job(app, monkeypatch, "Finish Rows")
        real_replace = tracks.replace_track_rows
        calls = []

        def fail_once(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("(1020, 'Record has changed since last read')")
            return real_replace(*args, **kwargs)

        monkeypatch.setattr(tracks, "replace_track_rows", fail_once)
        assert atmos.atmos_finish_task(*finish.args) is True

        db.session.expire_all()
        assert db.session.get(File, file.id).aws_stale is True
        codecs = [r.codec for r in FileAudioTrack.query.filter_by(file_id=file.id)]
        assert sorted(codecs) == sorted([TWIN, ATMOS])


def test_a_file_that_has_its_twins_gets_its_rows_back(app, monkeypatch):
    """The rows say no twin, the staged copy has it. The rows follow the file (#503)."""

    import app.atmos as atmos
    import app.videos as videos
    from app.models import FileAudioTrack

    with app.app_context():
        file, job, _ = _setup(app, monkeypatch, "Rows Back")
        monkeypatch.setattr(
            videos,
            "get_audio_tracks_from_file",
            lambda path: [
                {"codec": TWIN, "language": "eng", "language_name": "English"},
                {"codec": ATMOS, "language": "eng", "language_name": "English"},
            ],
        )
        monkeypatch.setattr(videos, "get_subtitle_tracks_from_file", lambda path: [])

        assert atmos.atmos_supplement_task(file.id) is True

        codecs = [
            row.codec
            for row in FileAudioTrack.query.filter_by(file_id=file.id).order_by(
                FileAudioTrack.track
            )
        ]
        assert codecs == [TWIN, ATMOS]


def test_a_finished_job_under_the_fixed_id_gives_no_expiry_to_the_new_one(
    app, monkeypatch
):
    """The fixed id is freed before the enqueue (#504)."""

    import app.atmos as atmos
    from rq.job import JobStatus

    with app.app_context():
        file, job, _ = _setup(app, monkeypatch, "Fixed Id")
        app.import_queue.remove(job)
        job.set_status(JobStatus.FINISHED)
        app.redis.expire(job.key, 500)

        assert atmos.maybe_enqueue_atmos_supplement(file.id) is True
        assert app.redis.ttl(job.key) == -1


def test_a_failed_remove_after_the_submit_still_cancels_the_encode(app, monkeypatch):
    """The encode is in the list that the cleanup cancels (#504)."""

    import app.atmos as atmos

    with app.app_context():
        file, job, mediaconvert = _setup(app, monkeypatch, "Remove Fault")
        real_remove = os.remove

        def remove(path):
            if ".atmos" in os.path.basename(path):
                raise OSError(6, "Device not configured")
            return real_remove(path)

        monkeypatch.setattr(atmos.os, "remove", remove)
        with pytest.raises(OSError):
            atmos.atmos_supplement_task(file.id)
        assert getattr(mediaconvert, "canceled", []) == ["mc-1"]


def test_an_unexpected_fault_in_a_check_books_the_next_check(app, monkeypatch):
    """The chain keeps its owner after a fault in the check body (#505)."""

    import app.atmos as atmos

    with app.app_context():
        file, job, mediaconvert = _setup(app, monkeypatch, "Check Fault")
        assert atmos.atmos_supplement_task(file.id) is True
        (check,) = _scheduled(app, "app.atmos.atmos_check_task")

        def broken(*args, **kwargs):
            raise ConnectionError("redis blip")

        monkeypatch.setattr(atmos, "_set_stage", broken)
        assert atmos.atmos_check_task(*check.args, **check.kwargs) is True
        checks = [
            j.kwargs["checks"] for j in _scheduled(app, "app.atmos.atmos_check_task")
        ]
        assert sorted(checks) == [0, 1]
        assert app.redis.exists(atmos.CHAIN_KEY.format(file_id=file.id))


def test_the_sweep_removes_only_the_stale_workspaces(app, monkeypatch, tmp_path):
    import app.atmos as atmos

    monkeypatch.setitem(app.config, "STAGING_DIR", str(tmp_path))
    old = 1_000_000_000
    with app.app_context():
        for file_id in (901, 902, 903):
            workspace = tmp_path / f"atmos-{file_id}"
            workspace.mkdir()
            (workspace / "copy.mkv").write_bytes(b"x")
            if file_id != 903:
                os.utime(workspace / "copy.mkv", (old, old))
                os.utime(workspace, (old, old))
        app.redis.set(atmos.CHAIN_KEY.format(file_id=902), "chain")
        (tmp_path / "atmos-notes").mkdir()

        removed = atmos.sweep_stale_atmos_workspaces()

    assert removed == [str(tmp_path / "atmos-901")]
    assert (tmp_path / "atmos-902").exists() and (tmp_path / "atmos-903").exists()


def test_an_early_end_of_a_conversion_queues_its_archive(app, monkeypatch):
    """A run that ends before its archive step queues upload_task (#505)."""

    import app.atmos as atmos
    from app import db

    with app.app_context():
        file, job, mediaconvert = _setup(app, monkeypatch, "Early End")
        file.converted_from_quality = "Remux-2160p"
        db.session.commit()
        monkeypatch.setattr(atmos, "get_setting", lambda name: True)
        assert atmos.atmos_supplement_task(file.id) is True
        (check,) = _scheduled(app, "app.atmos.atmos_check_task")

        mediaconvert.status = {"Status": "ERROR", "ErrorCode": 1030}
        with pytest.raises(atmos.AtmosRunEnded):
            atmos.atmos_check_task(*check.args, **check.kwargs)

        uploads = [
            j
            for queue in (app.file_queue, app.handoff_queue)
            for j in queue.jobs
            if j.func_name == "app.videos.upload_task"
        ]
        assert [j.args for j in uploads] == [(file.id,)]
