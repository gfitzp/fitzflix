"""Test the SMB lost-handle probe.

The probe is a cheap question. It finds a file with a handle that the
NAS lost, before a 28GB upload finds the problem at close().

A file in that state reads correctly. But it answers close(2) with EBADF
forever. Thus, the only way to see the state is to ask.
"""

import errno
import json
import os

import pytest


class UnclosableOS:
    """Fake an os module with a close(2) that answers EBADF like the NAS.

    The test patches it onto app.smb_probe only. Thus, the fake close
    cannot reach the Redis connection that the recorder uses.
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
    """Make sure a missing file is not a lost-handle failure.

    A missing file is a different problem. If the probe counted it as a
    handle failure, the real failures would be hidden."""

    from app.smb_probe import lost_handle, probe_path

    result = probe_path(str(tmp_path / "gone.mkv"))
    assert not result["ok"]
    assert result["stage"] == "open"
    assert result["errno"] == errno.ENOENT
    assert not lost_handle(result)


def test_failing_close_is_the_lost_handle_state(app, tmp_path, monkeypatch):
    """Make sure the probe finds the signature: open succeeds, close returns EBADF."""

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
    """Return a probe result for a file in the lost-handle state."""

    return {
        "path": path,
        "ok": False,
        "stage": stage,
        "errno": err,
        "message": f"{stage} failed: Bad file descriptor (errno {err})",
    }


def test_repeated_failures_keep_the_first_sighting(app):
    """Make sure a later probe keeps the first sighting.

    The investigation wants the time that a file has been stuck. Thus, a
    later probe must not reset the clock."""

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
    """Make sure a clean probe records the recovery that it found.

    The duration is the important number. Thus, the recorder records a
    recovery and does not delete it. The entry stops as a failure. It
    then carries the time that the file was stuck."""

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
    """Make sure a task probe does not destroy the measurement.

    This test closes a gap. On 2026-08-25, the 1 real recovery was not
    measured. The clean probe of mkvpropedit_task deleted the record
    before a recheck could read it."""

    from app.smb_probe import probe_and_record, recheck, record_result

    path = str(tmp_path / "healed-by-a-task.mkv")
    open(path, "wb").write(b"x")

    with app.app_context():
        record_result(failure(path), context="mkvpropedit_task")

        # The task touches the file again after the mount comes back.

        probe_and_record(path, context="mkvpropedit_task")

        report = recheck()
        healed, still_failing = report.healed, report.still_failing

        assert [result["path"] for result in healed] == [path]
        assert healed[0]["held_for_seconds"] is not None
        assert healed[0]["healed_by"] == "mkvpropedit_task"
        assert still_failing == []


def test_a_clean_probe_of_an_unrecorded_file_records_nothing(app, tmp_path):
    """Make sure a clean probe of an unrecorded file records nothing.

    Almost all files are healthy. If the recorder recorded them, the
    record would become a library listing."""

    from app.smb_probe import probe_path, record_result, recorded_state

    path = str(tmp_path / "fine.mkv")
    open(path, "wb").write(b"x")

    with app.app_context():
        assert record_result(probe_path(path)) is None
        assert recorded_state() == {}


def test_a_recovery_is_reported_once_and_then_reaped(app, tmp_path):
    """Make sure a recheck reports a recovery 1 time and then removes it.

    If not, each recheck reports the same recoveries forever."""

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
    """Make sure a new failure after a recovery starts a new clock.

    A second episode is its own episode. If it kept the first sighting,
    the duration would include a period when the file was healthy."""

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
        # Set the sighting to an earlier time. The file has recovered on
        # its own since then. That is exactly what the NAS does.

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
    """Make sure a file that is not local is not a finding.

    Each superseded edition keeps its row and its S3 archive after the
    local copy goes away. Thus, absence is the normal state for thousands
    of files. The recorder must not record it as a failure."""

    from app.smb_probe import absent, lost_handle, probe_path, record_result
    from app.smb_probe import recorded_state

    result = probe_path(str(tmp_path / "superseded.mkv"))

    assert absent(result)
    assert not lost_handle(result)

    with app.app_context():
        assert record_result(result, context="cli probe") is None
        assert recorded_state() == {}


def test_a_recorded_file_that_leaves_the_volume_is_dropped(app, tmp_path):
    """Make sure the recheck removes a recorded file that left the volume.

    The file did not recover, and it cannot be stuck. Thus, the record
    would give the wrong answer 2 times if it kept the file."""

    from app.smb_probe import recheck, record_result, recorded_state

    path = str(tmp_path / "vanished.mkv")

    with app.app_context():
        record_result(failure(path), context="mkvpropedit_task")
        assert path in recorded_state()

        # The file goes away. A rename, or a better edition, replaces it.

        report = recheck()

        assert report.healed == []
        assert report.still_failing == []
        assert [result["path"] for result in report.gone] == [path]
        assert recorded_state() == {}


def test_an_unmounted_share_is_not_thousands_of_departures(app, tmp_path, monkeypatch):
    """Make sure an unmounted share does not count as thousands of departures.

    This test closes a trap. Each file on an unmounted share reports
    ENOENT at the same time. If the recheck called that a departure, it
    would delete the whole record. The record holds durations that exist
    nowhere else."""

    from app import smb_probe

    path = str(tmp_path / "Movies" / "on-a-dead-share.mkv")

    with app.app_context():
        smb_probe.record_result(failure(path), context="mkvpropedit_task")

        # The share that holds the file is not there.

        monkeypatch.setattr(smb_probe, "share_available", lambda p: False)

        report = smb_probe.recheck()

        assert report.gone == []
        assert report.healed == []
        assert [result["path"] for result in report.skipped] == [path]

        # The record, and the first_seen that it carries, must survive.

        assert path in smb_probe.recorded_state()


def test_a_recovery_outlives_the_recheck_that_reported_it(app, tmp_path):
    """Make sure a recovery survives the recheck that reported it.

    The recheck removes what it reports. Without a durable history, the
    only trace of a measurement is the terminal that ran the command."""

    from app.smb_probe import history, probe_path, recheck, record_result

    path = str(tmp_path / "measured.mkv")
    open(path, "wb").write(b"x")

    with app.app_context():
        record_result(failure(path), context="mkvpropedit_task")
        record_result(probe_path(path), context="recheck")

        recheck()  # reports the recovery and removes the state entry

        episodes = history()
        assert [episode["path"] for episode in episodes] == [path]
        assert episodes[0]["held_for_seconds"] is not None
        assert episodes[0]["context"] == "mkvpropedit_task"

        # A second recheck does not duplicate it.

        recheck()
        assert len(history()) == 1


def test_history_accumulates_across_episodes(app, tmp_path):
    """Make sure the history collects all episodes.

    One episode never answers how long the state lasts."""

    from app.smb_probe import history, probe_path, record_result

    with app.app_context():
        for name in ("first.mkv", "second.mkv"):
            path = str(tmp_path / name)
            open(path, "wb").write(b"x")
            record_result(failure(path), context="cli probe")
            record_result(probe_path(path), context="recheck")

        assert len(history()) == 2


def test_share_root_is_the_share_not_the_library(app):
    """Make sure the share root is the share, not the library.

    The share of a path is the first component below LIBRARY_DIR. That
    is the part that unmounts: /Volumes/Movies, not /Volumes."""

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
    """Make sure the stale mark survives the rollback that follows it.

    Each caller is a failure path that rolls back. If the rollback
    removed the marker, the loss would stay hidden as before."""

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
    """Make sure a successful upload clears the stale mark.

    If not, the repair queue never empties."""

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
    """Make sure a transient mount error does not reject the file.

    A reject would move a library file out of the library because of a
    problem that clears on its own. This is exactly how the lost-handle
    state arrives: from inside the close of s3transfer."""

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

    monkeypatch.setitem(app.config, "AWS_BUCKET", "test-bucket")

    with app.app_context():
        with pytest.raises(OSError):
            aws_storage.aws_upload(path, "untouched")

        assert rejected == []


def test_repair_will_not_queue_a_file_whose_handle_is_still_lost(app, monkeypatch):
    """Make sure the repair does not queue a file with a lost handle.

    A retry while the handle is lost fails in exactly the same way.
    Thus, the probe decides if a repair can run. That is the reason the
    2 halves belong together."""

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

        # The file stays marked. Thus, the nightly sync still owes the
        # repair.

        db.session.expire_all()
        assert File.query.filter_by(aws_untouched_stale=True).count() == 1


def test_the_nightly_sweep_records_what_it_finds(app, monkeypatch):
    """Make sure the nightly sweep records what it finds.

    Nothing finds this state on its own. Thus, something has to ask on a
    schedule, and not wait for an upload to find the problem."""

    from app import db, smb_probe
    from app.maintenance import smb_handle_sweep
    from tests.factories import make_movie, make_movie_file

    with app.app_context():
        movie = make_movie("Swept", 2021)
        file = make_movie_file(movie, "Bluray-1080p")
        db.session.commit()
        path = smb_probe.library_path(file)

        monkeypatch.setattr(smb_probe, "probe_path", failure)

        assert smb_handle_sweep() is True

        state = smb_probe.recorded_state()
        assert path in state
        assert state[path]["context"] == "nightly sweep"


def test_the_sweep_is_scheduled_nightly(app):
    """Make sure the sweep has a nightly schedule.

    The cron table is the authority on each scheduler start. Thus, the
    entry in the table is the whole of the schedule."""

    from app import cron_table

    rows = [
        row
        for row in cron_table(app.config)
        if row["func"] == "app.maintenance.smb_handle_sweep"
    ]

    assert len(rows) == 1
    assert rows[0]["cron"] == "0 5 * * *"
    assert rows[0]["queue"] == "fitzflix-maintenance"


def test_a_broken_probe_never_fails_the_task_it_reports_on(app, monkeypatch):
    """Make sure a broken probe never fails the task that it reports on.

    The probe runs after work that already succeeded. A diagnostic that
    can fail the task that it reports on is worse than no diagnostic."""

    from app import smb_probe

    def exploding_probe(path):
        raise RuntimeError("redis is down, or the mount is")

    monkeypatch.setattr(smb_probe, "probe_path", exploding_probe)

    with app.app_context():
        assert smb_probe.probe_and_record("/library/whatever.mkv") is None


def test_mkvpropedit_probes_the_file_it_just_wrote(app, monkeypatch):
    """Make sure mkvpropedit probes the file that it wrote.

    The bulk mkvpropedit run reproduces the problem. Thus, the task
    reports for itself. It does not wait for the close of the re-archive
    to fail."""

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
    """Make sure mkvpropedit probes after a failed edit too.

    The 2026-08-24 sighting appeared as an S3 error at the end of the
    re-archive. Thus, the failure path most needs to say which file went
    bad."""

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


def test_a_leftover_mountpoint_is_not_an_available_share(app, tmp_path, monkeypatch):
    """Make sure a leftover mount point is not an available share.

    This is #232, seen live on 2026-08-25. When the SMB session dies,
    macOS can leave the mount point behind as a normal directory on the
    boot disk. isdir called that share present. Thus, unmounted()
    answered False, and the recheck would have recorded each file on the
    share as a departure."""

    import app.maintenance as maintenance
    from app.smb_probe import absent, probe_path, share_available, unmounted

    volumes = tmp_path / "Volumes"
    leftover = volumes / "TV Shows"
    leftover.mkdir(parents=True)
    monkeypatch.setattr(maintenance, "VOLUMES_ROOT", str(volumes))

    path = str(leftover / "Top Gear (2002)" / "s01e01.mkv")

    # This is exactly what the outage looked like. The directory is
    # there, and it is not a mount point.

    assert os.path.isdir(str(leftover))
    assert os.path.ismount(str(leftover)) is False

    with app.app_context():
        monkeypatch.setitem(app.config, "LIBRARY_DIR", str(volumes))

        assert share_available(path) is False

        result = probe_path(path)
        assert absent(result)
        assert unmounted(result) is True


def test_a_mounted_share_is_available(app, tmp_path, monkeypatch):
    import app.maintenance as maintenance
    from app.smb_probe import share_available

    volumes = tmp_path / "Volumes"
    mounted = volumes / "Movies"
    mounted.mkdir(parents=True)
    monkeypatch.setattr(maintenance, "VOLUMES_ROOT", str(volumes))
    monkeypatch.setattr(maintenance.os.path, "ismount", lambda path: True)

    with app.app_context():
        monkeypatch.setitem(app.config, "LIBRARY_DIR", str(volumes))

        assert share_available(str(mounted / "A Film (1999)" / "a.mkv")) is True


def test_a_library_off_the_volumes_root_needs_no_mountpoint(app, tmp_path, monkeypatch):
    """Make sure a library outside the volumes root needs no mount point.

    The original isdir check protected this case: a library that is not
    on a separate mount. It is not a mount point, and it was never meant
    to be one. If Fitzflix required a mount point everywhere, it would
    call this library dead."""

    import app.maintenance as maintenance
    from app.smb_probe import share_available

    monkeypatch.setattr(maintenance, "VOLUMES_ROOT", str(tmp_path / "Volumes"))
    library = tmp_path / "library"
    (library / "Movies").mkdir(parents=True)

    assert os.path.ismount(str(library / "Movies")) is False

    with app.app_context():
        monkeypatch.setitem(app.config, "LIBRARY_DIR", str(library))

        assert share_available(str(library / "Movies" / "a.mkv")) is True


def test_a_share_that_is_gone_entirely_is_still_unavailable(app, tmp_path, monkeypatch):
    """Make sure a share that is fully gone is still unavailable.

    A clean unmount deletes the mount point. That case must keep the
    same answer that it always had."""

    import app.maintenance as maintenance
    from app.smb_probe import share_available

    volumes = tmp_path / "Volumes"
    volumes.mkdir()
    monkeypatch.setattr(maintenance, "VOLUMES_ROOT", str(volumes))

    with app.app_context():
        monkeypatch.setitem(app.config, "LIBRARY_DIR", str(volumes))

        assert share_available(str(volumes / "Movies" / "a.mkv")) is False


def test_the_sweep_asks_each_share_before_opening_its_files(app, monkeypatch):
    """Make sure the sweep asks each share before it opens the files.

    This is #237. A WEDGED share is still in the mount table, but its
    syscalls hang. It would stall the first os.open of the sweep until
    the job timeout killed the job. That would lose the rest of the
    sweep, the recheck, and the history write. The sweep checks the
    health of each share 1 time, through the watchdog of volume_alive.
    If a share does not answer, the sweep never opens its files."""

    from app import db, smb_probe
    from app.maintenance import smb_handle_sweep
    from tests.factories import make_movie, make_movie_file

    with app.app_context():
        movie = make_movie("Wedged Share Film", 2021)
        make_movie_file(movie, "Bluray-1080p")
        db.session.commit()

        def boom(path):
            raise AssertionError(f"probed '{path}' on a share that never answered")

        monkeypatch.setattr(smb_probe, "share_responsive", lambda path: False)
        monkeypatch.setattr(smb_probe, "probe_path", boom)

        assert smb_handle_sweep() is True
        assert smb_probe.recorded_state() == {}


def test_recheck_skips_a_wedged_share_without_probing(app, monkeypatch, tmp_path):
    """Make sure the recheck skips a wedged share and does not probe it.

    This is the recheck half of #237. The recheck skips a recorded
    failure on a share that does not answer the watchdog. It does not
    touch the record, and it never opens the file. Thus, it does not
    hang on the probe."""

    from app import smb_probe

    path = str(tmp_path / "Movies" / "on-a-wedged-share.mkv")

    with app.app_context():
        smb_probe.record_result(failure(path), context="mkvpropedit_task")

        def boom(p):
            raise AssertionError(f"probed '{p}' on a share that never answered")

        monkeypatch.setattr(smb_probe, "share_responsive", lambda p: False)
        monkeypatch.setattr(smb_probe, "probe_path", boom)

        report = smb_probe.recheck()

        assert report.gone == []
        assert report.healed == []
        assert report.still_failing == []
        assert [result["path"] for result in report.skipped] == [path]
        assert "not responding" in report.skipped[0]["message"]

        # The record, and the first_seen that it carries, must survive.

        assert path in smb_probe.recorded_state()
