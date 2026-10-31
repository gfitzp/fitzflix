"""Tests of the access log settings of the web process."""

import importlib.util
import logging
import os
import subprocess
import sys

import pytest

from app.access_log import (
    ACCESS_LOG_FORMAT,
    TokenFilter,
    access_log_path,
    logconfig,
    redact_tokens,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.mark.parametrize(
    "line",
    [
        "GET /dvr/abc123/guide.xml HTTP/1.1",
        "GET /dvr/abc123/stream/wgbh.ts HTTP/1.1",
        "GET /dvr/abc123/playlist.m3u/lineup.json HTTP/1.1",
        "POST /plex/webhook/abc123 HTTP/1.1",
        "GET /reset-password/abc123?next=%2F HTTP/1.1",
        "GET /game/brag/abc123 HTTP/1.1",
        "GET /game/frame/abc123 HTTP/1.1",
    ],
)
def test_redact_tokens_hides_each_route_token(line):
    redacted = redact_tokens(line)

    assert "abc123" not in redacted
    assert "/<token>" in redacted
    assert redacted.endswith(" HTTP/1.1")


def test_redact_tokens_leaves_other_lines_alone():
    line = "GET /movie/123?next=/dvr HTTP/1.1"

    assert redact_tokens(line) == line


def test_token_filter_rewrites_the_request_atoms():
    atoms = {
        "r": "GET /dvr/abc123/guide.xml HTTP/1.1",
        "U": "/dvr/abc123/guide.xml",
        "s": "200",
    }
    record = logging.LogRecord(
        "gunicorn.access", logging.INFO, __file__, 1, ACCESS_LOG_FORMAT, atoms, None
    )

    assert TokenFilter().filter(record) is True
    assert record.args["r"] == "GET /dvr/<token>/guide.xml HTTP/1.1"
    assert record.args["U"] == "/dvr/<token>/guide.xml"
    assert record.args["s"] == "200"


def test_token_filter_passes_a_record_without_atoms():
    record = logging.LogRecord(
        "gunicorn.error", logging.INFO, __file__, 1, "Booting worker %s", (1,), None
    )

    assert TokenFilter().filter(record) is True
    assert record.args == (1,)


def test_access_log_path_sits_next_to_the_application_log(tmp_path):
    log_file = str(tmp_path / "logs" / "fitzflix.log")

    assert access_log_path(log_file) == str(tmp_path / "logs" / "access.log")


def test_logconfig_writes_the_access_log_through_a_watched_handler(tmp_path):
    access_log = str(tmp_path / "logs" / "access.log")

    config = logconfig(access_log)

    assert os.path.isdir(os.path.dirname(access_log))
    handler = config["handlers"]["access_file"]
    assert handler["class"] == "logging.handlers.WatchedFileHandler"
    assert handler["filename"] == access_log
    assert handler["filters"] == ["tokens"]
    assert config["filters"]["tokens"]["()"] is TokenFilter
    access = config["loggers"]["gunicorn.access"]
    assert access["handlers"] == ["access_file"]
    assert access["propagate"] is False

    # The defaults of gunicorn stay. The error log still reaches stderr.

    assert config["loggers"]["gunicorn.error"]["handlers"] == ["error_console"]
    assert "console" in config["handlers"]
    assert config["disable_existing_loggers"] is False


def test_logconfig_writes_a_redacted_line_through_dict_config(tmp_path):
    """Apply the configuration in a child process. The test process keeps its logging."""

    access_log = str(tmp_path / "logs" / "access.log")
    code = (
        "import logging, sys\n"
        "from logging.config import dictConfig\n"
        "from app.access_log import ACCESS_LOG_FORMAT, logconfig\n"
        f"dictConfig(logconfig({access_log!r}))\n"
        "atoms = {'t': '[30/Oct/2026:22:40:01 -0400]', 'p': '<1>', 'h': '10.0.0.2',"
        " 'r': 'GET /dvr/abc123/guide.xml HTTP/1.1', 's': '200', 'b': '12',"
        " 'L': '0.004210', 'a': 'curl'}\n"
        "logging.getLogger('gunicorn.access').info(ACCESS_LOG_FORMAT, atoms)\n"
        "logging.getLogger('gunicorn.error').info('Booting worker with pid: 1')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True
    )

    assert result.returncode == 0, result.stderr
    with open(access_log) as f:
        lines = f.read().splitlines()
    assert lines == [
        '[30/Oct/2026:22:40:01 -0400] <1> 10.0.0.2 "GET /dvr/<token>/guide.xml'
        ' HTTP/1.1" 200 12 0.004210 "curl"'
    ]
    assert "Booting worker" in result.stderr
    assert "Booting worker" not in result.stdout


def test_gunicorn_conf_sets_the_access_log(tmp_path, monkeypatch):
    import config

    monkeypatch.setattr(
        config.Config, "LOG_FILE", str(tmp_path / "logs" / "fitzflix.log")
    )
    spec = importlib.util.spec_from_file_location(
        "gunicorn_conf", os.path.join(ROOT, "gunicorn.conf.py")
    )
    module = importlib.util.module_from_spec(spec)

    spec.loader.exec_module(module)

    assert module.access_log_format == ACCESS_LOG_FORMAT
    handler = module.logconfig_dict["handlers"]["access_file"]
    assert handler["filename"] == str(tmp_path / "logs" / "access.log")
