"""evaluate_filename: the naming rules documented in the README's How-to-use
tables, plus edge cases characterized from current behavior.

These run offline: TMDB is unreachable in TestConfig, so titles and years
always come straight from the filename.
"""

import logging

import pytest

from app.videos import evaluate_filename

# (filename, expected library file path) — the README examples

README_CASES = [
    (
        "Jaws (1975) - [Bluray-1080p].mkv",
        "Movies/Jaws (1975)/Jaws (1975) - [Bluray-1080p].mkv",
    ),
    (
        "Blade Runner (1982) {edition-Final Cut} - [Bluray-2160p].mkv",
        "Movies/Blade Runner (1982) {edition-Final Cut}/Blade Runner (1982) {edition-Final Cut} - [Bluray-2160p].mkv",
    ),
    (
        "The Terminator (1984) - Fullscreen [DVD].mkv",
        "Movies/The Terminator (1984)/The Terminator (1984) - Full Screen [DVD].mkv",
    ),
    (
        "Jaws (1975) - Behind The Scenes - The Making of Jaws [DVD].mkv",
        "Movies/Jaws (1975)/Behind The Scenes/The Making of Jaws.mkv",
    ),
    (
        "Jaws (1975) - Deleted Scenes - Alternate Ending [DVD].mkv",
        "Movies/Jaws (1975)/Deleted Scenes/Alternate Ending.mkv",
    ),
    (
        "Jaws (1975) - Featurettes - From the Set [DVD].mkv",
        "Movies/Jaws (1975)/Featurettes/From the Set.mkv",
    ),
    (
        "Jaws (1975) - Interviews - A Conversation with Steven Spielberg [DVD].mkv",
        "Movies/Jaws (1975)/Interviews/A Conversation with Steven Spielberg.mkv",
    ),
    (
        "Jaws (1975) - Scenes - Opening Scene [DVD].mkv",
        "Movies/Jaws (1975)/Scenes/Opening Scene.mkv",
    ),
    (
        "Jaws (1975) - Shorts - The Shark Is Not Working [DVD].mkv",
        "Movies/Jaws (1975)/Shorts/The Shark Is Not Working.mkv",
    ),
    (
        "Jaws (1975) - Trailers - Theatrical Trailer [DVD].mkv",
        "Movies/Jaws (1975)/Trailers/Theatrical Trailer.mkv",
    ),
    (
        "Jaws (1975) - Other - Storyboard Gallery [DVD].mkv",
        "Movies/Jaws (1975)/Other/Storyboard Gallery.mkv",
    ),
    (
        "Doctor Who (2005) - S01E01 - [DVD].mkv",
        "TV Shows/Doctor Who (2005)/Season 01/Doctor Who (2005) - S01E01 - [DVD].mkv",
    ),
    (
        "Doctor Who (2005) - S00E01 - The Christmas Invasion [HDTV-1080p].mkv",
        "TV Shows/Doctor Who (2005)/Specials/Doctor Who (2005) - S00E01 - The Christmas Invasion [HDTV-1080p].mkv",
    ),
    (
        "Planet Earth - S01E05-E06 - [Bluray-1080p].mkv",
        "TV Shows/Planet Earth/Season 01/Planet Earth - S01E05-E06 - [Bluray-1080p].mkv",
    ),
]


@pytest.mark.parametrize("filename,expected_path", README_CASES)
def test_readme_examples(app, filename, expected_path):
    with app.app_context():
        details = evaluate_filename(filename, log=False)
    assert details, f"{filename} was rejected"
    assert details["file_path"] == expected_path


# Characterized behavior for combinations the README doesn't spell out

EDGE_CASES = [
    (
        # "Full Screen" moves after the version string when no special feature
        "Big Hit (1999) - Fullscreen - Director's Cut [DVD].mkv",
        {
            "file_path": "Movies/Big Hit (1999)/Big Hit (1999) - Director's Cut - Full Screen [DVD].mkv",
            "plex_title": "Big Hit (1999) - Director's Cut",
            "edition": "Director's Cut",
            "fullscreen": True,
        },
    ),
    (
        # ...but never moves past a special feature type (the "Clang Clang
        # Boogie" case documented in evaluate_filename's comments)
        "Clang Clang Boogie (2019) - Fullscreen - Interviews - I Like Salad [Bluray-1080p].mkv",
        {
            "file_path": "Movies/Clang Clang Boogie (2019)/Interviews/I Like Salad.mkv",
            "plex_title": "I Like Salad",
            "feature_type_name": "Interviews",
            "fullscreen": True,
        },
    ),
    (
        "Blade Runner (1982) {edition-Final Cut} - Fullscreen [DVD].mkv",
        {
            "file_path": "Movies/Blade Runner (1982) {edition-Final Cut}/Blade Runner (1982) {edition-Final Cut} - Full Screen [DVD].mkv",
            "plex_title": "Blade Runner (1982) {edition-Final Cut}",
            # A fullscreen copy of an edition reports the edition name, so it
            # ranks and locks alongside the widescreen copy of that edition
            "edition": "Final Cut",
            "fullscreen": True,
        },
    ),
    (
        "Dune (1984) {edition-Extended} - Alternate Cut [DVD].mkv",
        {
            "file_path": "Movies/Dune (1984) {edition-Extended}/Dune (1984) {edition-Extended} - Alternate Cut [DVD].mkv",
            "plex_title": "Dune (1984) {edition-Extended} - Alternate Cut",
            "edition": "Alternate Cut",
        },
    ),
    (
        "Doctor Who (2005) - S01E01 - Director's Cut [DVD].mkv",
        {
            "file_path": "TV Shows/Doctor Who (2005)/Season 01/Doctor Who (2005) - S01E01 - Director's Cut [DVD].mkv",
            "plex_title": "Doctor Who (2005) - S01E01 - Director's Cut",
            "edition": "Director's Cut",
            "season": 1,
            "episode": 1,
            "last_episode": 1,
        },
    ),
    (
        "Friends - S03E07 - Fullscreen [DVD].mkv",
        {
            "file_path": "TV Shows/Friends/Season 03/Friends - S03E07 - Full Screen [DVD].mkv",
            "plex_title": "Friends - S03E07 - Full Screen",
            "edition": "Full Screen",
            "fullscreen": True,
            "season": 3,
            "episode": 7,
        },
    ),
    (
        # Accented characters are transliterated in paths but kept in titles
        "Amélie (2001) - [DVD].mkv",
        {
            "file_path": "Movies/Amelie (2001)/Amelie (2001) - [DVD].mkv",
            "plex_title": "Amélie (2001)",
            "title": "Amélie",
        },
    ),
]


