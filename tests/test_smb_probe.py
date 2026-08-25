"""The SMB lost-handle probe: the cheap question that finds a file whose
handle the NAS has lost before a 28GB upload discovers it at close().

A file in that state reads perfectly and answers close(2) with EBADF
forever, so the only way to see it is to ask.
"""

import errno
import json
import os

import pytest


class UnclosableOS:
    """An os module whose close(2) answers EBADF the way the NAS does.

    Patched onto app.smb_probe only, so the fake close can't reach the
    Redis connection the recorder is using.
    """

    def __init__(self, err=errno.EBADF):
        self.err = err

    def __getattr__(self, name):
        return getattr(os, name)

    def close(self, fd):
        os.close(fd)
        raise OSError(self.err, os.strerror(self.err))


def test_healthy_file_probes_clean(app, tmp_path):
    from app.smb_probe import lost_handle, probe_path

    path = str(tmp_path / "healthy.mkv")
    open(path, "wb").write(b"x")

    result = probe_path(path)
    assert result["ok"]
    assert result["errno"] is None
    assert not lost_handle(result)


def test_missing_file_reports_the_open_not_a_lost_handle(app, tmp_path):
    """A file that isn't there is a different problem, and counting it as a
    handle failure would bury the real ones."""

    from app.smb_probe import lost_handle, probe_path

    result = probe_path(str(tmp_path / "gone.mkv"))
    assert not result["ok"]
    assert result["stage"] == "open"
    assert result["errno"] == errno.ENOENT
    assert not lost_handle(result)


def test_failing_close_is_the_lost_handle_state(app, tmp_path, monkeypatch):
    """The signature: the open succeeds, the close returns EBADF."""

    from app import smb_probe

    path = str(tmp_path / "unclosable.mkv")
    open(path, "wb").write(b"x")

    monkeypatch.setattr(smb_probe, "os", UnclosableOS())

    result = smb_probe.probe_path(path)
    assert not result["ok"]
    assert result["stage"] == "close"
    assert smb_probe.lost_handle(result)


def test_a_close_failure_that_is_not_ebadf_is_not_the_state(app, tmp_path, monkeypatch):
    from app import smb_probe

    path = str(tmp_path / "eio.mkv")
    open(path, "wb").write(b"x")

    monkeypatch.setattr(smb_probe, "os", UnclosableOS(errno.EIO))

    result = smb_probe.probe_path(path)
    assert not result["ok"]
    assert not smb_probe.lost_handle(result)


def failure(path, stage="close", err=errno.EBADF):
    """A probe result standing in for a file in the lost-handle state."""

    return {
        "path": path,
        "ok": False,
        "stage": stage,
        "errno": err,
        "message": f"{stage} failed: Bad file descriptor (errno {err})",
    }


def test_repeated_failures_keep_the_first_sighting(app):
    """How long a file has been stuck is the number the investigation
    wants, so a later probe must not reset the clock."""

    from app.smb_probe import record_result, recorded_state

    with app.app_context():
        first = record_result(failure("/library/stuck.mkv"), context="mkvpropedit_task")
        again = record_result(failure("/library/stuck.mkv"), context="cli probe")

        assert again["first_seen"] == first["first_seen"]
        assert again["last_seen"] >= first["last_seen"]
        assert again["context"] == "cli probe"

        state = recorded_state()
        assert list(state) == ["/library/stuck.mkv"]
        assert state["/library/stuck.mkv"]["errno"] == errno.EBADF


def test_a_clean_probe_records_the_recovery_it_found(app, tmp_path):
    """The duration is the whole point, so a recovery is recorded rather
    than deleted: the entry stops being a failure and starts carrying how
    long the file was stuck."""

    from app.smb_probe import (
        HEALED,
        failing_state,
        healed_state,
        probe_path,
        record_result,
    )

    path = str(tmp_path / "recovered.mkv")
    open(path, "wb").write(b"x")

    with app.app_context():
        record_result(failure(path), context="mkvpropedit_task")
        assert path in failing_state()

        entry = record_result(probe_path(path), context="mkvpropedit_task")

        assert entry["state"] == HEALED
        assert entry["healed_at"]
        assert entry["held_for_seconds"] >= 0
        assert entry["context"] == "mkvpropedit_task"
        assert path not in failing_state()
        assert path in healed_state()


