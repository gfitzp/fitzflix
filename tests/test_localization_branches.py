"""Pin the branches of localization_task that had no test before its split.

The seam split of tranche 3 cut localization_task into phases. Each
test here passed against the old function. The sample files come from
the session fixtures of conftest.
"""

import os
import subprocess

import pytest

from tests.test_scheduling import scheduled_jobs

import app.videos as videos

from app.models import File
from app.videos import finalize_localization, localization_task

from tests.localization_helpers import rejected_files, settle

# The pins of the localization_task branches that had no test before the
# seam split (tranche 3). Each test passed against the old function.


def test_a_missing_source_stops_quietly(app, incoming_dir):
    """A source that is gone returns False and queues nothing."""

    source = os.path.join(incoming_dir, "Gone Already (2021) - [DVD].mkv")
    with app.app_context():
        assert localization_task(source) is False
        assert scheduled_jobs(app.import_queue) == []
        assert len(app.handoff_queue) == 0


def test_a_temp_copy_name_is_ignored(app, incoming_dir):
    """A name with "-temp-NNNN." is a copy in progress. The task leaves it."""

    source = os.path.join(incoming_dir, "Temp Copy (2021) - [DVD]-temp-1234.mkv")
    with open(source, "wb") as f:
        f.write(b"video")
    try:
        with app.app_context():
            assert localization_task(source) is False
            assert scheduled_jobs(app.import_queue) == []
        assert os.path.exists(source)
        assert os.path.basename(source) not in rejected_files(app)
    finally:
        os.remove(source)


def test_a_name_that_does_not_parse_goes_to_rejects(app, incoming_dir, caplog):
    """A name with no known format goes to rejects as "incorrect filename"."""

    basename = "no format at all.mkv"
    source = os.path.join(incoming_dir, basename)
    with open(source, "wb") as f:
        f.write(b"video")
    settle(source)
    with app.app_context():
        assert localization_task(source) is False
    assert "doesn't match expected naming formats" in caplog.text
    rejected = os.path.join(app.config["REJECTS_DIR"], "incorrect filename", basename)
    assert os.path.exists(rejected)
    os.remove(rejected)


def test_an_inferior_file_goes_to_rejects_and_frees_the_lock(
    app, sample_mkv, incoming_dir, monkeypatch
):
    """A better copy in the library rejects the import and emails the admin."""

    from app import importing

    basename = "Inferior Copy (2021) - [DVD].mkv"
    source = os.path.join(incoming_dir, basename)
    with open(sample_mkv, "rb") as f_in, open(source, "wb") as f_out:
        f_out.write(f_in.read())

    better = File(basename="Inferior Copy (2021) - [Bluray-1080p].mkv")
    monkeypatch.setattr(importing, "find_better_files", lambda details: [better])
    sent = []
    monkeypatch.setattr(
        importing, "notify_admin", lambda *args, **kwargs: sent.append((args, kwargs))
    )
    with app.app_context():
        assert localization_task(source) is False
        details = importing.evaluate_filename(
            os.path.join(app.config["REJECTS_DIR"], "inferior quality", basename)
        )
        lock = app.lock_manager.lock(importing.lock_key_for_details(details), 1000)
        assert lock
        app.lock_manager.unlock(lock)
    rejected = os.path.join(app.config["REJECTS_DIR"], "inferior quality", basename)
    assert os.path.exists(rejected)
    assert [args[1] for args, _ in sent] == ["inferior_warning"]
    assert sent[0][1]["better_versions"] == [better]
    assert os.listdir(app.config["STAGING_DIR"]) == []
    os.remove(rejected)


def test_a_file_too_big_for_staging_is_localized_beside_the_library(
    app, sample_mkv, incoming_dir, monkeypatch
):
    """A file that cannot fit on staging is processed on its source volume.

    The hidden output goes straight to its library directory. The move
    finds it at its destination name already."""

    from app import importing

    monkeypatch.setattr(importing, "reserve_staging", lambda *args: "too big")
    basename = "Too Big (2021) - [DVD].mkv"
    source = os.path.join(incoming_dir, basename)
    with open(sample_mkv, "rb") as f_in, open(source, "wb") as f_out:
        f_out.write(f_in.read())

    with app.app_context():
        assert localization_task(source) is True
        (move,) = [
            job
            for job in app.handoff_queue.jobs
            if job.func_name == "app.videos.move_localized_file"
        ]
        hidden = move.args[3]
        directory = os.path.join(app.config["LIBRARY_DIR"], "Movies/Too Big (2021)")
        assert hidden == os.path.join(directory, f".{basename}")
        assert os.listdir(app.config["STAGING_DIR"]) == []
        assert videos.move_localized_file(*move.args) is True
        (finalize,) = [
            job
            for job in app.sql_queue.jobs
            if job.func_name == "app.videos.finalize_localization"
        ]
        assert finalize.args[3] == hidden
        assert finalize.kwargs["staged_output"] is None
        finalize_localization(*finalize.args, **finalize.kwargs)

    output = os.path.join(directory, basename)
    assert os.path.exists(output)
    assert not os.path.exists(hidden)
    assert not os.path.exists(source)
    os.remove(output)


