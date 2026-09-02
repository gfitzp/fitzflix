"""Test the default-flag rule on real Matroska files.

The rule from Glenn (2026-08-24, closed #194): the FIRST audio track is
always the default track. An E-AC-3 Atmos track is the default only when
it is the first track. This is important because Infuse plays the track
that has the flag. Thus, the flag decides what a viewer hears. The
channel count does not decide. The Plex display does not decide.

3 code paths write that flag: inspect_localized_file in the import, the
mkvpropedit editor, and the Atmos supplement. None of them had a test.
These tests build a file with 3 audio tracks with ffmpeg and mkvmerge.
That is the same harness that test_localization.py uses. They check the
rule end to end. This includes the half that the code does not make
obvious: when the caller selects a different default, Fitzflix REORDERS
the file. That track becomes the first track. Thus, "default" and
"first" stay the same thing.
"""

import os
import shutil
import subprocess

import pytest

from tests.conftest import _TMP


@pytest.fixture(scope="module")
def three_audio_mkv(app):
    """Build a 1-second Matroska with eng/fra/deu audio.

    The default flag is on the SECOND track on purpose. Thus, the rule
    has something to correct."""

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
    """Return (language, default) for each audio track, read the same way as the app."""

    from app.tracks import get_audio_tracks_from_file

    with app.app_context():
        return [
            (track["language"], bool(track["default"]))
            for track in get_audio_tracks_from_file(path)
        ]


def test_the_fixture_really_starts_out_wrong(app, three_audio_mkv):
    """Check the fixture itself.

    If mkvmerge stops obeying the flags that this fixture asks for, the
    tests below pass without proof of anything."""

    assert _audio(app, three_audio_mkv) == [
        ("eng", False),
        ("fra", True),
        ("deu", False),
    ]


def test_import_makes_the_first_audio_track_the_only_default(
    app, three_audio_mkv, tmp_path
):
    """Test the half of the rule in the import path.

    The flag on the disc is not important. The file that reaches the
    library has its first track as the default."""

    from app.importing import inspect_localized_file

    staged = str(tmp_path / "Flag Test (2021) - [Bluray-1080p].mkv")
    shutil.copy(three_audio_mkv, staged)

    with app.app_context():
        inspect_localized_file(staged, "Matroska")

    assert _audio(app, staged) == [("eng", True), ("fra", False), ("deu", False)]


def test_choosing_a_later_default_moves_it_to_the_front(app, three_audio_mkv):
    """Test the half of the rule that the flag edit alone does not show.

    When the caller selects a default that is not first, Fitzflix remuxes
    the file. That track becomes first. This keeps "the default track"
    and "the first track" the same thing. The Atmos twins depend on this
    invariant."""

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

    # Ask for the SECOND track (fra) as the default.

    with app.app_context():
        assert mkvpropedit_unlocked(file_id, 2, None, None) is True

    # French is now first, and it is the only default. The other 2 tracks
    # keep their order behind it.

    assert _audio(app, library_path) == [
        ("fra", True),
        ("eng", False),
        ("deu", False),
    ]


def test_the_stored_track_rows_follow_the_reordered_file(app, three_audio_mkv):
    """Make sure that the rule also reaches the database.

    The File page and the Atmos candidate search read the track rows.
    Fitzflix rebuilds those rows from the reordered file, not from the
    order that the caller asked about."""

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