def test_a_task_probe_does_not_destroy_the_measurement(app, tmp_path):
    """The gap this closes. On Aug 25 2026 the one real recovery went
    unmeasured because mkvpropedit_task's own clean probe deleted the
    record before any recheck could read it."""

    from app.smb_probe import probe_and_record, recheck, record_result

    path = str(tmp_path / "healed-by-a-task.mkv")
    open(path, "wb").write(b"x")

    with app.app_context():
        record_result(failure(path), context="mkvpropedit_task")

        # The task touches the file again after the mount comes back

        probe_and_record(path, context="mkvpropedit_task")

        report = recheck()
        healed, still_failing = report.healed, report.still_failing

        assert [result["path"] for result in healed] == [path]
        assert healed[0]["held_for_seconds"] is not None
        assert healed[0]["healed_by"] == "mkvpropedit_task"
        assert still_failing == []


def test_a_clean_probe_of_an_unrecorded_file_records_nothing(app, tmp_path):
    """Healthy files are the overwhelming majority; recording them would
    turn the record into a library listing."""

    from app.smb_probe import probe_path, record_result, recorded_state

    path = str(tmp_path / "fine.mkv")
    open(path, "wb").write(b"x")

    with app.app_context():
        assert record_result(probe_path(path)) is None
        assert recorded_state() == {}


def test_a_recovery_is_reported_once_and_then_reaped(app, tmp_path):
    """Otherwise every recheck re-reports the same recoveries forever."""

    from app.smb_probe import probe_path, recheck, record_result, recorded_state

    path = str(tmp_path / "reported-once.mkv")
    open(path, "wb").write(b"x")

    with app.app_context():
        record_result(failure(path))
        record_result(probe_path(path))

        healed = recheck().healed
        assert [result["path"] for result in healed] == [path]

        assert recorded_state() == {}
        assert recheck() == ([], [], [], [])


def test_breaking_again_after_a_recovery_starts_a_new_clock(app, tmp_path):
    """A second episode is its own episode: inheriting the first sighting
    would report a duration that spans a stretch when the file was fine."""

    from app.smb_probe import FAILING, probe_path, record_result

    path = str(tmp_path / "twice.mkv")
    open(path, "wb").write(b"x")

    with app.app_context():
        first = record_result(failure(path))
        record_result(probe_path(path))
        again = record_result(failure(path))

        assert again["state"] == FAILING
        assert again["first_seen"] > first["first_seen"]
        assert "healed_at" not in again


def test_recheck_reports_how_long_a_file_was_stuck(app, tmp_path):
    from app.smb_probe import STATE_KEY, recheck, recorded_state

    path = str(tmp_path / "healed.mkv")
    open(path, "wb").write(b"x")

    with app.app_context():
        # Backdate the sighting: the file has since recovered on its own,
        # which is exactly what the NAS does

        app.redis.hset(
            STATE_KEY,
            path,
            json.dumps(
                {
                    "stage": "close",
                    "errno": errno.EBADF,
                    "message": "close failed",
                    "context": "mkvpropedit_task",
                    "first_seen": "2026-08-24T12:00:00+00:00",
                    "last_seen": "2026-08-24T12:00:00+00:00",
                }
            ),
        )

        report = recheck()
        healed, still_failing = report.healed, report.still_failing

        assert [result["path"] for result in healed] == [path]
        assert healed[0]["held_for_seconds"] > 0
        assert still_failing == []
        assert recorded_state() == {}


def test_recheck_keeps_a_file_that_is_still_stuck(app, tmp_path, monkeypatch):
    from app import smb_probe

    path = str(tmp_path / "still-stuck.mkv")
    open(path, "wb").write(b"x")

    with app.app_context():
        smb_probe.record_result(failure(path), context="mkvpropedit_task")
        monkeypatch.setattr(smb_probe, "os", UnclosableOS())

        report = smb_probe.recheck()
        healed, still_failing = report.healed, report.still_failing

        assert healed == []
        assert [result["path"] for result in still_failing] == [path]
        assert path in smb_probe.recorded_state()


def test_a_file_that_is_not_local_is_not_a_finding(app, tmp_path):
    """Every superseded edition keeps its row and its S3 archive after the
    local copy goes away, so absence is the normal state for thousands of
    files and must not be recorded as a failure."""

    from app.smb_probe import absent, lost_handle, probe_path, record_result
    from app.smb_probe import recorded_state

    result = probe_path(str(tmp_path / "superseded.mkv"))

    assert absent(result)
    assert not lost_handle(result)

    with app.app_context():
        assert record_result(result, context="cli probe") is None
        assert recorded_state() == {}


def test_a_recorded_file_that_leaves_the_volume_is_dropped(app, tmp_path):
    """It didn't recover and it can't still be stuck, so keeping it in the
    record would be the wrong answer twice."""

    from app.smb_probe import recheck, record_result, recorded_state

    path = str(tmp_path / "vanished.mkv")

    with app.app_context():
        record_result(failure(path), context="mkvpropedit_task")
        assert path in recorded_state()

        # The file goes away — a rename, or a better edition replacing it

        report = recheck()

        assert report.healed == []
        assert report.still_failing == []
        assert [result["path"] for result in report.gone] == [path]
        assert recorded_state() == {}