def test_an_unstaged_localization_that_fails_removes_its_partial_output(
    app, sample_mkv, incoming_dir, monkeypatch
):
    """Without staging, the partial output is a hidden library file (#389)."""

    from app import importing

    monkeypatch.setattr(importing, "reserve_staging", lambda *args: "too big")
    basename = "Half Written (2021) - [DVD].mkv"
    source = os.path.join(incoming_dir, basename)
    with open(sample_mkv, "rb") as f_in, open(source, "wb") as f_out:
        f_out.write(f_in.read())
    directory = os.path.join(app.config["LIBRARY_DIR"], "Movies/Half Written (2021)")
    hidden = os.path.join(directory, f".{basename}")

    def half_written(work_path, hidden_output_file, *args):
        os.makedirs(os.path.dirname(hidden_output_file), exist_ok=True)
        with open(hidden_output_file, "wb") as handle:
            handle.write(b"partial")
        raise subprocess.CalledProcessError(2, "mkvmerge")

    monkeypatch.setattr(importing, "_localize_matroska", half_written)
    with app.app_context():
        assert localization_task(source) is True
    assert not os.path.exists(hidden)
    assert basename in rejected_files(app)


def _track(language, streamorder):
    """Return a track dict of the shape that get_*_tracks_from_file gives."""

    return {"language": language, "streamorder": streamorder, "default": False}


@pytest.mark.parametrize(
    "native, audio, subtitles, expected",
    [
        # Native audio first, native subtitles: every native track is off.
        (
            "eng",
            [_track("eng", 1)],
            [_track("eng", 2), _track("fre", 3), _track("eng", 4)],
            ["-a", "eng", "-s", "eng"]
            + ["--default-track-flag", "2:0", "--default-track-flag", "4:0"],
        ),
        # Foreign audio first, native audio and subtitles present: the first
        # native subtitle track is on, and the others are off.
        (
            "eng",
            [_track("fre", 1), _track("eng", 2)],
            [_track("fre", 3), _track("eng", 4), _track("eng", 5)],
            ["-a", "fre,eng", "-s", "eng"]
            + ["--default-track-flag", "4:1", "--default-track-flag", "5:0"],
        ),
        # Foreign audio only, no native subtitles.
        (
            "eng",
            [_track("fre", 1)],
            [_track("fre", 2)],
            ["-a", "fre", "--no-subtitles"],
        ),
        # Native audio, no subtitles at all.
        ("eng", [_track("eng", 1)], [], ["-a", "eng", "--no-subtitles"]),
        # No audio tracks and no subtitles.
        ("eng", [], [], ["-a", "1", "--no-subtitles"]),
        # A MakeMKV file uses the ISO 639-3 code of the native language.
        (
            "fre",
            [_track("fra", 1)],
            [_track("fra", 2)],
            ["-a", "fra", "-s", "fra", "--default-track-flag", "2:0"],
        ),
        # A file that mkvmerge wrote reads back with the terminological
        # code. "ger" and "deu" are the same language (#423).
        (
            "ger",
            [_track("deu", 1)],
            [_track("deu", 2)],
            ["-a", "deu", "-s", "deu", "--default-track-flag", "2:0"],
        ),
        (
            "fre",
            [_track("eng", 1), _track("fra", 2)],
            [_track("fra", 3)],
            ["-a", "eng,fra", "-s", "fra", "--default-track-flag", "3:1"],
        ),
    ],
)
def test_the_mkvmerge_command_of_each_track_layout(
    app, sample_mkv, incoming_dir, monkeypatch, native, audio, subtitles, expected
):
    """Pin the mkvmerge arguments that the localization builds for each layout."""

    from app import importing

    monkeypatch.setitem(app.config, "NATIVE_LANGUAGE", native)
    monkeypatch.setattr(importing, "get_audio_tracks_from_file", lambda path: audio)
    monkeypatch.setattr(
        importing, "get_subtitle_tracks_from_file", lambda path: subtitles
    )
    if native == "fre" and audio[0]["language"] == "fra":
        real_parse = importing.MediaInfo.parse

        class MakeMKVMediaInfo:
            @staticmethod
            def parse(path, **kwargs):
                info = real_parse(path, **kwargs)
                for track in info.tracks:
                    if track.track_type == "General":
                        track.writing_application = "MakeMKV v1.17.4"
                return info

        monkeypatch.setattr(importing, "MediaInfo", MakeMKVMediaInfo)

    commands = []
    real_popen = subprocess.Popen

    def record(argv, **kwargs):
        if argv[0] == app.config["MKVMERGE_BIN"] and "-a" in argv:
            commands.append(argv)
            argv = [argv[0], "-o", argv[2], argv[-1]]
        return real_popen(argv, **kwargs)

    monkeypatch.setattr(importing.subprocess, "Popen", record)
    basename = "Track Layout (2021) - [DVD].mkv"
    source = os.path.join(incoming_dir, basename)
    with open(sample_mkv, "rb") as f_in, open(source, "wb") as f_out:
        f_out.write(f_in.read())
    try:
        with app.app_context():
            assert localization_task(source) is True
            (move,) = [
                job
                for job in app.handoff_queue.jobs
                if job.func_name == "app.videos.move_localized_file"
            ]
            hidden = move.args[3]
        (command,) = commands
        assert command[:3] == [app.config["MKVMERGE_BIN"], "-o", hidden]
        assert command[3:-5] == expected
        assert command[-5:-1] == ["--title", "", "--track-name", "-1:"]
        assert command[-1].startswith(app.config["STAGING_DIR"])
        os.remove(hidden)
    finally:
        if os.path.exists(source):
            os.remove(source)