@pytest.mark.parametrize(
    "filename,expected", EDGE_CASES, ids=[case[0] for case in EDGE_CASES]
)
def test_edge_cases(app, filename, expected):
    with app.app_context():
        details = evaluate_filename(filename, log=False)
    assert details, f"{filename} was rejected"
    for key, value in expected.items():
        assert details[key] == value, f"{key}: {details[key]!r} != {value!r}"


@pytest.mark.parametrize(
    "filename",
    [
        "Jaws (1975) - [Bluray-4K].mkv",  # unknown quality title
        "Friends - S01E01 - [Ultra-HD].mkv",  # unknown quality title (TV)
        "Jaws - [DVD].mkv",  # movie without a year
        "totally random file.mkv",  # matches neither format
        "Jaws (1975).mkv",  # no quality tag at all
    ],
)
def test_rejected_filenames(app, filename):
    with app.app_context():
        assert evaluate_filename(filename, log=False) is False


def test_quiet_mode_emits_no_log_lines(app, fake_tmdb, log_capture):
    """log=False (the admin filename tester) must not write import log lines."""

    with app.app_context():
        quiet = evaluate_filename("Jaws (1975) - [DVD].mkv", log=False)
        quiet_lines = [r for r in log_capture if r.levelno >= logging.INFO]
        loud = evaluate_filename("Jaws (1975) - [DVD].mkv")
        loud_lines = [r for r in log_capture if r.levelno >= logging.INFO]

    assert quiet and loud and quiet["file_path"] == loud["file_path"]
    assert not quiet_lines
    assert loud_lines


def test_yeared_name_attaches_to_bare_series_when_year_matches(app):
    """Sonarr now names new files "Title (Year)": the
    yeared form must land on the existing bare-titled record when the
    year matches its first-air year, not split into a second series."""

    from datetime import datetime

    from tests.factories import make_tv_series

    with app.app_context():
        make_tv_series("Top Gear", tmdb_first_air_date=datetime(2002, 10, 20))
        details = evaluate_filename(
            "Top Gear (2002) - S05E01 - [WEBDL-1080p].mkv", log=False
        )
        assert details["title"] == "Top Gear"
        assert details["file_path"] == (
            "TV Shows/Top Gear/Season 05/Top Gear - S05E01 - [WEBDL-1080p].mkv"
        )

        # A different year is a different show and keeps its own name
        wrong_year = evaluate_filename(
            "Top Gear (1978) - S01E01 - [DVD].mkv", log=False
        )
        assert wrong_year["title"] == "Top Gear (1978)"


def test_bare_name_attaches_to_unique_year_suffixed_series(app):
    """The inverse direction after a series rename: a stray bare-named
    file lands on the year-suffixed record — but only when exactly one
    candidate exists."""

    from tests.factories import make_tv_series

    with app.app_context():
        make_tv_series("Batman (1966)")
        details = evaluate_filename("Batman - S01E10 - [Bluray-1080p].mkv", log=False)
        assert details["title"] == "Batman (1966)"

        make_tv_series("Doctor Who (1963)")
        make_tv_series("Doctor Who (2005)")
        ambiguous = evaluate_filename("Doctor Who - S01E01 - [DVD].mkv", log=False)
        assert ambiguous["title"] == "Doctor Who"


def test_importable_basename_skips_hidden_and_transient_names():
    """The import sweeps only ever enqueue finished files (#244): hidden
    names and transfer tools' intermediate artifacts stay invisible —
    their promotion to the real name is what fires the import."""

    from app import importable_basename

    assert importable_basename("Jaws (1975) - [Bluray-1080p].mkv")
    assert not importable_basename(".Jaws (1975) - [Bluray-1080p].mkv")
    assert not importable_basename("Jaws (1975) - [Bluray-1080p].mkv.staged")
    assert not importable_basename("Jaws (1975) - [Bluray-1080p].mkv.partial")
    assert not importable_basename("Jaws (1975) - [Bluray-1080p].mkv.part")
    assert not importable_basename("Jaws (1975) - [Bluray-1080p].mkv.tmp")
    assert not importable_basename("Jaws (1975) - [Bluray-1080p].mkv.filepart")
    assert not importable_basename("Jaws (1975) - [Bluray-1080p].mkv.crdownload")
