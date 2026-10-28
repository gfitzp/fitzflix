"""Test that a stop signal does not reject a good import (#497).

A supervisor stop, a restart, or a shutdown sends SIGTERM. A child that
SIGTERM stops has the return code -15. A second SIGTERM to a worker
raises SystemExit inside the job. Both keep the source in the import
directory for the hourly sweep. The title lock goes first."""

import os
import signal
import subprocess

import pytest


class _StoppedChild:
    """A fake child process that a signal ended with returncode."""

    def __init__(self, returncode):
        self.stdout = iter([])
        self.pid = 4343
        self.args = ["HandBrakeCLI"]
        self.returncode = returncode

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        pass


PLAN = {"source_format": "HDR10", "source_quality": "x", "target_quality": "y"}


def _convert_with_child(app, tmp_path, monkeypatch, returncode):
    from app import sdr_conversion

    hidden = tmp_path / ".Stopped (2001) - [Bluray-1080p].mkv"
    hidden.write_bytes(b"localized")
    source = tmp_path / "Stopped (2001) - [Remux-2160p].mkv"
    source.write_bytes(b"source")
    monkeypatch.setattr(sdr_conversion, "probe_video", lambda path: {})
    monkeypatch.setattr(
        sdr_conversion.subprocess,
        "Popen",
        lambda *args, **kwargs: _StoppedChild(returncode),
    )
    with app.app_context():
        lock = app.lock_manager.lock('{"title": "Stopped"}', 60000)
        result = sdr_conversion.convert_localized_file(
            str(source),
            {"basename": "Stopped (2001) - [Bluray-1080p].mkv"},
            lock,
            str(hidden),
            PLAN,
        )
    return result, source, hidden


def test_a_conversion_whose_encoder_gets_sigterm_keeps_the_source(
    app, tmp_path, monkeypatch
):
    result, source, hidden = _convert_with_child(
        app, tmp_path, monkeypatch, -signal.SIGTERM
    )

    assert result is False
    assert source.exists()
    assert not hidden.exists()
    assert app.redis.get('{"title": "Stopped"}') is None
    rejected = os.path.join(app.config["REJECTS_DIR"], "conversion failed")
    assert not os.path.exists(os.path.join(rejected, source.name))


def test_a_conversion_whose_encoder_fails_still_rejects_the_source(
    app, tmp_path, monkeypatch
):
    result, source, _ = _convert_with_child(app, tmp_path, monkeypatch, 2)

    assert result is False
    assert not source.exists()
    rejected = os.path.join(app.config["REJECTS_DIR"], "conversion failed")
    assert os.path.exists(os.path.join(rejected, source.name))


def test_a_second_sigterm_during_the_library_copy_cleans_up_and_goes_up(
    app, tmp_path, monkeypatch
):
    """SystemExit in the copy removes the copies, keeps the source, and re-raises."""

    from app import videos

    hidden = tmp_path / ".Copied (2001) - [DVD].mkv"
    hidden.write_bytes(b"localized")
    source = tmp_path / "Copied (2001) - [DVD].mkv"
    source.write_bytes(b"source")

    def stopped(*args, **kwargs):
        raise SystemExit(1)

    monkeypatch.setattr(videos, "_rename_with_retries", stopped)
    monkeypatch.setattr(videos, "copy_with_progress", stopped)
    with app.app_context():
        lock = app.lock_manager.lock('{"title": "Copied"}', 60000)
        with pytest.raises(SystemExit):
            videos.move_localized_file(
                str(source),
                {
                    "basename": "Copied (2001) - [DVD].mkv",
                    "dirname": "Movies/Copied (2001)",
                },
                lock,
                str(hidden),
            )

    assert source.exists()
    assert not hidden.exists()
    assert app.redis.get('{"title": "Copied"}') is None


def test_interrupted_knows_the_stop_signals(app):
    from app.importing import interrupted

    assert interrupted(subprocess.CalledProcessError(-signal.SIGTERM, ["x"]))
    assert interrupted(SystemExit(1))
    assert not interrupted(subprocess.CalledProcessError(-signal.SIGKILL, ["x"]))
    assert not interrupted(subprocess.CalledProcessError(2, ["x"]))
    assert not interrupted(RuntimeError("x"))