def test_an_mp4_that_mkvmerge_cannot_carry_loses_its_metadata(
    app, sample_mp4, incoming_dir, monkeypatch
):
    """An MPEG-4 file that cannot become Matroska goes through AtomicParsley.

    Then the task copies it to its hidden output as it is. The job shows
    each step on the queue page."""

    from app import importing

    monkeypatch.setattr(importing, "convert_to_matroska", lambda *args: False)
    monkeypatch.setitem(app.config, "ATOMICPARSLEY_BIN", "/bin/echo")

    from tests.fakes import Job

    job = Job()
    monkeypatch.setattr(importing, "get_current_job", lambda: job)
    basename = "Plain Mp4 (2021) - [DVD].mp4"
    source = os.path.join(incoming_dir, basename)
    with open(sample_mp4, "rb") as f_in, open(source, "wb") as f_out:
        f_out.write(f_in.read())
    try:
        with app.app_context():
            assert localization_task(source) is True
            (move,) = [
                job
                for job in app.handoff_queue.jobs
                if job.func_name == "app.videos.move_localized_file"
            ]
            hidden = move.args[3]
        assert hidden.endswith(".mp4")
        assert os.path.getsize(hidden) == os.path.getsize(source)
        staged = os.path.join(app.config["STAGING_DIR"], basename)
        assert {
            "description": f"'{basename}' — Removing MPEG-4 metadata",
            "progress": -1,
        } in job.saved
        assert job.saved[-1] == {
            "description": f"'{basename}' — Copying to destination",
            "progress": -1,
        }
        assert not os.path.exists(staged)
        os.remove(hidden)
    finally:
        if os.path.exists(source):
            os.remove(source)


@pytest.mark.parametrize(
    "subtitles, foreign, expected",
    [
        # Foreign audio: the first native subtitle track is the default.
        (
            [_track("fre", 3), _track("eng", 4), _track("eng", 5)],
            True,
            ["-s", "eng", "--default-track-flag", "4:1", "--default-track-flag", "5:0"],
        ),
        # Native audio: no native subtitle track is a default.
        (
            [_track("eng", 2), _track("eng", 3)],
            False,
            ["-s", "eng", "--default-track-flag", "2:0", "--default-track-flag", "3:0"],
        ),
        # Foreign subtitles only, and no subtitles at all, build 1 command.
        ([_track("fre", 2)], True, ["--no-subtitles"]),
        ([_track("fre", 2)], False, ["--no-subtitles"]),
        ([], True, ["--no-subtitles"]),
        ([], False, ["--no-subtitles"]),
    ],
)
def test_build_localize_command(app, subtitles, foreign, expected):
    """The command builder is pure. It needs no file and no app context."""

    from app.importing import build_localize_command

    command = build_localize_command(
        "mkvmerge", "/in.mkv", "/out.mkv", "fre,eng", "eng", subtitles, foreign
    )
    assert command == (
        ["mkvmerge", "-o", "/out.mkv", "-a", "fre,eng"]
        + expected
        + ["--title", "", "--track-name", "-1:", "/in.mkv"]
    )


def test_the_two_spellings_of_a_language_match():
    """The terminological and bibliographic codes name 1 language (#423)."""

    from app.tracks import file_language_spelling, same_language

    assert same_language("ger", "deu") and same_language("fra", "fre")
    assert same_language("ENG", "eng")
    assert not same_language("ger", "eng")
    assert not same_language(None, "und")
    tracks = [{"language": "fra"}, {"language": "deu"}]
    assert file_language_spelling("ger", tracks) == "deu"
    assert file_language_spelling("eng", tracks) == "eng"


def test_report_progress_writes_the_queue_page_text(app):
    """report_progress names the file and the activity, and saves the meta."""

    from app.tasks import report_progress

    from tests.fakes import Job

    job = Job({"progress": 40})
    report_progress(job, "Film.mkv", "Downloading from AWS", percent=None)
    assert job.meta == {
        "description": "'Film.mkv' — Downloading from AWS",
        "progress": 40,
    }
    report_progress(job, None, "Pruning extra files", 7)
    assert job.meta == {"description": "Pruning extra files", "progress": 7}
    report_progress(job, "Film.mkv", "Cataloging")
    assert job.meta["progress"] == -1
    assert len(job.saved) == 3
    report_progress(None, "Film.mkv", "Cataloging")
