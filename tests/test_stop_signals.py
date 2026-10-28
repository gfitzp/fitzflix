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