def test_an_unmounted_share_is_not_thousands_of_departures(app, tmp_path, monkeypatch):
    """The trap this closes. Every file on an unmounted share reports
    ENOENT at once, and calling that departure would drop the whole
    record — including durations that exist nowhere else."""

    from app import smb_probe

    path = str(tmp_path / "Movies" / "on-a-dead-share.mkv")

    with app.app_context():
        smb_probe.record_result(failure(path), context="mkvpropedit_task")

        # The share the file lives on is not there

        monkeypatch.setattr(smb_probe, "share_available", lambda p: False)

        report = smb_probe.recheck()

        assert report.gone == []
        assert report.healed == []
        assert [result["path"] for result in report.skipped] == [path]

        # The record — and the first_seen it carries — must survive

        assert path in smb_probe.recorded_state()


def test_a_recovery_outlives_the_recheck_that_reported_it(app, tmp_path):
    """recheck reaps what it reports, so without a durable history the
    only trace of a measurement is the terminal that ran the command."""

    from app.smb_probe import history, probe_path, recheck, record_result

    path = str(tmp_path / "measured.mkv")
    open(path, "wb").write(b"x")

    with app.app_context():
        record_result(failure(path), context="mkvpropedit_task")
        record_result(probe_path(path), context="recheck")

        recheck()  # reports the recovery and reaps the state entry

        episodes = history()
        assert [episode["path"] for episode in episodes] == [path]
        assert episodes[0]["held_for_seconds"] is not None
        assert episodes[0]["context"] == "mkvpropedit_task"

        # And a second recheck doesn't duplicate it

        recheck()
        assert len(history()) == 1


def test_history_accumulates_across_episodes(app, tmp_path):
    """One episode never answers how long the state lasts."""

    from app.smb_probe import history, probe_path, record_result

    with app.app_context():
        for name in ("first.mkv", "second.mkv"):
            path = str(tmp_path / name)
            open(path, "wb").write(b"x")
            record_result(failure(path), context="cli probe")
            record_result(probe_path(path), context="recheck")

        assert len(history()) == 2


def test_share_root_is_the_share_not_the_library(app):
    """A path's share is the first component below LIBRARY_DIR, which is
    what gets unmounted — /Volumes/Movies, not /Volumes."""

    import os

    from app.smb_probe import share_root

    with app.app_context():
        library = app.config["LIBRARY_DIR"]
        assert share_root(
            os.path.join(library, "Movies", "x", "y.mkv")
        ) == os.path.join(library, "Movies")
        assert share_root(os.path.join(library, "TV Shows", "s", "e.mkv")) == (
            os.path.join(library, "TV Shows")
        )


def test_the_stale_mark_survives_the_rollback_that_follows_it(app):
    """Every caller is a failure path that rolls back. A marker taken
    with it would leave the loss as undiscoverable as before."""

    from app import db
    from app.aws_storage import mark_archive_stale
    from app.models import File
    from tests.factories import make_movie, make_movie_file

    with app.app_context():
        movie = make_movie("Rolled Back", 2021)
        file = make_movie_file(movie, "Bluray-1080p")
        db.session.commit()
        file_id = file.id

        assert mark_archive_stale(file_id, reason="test") is True
        db.session.rollback()

        db.session.expire_all()
        assert File.query.get(file_id).aws_untouched_stale is True


def test_a_successful_upload_clears_the_stale_mark(app, monkeypatch):
    """Otherwise the repair queue never empties."""

    from datetime import datetime, timezone

    from app import db
    from app.models import File
    from tests.factories import make_movie, make_movie_file

    import app.aws_storage as aws_storage

    with app.app_context():
        movie = make_movie("Repaired", 2021)
        file = make_movie_file(movie, "Bluray-1080p")
        file.aws_untouched_key = "untouched/repaired.mkv"
        file.aws_untouched_stale = True
        db.session.commit()
        file_id = file.id

        monkeypatch.setattr(
            aws_storage,
            "aws_upload",
            lambda **kwargs: (
                "untouched/repaired.mkv",
                datetime.now(timezone.utc),
                123,
            ),
        )

        assert aws_storage.upload_task(file_id) is True

        db.session.expire_all()
        assert File.query.get(file_id).aws_untouched_stale is False


