"""Test the remux of a library file through staging (#298)."""

import os
import shutil
import subprocess

import pytest

import app.importing as importing
import app.tracks as tracks
from app.importing import STAGING_RESERVED_KEY

from tests.conftest import _TMP


@pytest.fixture(scope="module")
def two_audio_mkv(app):
    """Build a 1-second Matroska file with 2 audio tracks."""

    path = os.path.join(_TMP, "sample-two-audio.mkv")
    if not os.path.exists(path):
        subprocess.run(
            [
                app.config["FFMPEG_BIN"],
                "-f",
                "lavfi",
                "-i",
                "testsrc=duration=1:size=320x240:rate=10",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=1",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=880:duration=1",
                "-map",
                "0",
                "-map",
                "1",
                "-map",
                "2",
                "-c:v",
                "libx264",
                "-c:a",
                "aac",
                "-shortest",
                "-y",
                path,
            ],
            check=True,
            capture_output=True,
        )
    return path


@pytest.fixture
def library_file(app, two_audio_mkv):
    """Copy the sample into a library folder and return its path."""

    directory = os.path.join(app.config["MOVIE_LIBRARY"], "Remux Subject (2000)")
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, "Remux Subject (2000) - [DVD].mkv")
    shutil.copy(two_audio_mkv, path)
    yield path
    shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def staging(app):
    """Return an empty staging directory."""

    path = app.config["STAGING_DIR"]
    os.makedirs(path, exist_ok=True)
    for name in os.listdir(path):
        os.remove(os.path.join(path, name))
    yield path
    for name in os.listdir(path):
        os.remove(os.path.join(path, name))


def _audio_count(app, path):
    """Return the number of audio tracks in a file."""

    with app.app_context():
        return len(tracks.get_audio_tracks_from_file(path))


def _drop_second_audio(app, seen):
    def build(source, output):
        seen.append((source, output))
        return [app.config["MKVMERGE_BIN"], "-o", output, "-a", "1", source]

    return build


def test_a_library_file_is_remuxed_on_staging(app, library_file, staging, monkeypatch):
    monkeypatch.setattr(tracks, "_on_volume_of", lambda path, directory: False)
    seen = []
    with app.app_context():
        assert _audio_count(app, library_file) == 2
        tracks.remux_file(
            library_file, _drop_second_audio(app, seen), None, "x", "Remuxing"
        )
        assert _audio_count(app, library_file) == 1

        # mkvmerge read and wrote on staging only.
        source, output = seen[0]
        assert source.startswith(staging) and output.startswith(staging)
        assert os.listdir(staging) == []
        assert os.listdir(os.path.dirname(library_file)) == [
            os.path.basename(library_file)
        ]
        assert not [
            field
            for field in app.redis.hkeys(STAGING_RESERVED_KEY)
            if field.startswith(b"remux:")
        ]


def test_a_failed_remux_leaves_the_library_file_alone(
    app, library_file, staging, monkeypatch
):
    monkeypatch.setattr(tracks, "_on_volume_of", lambda path, directory: False)
    before = os.path.getsize(library_file)
    with app.app_context():
        with pytest.raises(subprocess.CalledProcessError):
            tracks.remux_file(
                library_file,
                lambda source, output: [
                    app.config["MKVMERGE_BIN"],
                    "-o",
                    output,
                    "--no-such-option",
                    source,
                ],
                None,
                "x",
                "Remuxing",
            )
        assert os.path.getsize(library_file) == before
        assert _audio_count(app, library_file) == 2
        assert os.listdir(staging) == []
        assert os.listdir(os.path.dirname(library_file)) == [
            os.path.basename(library_file)
        ]
        assert not [
            field
            for field in app.redis.hkeys(STAGING_RESERVED_KEY)
            if field.startswith(b"remux:")
        ]


def test_without_staging_space_the_remux_runs_in_place(
    app, library_file, staging, monkeypatch
):
    monkeypatch.setattr(tracks, "_on_volume_of", lambda path, directory: False)
    monkeypatch.setattr(importing, "reserve_staging", lambda *a, **k: "wait")
    seen = []
    with app.app_context():
        tracks.remux_file(
            library_file, _drop_second_audio(app, seen), None, "x", "Remuxing"
        )
    source, output = seen[0]
    assert source == library_file
    assert os.path.dirname(output) == os.path.dirname(library_file)
    assert _audio_count(app, library_file) == 1
    assert os.listdir(staging) == []


def test_a_file_on_staging_is_remuxed_in_place(app, two_audio_mkv, staging):
    path = os.path.join(staging, ".Import Subject (2000) - [DVD].mkv")
    shutil.copy(two_audio_mkv, path)
    seen = []
    with app.app_context():
        tracks.remux_file(path, _drop_second_audio(app, seen), None, "x", "Remuxing")
    source, output = seen[0]
    assert source == path
    assert os.path.dirname(output) == staging
    assert _audio_count(app, path) == 1
    assert os.listdir(staging) == [os.path.basename(path)]


def test_extras_with_the_same_name_get_their_own_staging_files(
    app, two_audio_mkv, staging, monkeypatch
):
    """Many films have a "Trailer.mkv". Their remuxes must not share files."""

    monkeypatch.setattr(tracks, "_on_volume_of", lambda path, directory: False)
    paths = []
    for film in ("First Film (2001)", "Second Film (2002)"):
        directory = os.path.join(app.config["MOVIE_LIBRARY"], film, "Trailers")
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "Trailer.mkv")
        shutil.copy(two_audio_mkv, path)
        paths.append(path)
    seen = []
    try:
        with app.app_context():
            for path in paths:
                tracks.remux_file(
                    path, _drop_second_audio(app, seen), None, "x", "Remuxing"
                )
        (first_source, first_output), (second_source, second_output) = seen
        assert first_source != second_source and first_output != second_output
        assert all(_audio_count(app, path) == 1 for path in paths)
    finally:
        for film in ("First Film (2001)", "Second Film (2002)"):
            shutil.rmtree(
                os.path.join(app.config["MOVIE_LIBRARY"], film), ignore_errors=True
            )
