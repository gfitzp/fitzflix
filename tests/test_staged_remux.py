"""Test the remux of a library file through staging (#298)."""

import os
import shutil
import subprocess

import pytest

import app.importing as importing
import app.tracks as tracks
from app.importing import STAGING_RESERVED_KEY

from tests.conftest import _TMP
from tests.media import make_sample_mkv


@pytest.fixture(scope="session")
def two_audio_mkv(app):
    """Build a 1-second Matroska file with 2 audio tracks."""

    return make_sample_mkv(
        os.path.join(_TMP, "sample-two-audio.mkv"),
        audio=(("und", True), ("und", False)),
    )


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


def test_an_in_place_remux_checks_the_space_first(app, library_file, monkeypatch):
    """A remux that runs out of space fills the shared SSD. Check before writing."""

    import errno
    from types import SimpleNamespace

    monkeypatch.setattr(tracks, "_on_volume_of", lambda path, directory: True)
    monkeypatch.setattr(
        tracks.shutil, "disk_usage", lambda path: SimpleNamespace(free=0)
    )
    ran = []
    with app.app_context():
        with pytest.raises(OSError) as raised:
            tracks.remux_file(
                library_file,
                lambda source, output: ran.append(output) or ["true"],
                None,
                "x",
                "Remuxing",
            )
    assert raised.value.errno == errno.ENOSPC
    assert ran == []
    assert _audio_count(app, library_file) == 2


def test_a_busy_reservation_lock_tries_again_and_then_remuxes_in_place(
    app, library_file, staging, monkeypatch
):
    """A busy lock was read as no space on the first answer (#569).

    The first fix raised a transient error after the tries. The track
    scan, the audio plan and the post-reorder step book no retry for
    it, so the job failed (#569 follow-up). Now the remux runs in place
    after the tries, as it does for a lack of space."""

    monkeypatch.setattr(tracks, "_on_volume_of", lambda path, directory: False)
    answers = []
    monkeypatch.setattr(
        importing,
        "reserve_staging",
        lambda *args: answers.append("lock") or "lock",
    )
    seen = []
    with app.app_context():
        tracks.remux_file(
            library_file, _drop_second_audio(app, seen), None, "x", "Remuxing"
        )
    assert answers == ["lock"] * tracks.REMUX_RESERVATION_TRIES
    source, output = seen[0]
    assert os.path.dirname(output) == os.path.dirname(library_file)
    assert _audio_count(app, library_file) == 1
    assert os.listdir(staging) == []


def test_a_reservation_lock_that_frees_up_remuxes_on_staging(
    app, library_file, staging, monkeypatch
):
    monkeypatch.setattr(tracks, "_on_volume_of", lambda path, directory: False)
    real = importing.reserve_staging
    answers = ["lock"]
    monkeypatch.setattr(
        importing,
        "reserve_staging",
        lambda *args: answers.pop(0) if answers else real(*args),
    )
    seen = []
    with app.app_context():
        tracks.remux_file(
            library_file, _drop_second_audio(app, seen), None, "x", "Remuxing"
        )
    source, output = seen[0]
    assert source.startswith(staging) and output.startswith(staging)
    assert _audio_count(app, library_file) == 1


def test_a_stray_that_stays_after_the_rename_does_not_fail_the_remux(
    app, library_file, staging, monkeypatch
):
    """The cleanup runs after the rename. A fault there only warns (#582).

    Before, the OSError of the cleanup reached the caller. The caller
    read it as a fault before the restructure, and a retry with the old
    track numbers was then thought to be safe."""

    monkeypatch.setattr(tracks, "_on_volume_of", lambda path, directory: False)
    real_remove = os.remove

    def stuck(path, *args, **kwargs):
        if os.path.basename(path).startswith(".remux-"):
            raise OSError(116, "Stale NFS file handle")
        return real_remove(path, *args, **kwargs)

    seen = []
    with app.app_context():
        with monkeypatch.context() as patch:
            patch.setattr(tracks.os, "remove", stuck)
            tracks.remux_file(
                library_file, _drop_second_audio(app, seen), None, "x", "Remuxing"
            )
        assert _audio_count(app, library_file) == 1
        assert not [
            field
            for field in app.redis.hkeys(STAGING_RESERVED_KEY)
            if field.startswith(b"remux:")
        ]
