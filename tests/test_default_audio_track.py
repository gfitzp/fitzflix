"""The default-flag rule, on real Matroska files.

Glenn's rule (Aug 24 2026, closing #194): the FIRST audio track is
always the default track, and an E-AC-3 Atmos track is the default only
when it is the first track. It matters because Infuse plays whichever
track carries the flag — so the flag, not the channel count and not what
Plex displays, decides what a viewer hears.

Three code paths write that flag (the import's inspect_localized_file,
the mkvpropedit editor, and the Atmos supplement), and none of them had
a test. These build a three-audio-track file with ffmpeg and mkvmerge —
the harness test_localization.py already uses — and check the rule end
to end, including the half that isn't obvious from reading the code:
choosing a different default REORDERS the file so that track becomes
first, which is what keeps "default" and "first" the same thing.
"""

import os
import shutil
import subprocess

import pytest

from tests.conftest import _TMP


@pytest.fixture(scope="module")
def three_audio_mkv(app):
    """A 1-second Matroska with eng/fra/deu audio, its default flag
    deliberately on the SECOND track so the rule has something to fix."""

    base = os.path.join(_TMP, "three-audio-base.mp4")
    mkv = os.path.join(_TMP, "three-audio.mkv")
    if not os.path.exists(mkv):
        subprocess.run(
            [app.config["FFMPEG_BIN"]]
            + ["-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=10"]
            + ["-f", "lavfi", "-i", "sine=frequency=440:duration=1"]
            + ["-f", "lavfi", "-i", "sine=frequency=880:duration=1"]
            + ["-f", "lavfi", "-i", "sine=frequency=1320:duration=1"]
            + ["-map", "0:v", "-map", "1:a", "-map", "2:a", "-map", "3:a"]
            + ["-c:v", "libx264", "-c:a", "aac", "-shortest", "-y", base],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [app.config["MKVMERGE_BIN"], "-o", mkv]
            + ["--language", "1:eng", "--language", "2:fra", "--language", "3:deu"]
            + ["--default-track", "1:0"]
            + ["--default-track", "2:1"]
            + ["--default-track", "3:0"]
            + [base],
            check=True,
            capture_output=True,
        )
    return mkv


def _audio(app, path):
    """(language, default) per audio track, read the way the app reads it."""

    from app.tracks import get_audio_tracks_from_file

    with app.app_context():
        return [
            (track["language"], bool(track["default"]))
            for track in get_audio_tracks_from_file(path)
        ]


def test_the_fixture_really_starts_out_wrong(app, three_audio_mkv):
    """Guard on the guard: if mkvmerge ever stopped honoring the flags
    this fixture asks for, the tests below would pass without proving
    anything."""

    assert _audio(app, three_audio_mkv) == [
        ("eng", False),
        ("fra", True),
        ("deu", False),
    ]


def test_import_makes_the_first_audio_track_the_only_default(
    app, three_audio_mkv, tmp_path
):
    """The import path's half of the rule: whatever the disc flagged,
    the file that reaches the library defaults to its first track."""

    from app.importing import inspect_localized_file

    staged = str(tmp_path / "Flag Test (2021) - [Bluray-1080p].mkv")
    shutil.copy(three_audio_mkv, staged)

    with app.app_context():
        inspect_localized_file(staged, "Matroska")

    assert _audio(app, staged) == [("eng", True), ("fra", False), ("deu", False)]


def test_choosing_a_later_default_moves_it_to_the_front(app, three_audio_mkv):
    """The half that isn't obvious from the flag edit alone: picking a
    non-first default remuxes the file so that track becomes first. That
    is what keeps "the default track" and "the first track" the same
    thing — the invariant the Atmos twins rely on."""

    from app import db
    from app.tracks import mkvpropedit_unlocked
    from tests.factories import make_movie, make_movie_file

    with app.app_context():
        movie = make_movie("Reorder Test", 2021)
        file = make_movie_file(movie, "Bluray-1080p")
        db.session.commit()
        file_id = file.id
        library_path = os.path.join(app.config["LIBRARY_DIR"], file.file_path)

    os.makedirs(os.path.dirname(library_path), exist_ok=True)
    shutil.copy(three_audio_mkv, library_path)

    # Ask for the SECOND track (fra) as the default

    with app.app_context():
        assert mkvpropedit_unlocked(file_id, 2, None, None) is True

    # French is now first, and the only default — the other two keep
    # their order behind it

    assert _audio(app, library_path) == [
        ("fra", True),
        ("eng", False),
        ("deu", False),
    ]


def test_the_stored_track_rows_follow_the_reordered_file(app, three_audio_mkv):
    """The rule has to survive into the database too: the rows the File
    page and the Atmos candidate search read are rebuilt from the
    reordered file, not from the order the caller asked about."""

    from app import db
    from app.models import FileAudioTrack
    from app.tracks import mkvpropedit_unlocked
    from tests.factories import make_movie, make_movie_file

    with app.app_context():
        movie = make_movie("Row Order Test", 2021)
        file = make_movie_file(movie, "Bluray-1080p")
        db.session.commit()
        file_id = file.id
        library_path = os.path.join(app.config["LIBRARY_DIR"], file.file_path)

    os.makedirs(os.path.dirname(library_path), exist_ok=True)
    shutil.copy(three_audio_mkv, library_path)

    with app.app_context():
        assert mkvpropedit_unlocked(file_id, 3, None, None) is True

        rows = (
            FileAudioTrack.query.filter_by(file_id=file_id)
            .order_by(FileAudioTrack.track)
            .all()
        )
        assert [(row.track, row.language, bool(row.default)) for row in rows] == [
            (1, "deu", True),
            (2, "eng", False),
            (3, "fra", False),
        ]