def test_a_transient_mount_error_does_not_reject_the_file(app, tmp_path, monkeypatch):
    """Rejecting would move a library file out of the library over a
    problem that clears on its own — and this is precisely how the
    lost-handle state arrives, from inside s3transfer's close."""

    import app.aws_storage as aws_storage
    import app.videos as videos

    path = str(tmp_path / "transient.mkv")
    open(path, "wb").write(b"x")

    rejected = []
    monkeypatch.setattr(
        videos, "move_to_rejects", lambda p, reason="": rejected.append(p)
    )

    class ExplodingClient:
        def list_objects(self, **kwargs):
            return {}

        def upload_file(self, *args, **kwargs):
            raise OSError(errno.EBADF, "Bad file descriptor")

    monkeypatch.setattr(aws_storage, "aws_s3_client", lambda **kw: ExplodingClient())

    app.config["AWS_BUCKET"] = "test-bucket"

    with app.app_context():
        with pytest.raises(OSError):
            aws_storage.aws_upload(path, "untouched")

        assert rejected == []


def test_repair_will_not_queue_a_file_whose_handle_is_still_lost(app, monkeypatch):
    """Retrying while the handle is lost fails exactly the same way, so
    the probe is what decides whether a repair can run at all — the whole
    reason the two halves belong together."""

    import app.cli as app_cli

    from app import db, smb_probe
    from app.models import File
    from tests.factories import make_movie, make_movie_file

    app_cli.register(app)

    with app.app_context():
        movie = make_movie("Still Stuck", 2021)
        file = make_movie_file(movie, "Bluray-1080p")
        file.aws_untouched_key = "untouched/still-stuck.mkv"
        file.aws_untouched_stale = True
        db.session.commit()

        queued = []
        monkeypatch.setattr(smb_probe, "probe_path", failure)
        monkeypatch.setattr(
            app.file_queue,
            "enqueue",
            lambda *a, **kw: queued.append(a),
        )

        result = app.test_cli_runner().invoke(args=["smb", "repair", "--enqueue"])

        assert "BLOCKED" in result.output
        assert queued == []

        # And it stays marked, so the nightly sync still owes the repair

        db.session.expire_all()
        assert File.query.filter_by(aws_untouched_stale=True).count() == 1


def test_a_broken_probe_never_fails_the_task_it_reports_on(app, monkeypatch):
    """The probe runs after work that already succeeded. A diagnostic that
    can fail the task it's reporting on is worse than no diagnostic."""

    from app import smb_probe

    def exploding_probe(path):
        raise RuntimeError("redis is down, or the mount is")

    monkeypatch.setattr(smb_probe, "probe_path", exploding_probe)

    with app.app_context():
        assert smb_probe.probe_and_record("/library/whatever.mkv") is None


def test_mkvpropedit_probes_the_file_it_just_wrote(app, monkeypatch):
    """The bulk mkvpropedit run is the reproducer, so the task reports for
    itself instead of waiting for the re-archive's close to fail."""

    import app.videos as videos

    from app import db, smb_probe, tracks
    from tests.factories import make_movie, make_movie_file

    with app.app_context():
        movie = make_movie("Probe After Edit", 2021)
        file = make_movie_file(movie, "Bluray-1080p")
        db.session.commit()
        file_id = file.id
        path = smb_probe.library_path(file)

        monkeypatch.setattr(tracks, "mkvpropedit_unlocked", lambda *a, **kw: True)
        monkeypatch.setattr(smb_probe, "probe_path", failure)

        assert videos.mkvpropedit_task(file_id, "2", None, [], {"a1": "eng"}) is True

        state = smb_probe.recorded_state()
        assert path in state
        assert state[path]["context"] == "mkvpropedit_task"


def test_mkvpropedit_probes_after_a_failed_edit_too(app, monkeypatch):
    """The Aug 24 sighting surfaced as an S3 error at the end of the
    re-archive, so the failure path is the one that most needs to say
    which file went bad."""

    import app.videos as videos

    from app import db, smb_probe, tracks
    from tests.factories import make_movie, make_movie_file

    with app.app_context():
        movie = make_movie("Probe After Failure", 2021)
        file = make_movie_file(movie, "Bluray-1080p")
        db.session.commit()
        file_id = file.id
        path = smb_probe.library_path(file)

        def unsafe_unlocked(*args, **kwargs):
            error = OSError(errno.EBADF, "Bad file descriptor")
            error.retry_unsafe = True
            raise error

        monkeypatch.setattr(tracks, "mkvpropedit_unlocked", unsafe_unlocked)
        monkeypatch.setattr(smb_probe, "probe_path", failure)

        with pytest.raises(OSError):
            videos.mkvpropedit_task(file_id, "2", None, [])

        assert path in smb_probe.recorded_state()