def test_abandon_import_releases_the_lock_before_the_slow_steps(app, monkeypatch):
    from app import importing

    events = []
    with app.app_context():
        monkeypatch.setattr(
            app.lock_manager, "unlock", lambda lock: events.append("unlock")
        )
        monkeypatch.setattr(
            importing,
            "discard_pending_archive",
            lambda key: events.append("discard"),
        )
        monkeypatch.setattr(
            importing,
            "move_to_rejects",
            lambda path, reason: events.append("reject"),
        )
        importing.abandon_import(
            "/import/Slow (2001).mkv",
            ("lock",),
            pending_archive="pending-untouched/run/Slow (2001).mkv",
        )
    assert events == ["unlock", "discard", "reject"]


class _WritingChild:
    """A fake child that runs until it is killed. Its output is 1 line."""

    def __init__(self, *args, **kwargs):
        self.stdout = iter(["line 50%\n", "line 60%\n"])
        self.pid = 4545
        self.args = ["tool"]
        self.killed = False
        self.returncode = None

    def poll(self):
        return -9 if self.killed else None

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return -9


def test_a_fault_in_the_atmos_step_loop_stops_the_child(app, monkeypatch):
    """A Redis fault in the progress write stops mkvextract or truehdd (#499)."""

    import app.atmos as atmos

    children = []

    def popen(*args, **kwargs):
        children.append(_WritingChild())
        return children[-1]

    class BrokenJob:
        """A job whose progress write fails after the child starts."""

        meta = {}

        def save_meta(self):
            if children:
                raise ConnectionError("redis down")

    monkeypatch.setattr(atmos.subprocess, "Popen", popen)
    with app.app_context(), pytest.raises(ConnectionError):
        atmos._run_step(["truehdd"], BrokenJob(), "Film.mkv", "Decoding")
    assert children[0].killed


def test_a_fault_in_the_atomicparsley_loop_stops_the_child(app, monkeypatch):
    """AtomicParsley rewrites the file in place. A fault stops it (#499)."""

    from app import importing

    children = []

    def popen(*args, **kwargs):
        children.append(_WritingChild())
        return children[-1]

    def broken(*args, **kwargs):
        raise ConnectionError("redis down")

    monkeypatch.setattr(importing.subprocess, "Popen", popen)
    monkeypatch.setattr(importing, "report_progress", broken)
    with app.app_context(), pytest.raises(ConnectionError):
        importing._copy_as_is(
            "/nowhere/Film.mp4",
            "/nowhere/.Film.mp4",
            "/nowhere",
            "Film.mp4",
            "MPEG-4",
            None,
        )
    assert children[0].killed


def _in_place_run(app, sample_mkv, incoming_dir, monkeypatch, failing_phase):
    """Run an in-place import whose failing_phase raises SystemExit."""

    from app import importing

    basename = "In Place Stop (2003) - [DVD].mkv"
    os.makedirs(incoming_dir, exist_ok=True)
    source = os.path.join(incoming_dir, basename)
    with open(sample_mkv, "rb") as f_in, open(source, "wb") as f_out:
        f_out.write(f_in.read())

    def stopped(*args, **kwargs):
        raise SystemExit(1)

    monkeypatch.setattr(importing, "_stage_source", lambda path, run: (path, False))
    monkeypatch.setattr(importing, failing_phase, stopped)
    with app.app_context(), pytest.raises(SystemExit):
        importing.localization_task(source)
    rejected = os.path.join(app.config["REJECTS_DIR"], "exception", basename)
    return source, rejected


def test_a_stop_during_the_untouched_upload_keeps_an_in_place_source(
    app, sample_mkv, incoming_dir, monkeypatch
):
    """The upload does not change the rip. The source stays (#497 follow-up)."""

    source, rejected = _in_place_run(
        app, sample_mkv, incoming_dir, monkeypatch, "_archive_original"
    )
    try:
        assert os.path.exists(source) and not os.path.exists(rejected)
    finally:
        for path in (source, rejected):
            if os.path.exists(path):
                os.remove(path)


def test_a_stop_during_an_in_place_localization_still_rejects(
    app, sample_mkv, incoming_dir, monkeypatch
):
    source, rejected = _in_place_run(
        app, sample_mkv, incoming_dir, monkeypatch, "_localize"
    )
    try:
        assert os.path.exists(rejected) and not os.path.exists(source)
    finally:
        for path in (source, rejected):
            if os.path.exists(path):
                os.remove(path)
