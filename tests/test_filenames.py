"""Test evaluate_filename against the naming rules of the README.

The README documents the rules in its How-to-use tables. The edge cases
record the current behavior.

These tests run offline. TMDB is unreachable in TestConfig. Thus, the
titles and the years always come directly from the filename.
"""

import logging

import pytest

from app.videos import evaluate_filename

# The README examples: (filename, expected library file path)

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


# The recorded behavior for the combinations that the README does not
# describe

EDGE_CASES = [
    (
        # "Full Screen" moves after the version string if there is no
        # special feature
        "Big Hit (1999) - Fullscreen - Director's Cut [DVD].mkv",
        {
            "file_path": "Movies/Big Hit (1999)/Big Hit (1999) - Director's Cut - Full Screen [DVD].mkv",
            "plex_title": "Big Hit (1999) - Director's Cut",
            "edition": "Director's Cut",
            "fullscreen": True,
        },
    ),
    (
        # But it never moves past a special feature type. This is the
        # "Clang Clang Boogie" case in the comments of evaluate_filename.
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
            # A fullscreen copy of an edition reports the edition name.
            # Thus, it ranks and locks with the widescreen copy of that
            # edition.
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
        # Fitzflix transliterates accented characters in paths. It keeps
        # them in titles.
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
        "Jaws (1975) - [Bluray-4K].mkv",  # an unknown quality title
        "Friends - S01E01 - [Ultra-HD].mkv",  # an unknown quality title (TV)
        "Jaws - [DVD].mkv",  # a movie without a year
        "totally random file.mkv",  # matches no format
        "Jaws (1975).mkv",  # no quality tag at all
        "Jaws (1975) {bogus-123} - [DVD].mkv",  # an unknown brace tag kind
        "Jaws (1975) {imdb-0073195} - [DVD].mkv",  # the imdb id has no tt prefix
        "Hamilton {edition-Broadway} - [DVD].mkv",  # a yearless name needs an id tag
    ],
)
def test_rejected_filenames(app, filename):
    with app.app_context():
        assert evaluate_filename(filename, log=False) is False


# Plex external-id tags (#155): {tmdb-NNN}, {imdb-ttNNN}, {tvdb-NNN} after
# the year, in either order with {edition-...}. These tests run offline.
# Thus, an imdb or tvdb tag that cannot be resolved stays as it is. The
# tests further down cover the resolution paths with fakes and library
# records.

ID_TAG_CASES = [
    (
        "Hamilton (2025) {tmdb-556574} - [Bluray-1080p].mkv",
        {
            "file_path": "Movies/Hamilton (2025) {tmdb-556574}/Hamilton (2025) {tmdb-556574} - [Bluray-1080p].mkv",
            "plex_title": "Hamilton (2025) {tmdb-556574}",
            "title": "Hamilton",
            "year": 2025,
            "tmdb_id": 556574,
        },
    ),
    (
        # The id tag comes before the edition tag.
        "Blade Runner (1982) {tmdb-78} {edition-Final Cut} - [Bluray-2160p].mkv",
        {
            "file_path": "Movies/Blade Runner (1982) {tmdb-78} {edition-Final Cut}/Blade Runner (1982) {tmdb-78} {edition-Final Cut} - [Bluray-2160p].mkv",
            "plex_title": "Blade Runner (1982) {tmdb-78} {edition-Final Cut}",
            "edition": "Final Cut",
            "tmdb_id": 78,
        },
    ),
    (
        # The id tag comes after the edition tag. The library name
        # normalizes to id-then-edition.
        "Blade Runner (1982) {edition-Final Cut} {tmdb-78} - [Bluray-2160p].mkv",
        {
            "file_path": "Movies/Blade Runner (1982) {tmdb-78} {edition-Final Cut}/Blade Runner (1982) {tmdb-78} {edition-Final Cut} - [Bluray-2160p].mkv",
            "edition": "Final Cut",
            "tmdb_id": 78,
        },
    ),
    (
        # An imdb tag that cannot be resolved offline stays as it is.
        "Ran (1985) {imdb-tt0089881} - [Bluray-2160p].mkv",
        {
            "file_path": "Movies/Ran (1985) {imdb-tt0089881}/Ran (1985) {imdb-tt0089881} - [Bluray-2160p].mkv",
            "title": "Ran",
            "tmdb_id": None,
        },
    ),
    (
        "Ran (1985) {tvdb-3839} - [Bluray-2160p].mkv",
        {
            "file_path": "Movies/Ran (1985) {tvdb-3839}/Ran (1985) {tvdb-3839} - [Bluray-2160p].mkv",
            "tmdb_id": None,
        },
    ),
    (
        # Version strings and Full Screen still work together with a tag.
        "Big Hit (1999) {tmdb-9737} - Fullscreen - Director's Cut [DVD].mkv",
        {
            "file_path": "Movies/Big Hit (1999) {tmdb-9737}/Big Hit (1999) {tmdb-9737} - Director's Cut - Full Screen [DVD].mkv",
            "plex_title": "Big Hit (1999) {tmdb-9737} - Director's Cut",
            "fullscreen": True,
        },
    ),
    (
        # A special features file goes under the tagged movie folder.
        "Jaws (1975) {tmdb-578} - Trailers - Theatrical Trailer [DVD].mkv",
        {
            "file_path": "Movies/Jaws (1975) {tmdb-578}/Trailers/Theatrical Trailer.mkv",
            "plex_title": "Theatrical Trailer",
            "feature_type_name": "Trailers",
        },
    ),
    (
        # TV: the tag stays on the show folder, where Plex reads it.
        # Fitzflix removes it from the series title and episode filename.
        "Doctor Who (2005) {tmdb-57243} - S01E01 - [DVD].mkv",
        {
            "file_path": "TV Shows/Doctor Who (2005) {tmdb-57243}/Season 01/Doctor Who (2005) - S01E01 - [DVD].mkv",
            "title": "Doctor Who (2005)",
            "tmdb_id": 57243,
            "season": 1,
            "episode": 1,
        },
    ),
    (
        "Doctor Who (2005) {tvdb-78804} - S00E01 - The Christmas Invasion [HDTV-1080p].mkv",
        {
            "file_path": "TV Shows/Doctor Who (2005) {tvdb-78804}/Specials/Doctor Who (2005) - S00E01 - The Christmas Invasion [HDTV-1080p].mkv",
            "title": "Doctor Who (2005)",
            "tmdb_id": None,
        },
    ),
]


@pytest.mark.parametrize(
    "filename,expected", ID_TAG_CASES, ids=[case[0] for case in ID_TAG_CASES]
)
def test_external_id_tags(app, filename, expected):
    with app.app_context():
        details = evaluate_filename(filename, log=False)
    assert details, f"{filename} was rejected"
    for key, value in expected.items():
        assert details[key] == value, f"{key}: {details[key]!r} != {value!r}"


def test_tmdb_tag_adopts_existing_movie_record(app):
    """Test that a tmdb tag adopts the existing movie record.

    The id names the exact film. The name comes from the record that
    already owns the tmdb id, not from the spelling in the filename.
    This needs no network."""

    from tests.factories import make_movie

    with app.app_context():
        make_movie("Duck, You Sucker", 1971, tmdb_id=844)
        details = evaluate_filename(
            "A Fistful of Dynamite (1971) {tmdb-844} - [Bluray-1080p].mkv", log=False
        )
        assert details["title"] == "Duck, You Sucker"
        assert details["tmdb_id"] == 844
        assert details["file_path"] == (
            "Movies/Duck, You Sucker (1971) {tmdb-844}/"
            "Duck, You Sucker (1971) {tmdb-844} - [Bluray-1080p].mkv"
        )


def test_imdb_tag_resolves_through_library_record(app):
    """Test that an imdb tag resolves through the library record.

    A matched movie stores its imdb id. Thus, an imdb tag resolves to
    the canonical tmdb form while TMDB is unreachable."""

    from tests.factories import make_movie

    with app.app_context():
        make_movie("Ran", 1985, tmdb_id=11645, imdb_id="tt0089881")
        details = evaluate_filename(
            "Ran (1985) {imdb-tt0089881} {edition-Criterion} - [Bluray-2160p].mkv",
            log=False,
        )
        assert details["tmdb_id"] == 11645
        assert details["file_path"] == (
            "Movies/Ran (1985) {tmdb-11645} {edition-Criterion}/"
            "Ran (1985) {tmdb-11645} {edition-Criterion} - [Bluray-2160p].mkv"
        )


def test_yearless_form_resolves_year_from_library(app):
    """Test that the yearless form takes its year from the library.

    The yearless "Title {tmdb-NNN}" form of Plex files under the year
    that the id resolves to."""

    from tests.factories import make_movie

    with app.app_context():
        make_movie("Hamilton", 2020, tmdb_id=556574)
        details = evaluate_filename(
            "Hamilton {tmdb-556574} - [Bluray-1080p].mkv", log=False
        )
        assert details["year"] == 2020
        assert details["file_path"] == (
            "Movies/Hamilton (2020) {tmdb-556574}/"
            "Hamilton (2020) {tmdb-556574} - [Bluray-1080p].mkv"
        )


def test_yearless_form_rejected_when_id_unresolvable(app):
    """Test that Fitzflix rejects the yearless form when the id is unresolvable.

    With no library record and no reachable TMDB, there is no year to
    file under. Fitzflix rejects the file with a reason. It never
    guesses."""

    with app.app_context():
        details = evaluate_filename(
            "Hamilton {tmdb-556574} - [Bluray-1080p].mkv", log=False
        )
    assert not details
    assert details is not False
    assert details.reason == "id not resolvable"


def test_unknown_external_id_rejected_loudly(app, fake_tmdb):
    """Test that an unknown external id is a loud reject.

    When /find answers with no results, Fitzflix rejects the file. A
    fallback to a title search could attach the wrong film (#155)."""

    with app.app_context():
        details = evaluate_filename(
            "Ran (1985) {imdb-tt9999999} - [Bluray-2160p].mkv", log=False
        )
    assert not details
    assert details is not False
    assert details.reason == "id not found"


def test_unknown_tmdb_id_rejected_loudly(app, monkeypatch):
    """Test that an unknown tmdb id is a loud reject.

    A 404 on /movie/<id> for an id that the filename named is a reject.
    It is not the usual tolerated TMDB glitch."""

    import requests

    import app.models as fitzflix_models

    class NotFoundResponse:
        status_code = 404

        def json(self):
            return {"status_message": "not found"}

        def raise_for_status(self):
            error = requests.exceptions.HTTPError("404")
            error.response = self
            raise error

    monkeypatch.setattr(
        fitzflix_models.requests, "get", lambda *args, **kwargs: NotFoundResponse()
    )

    with app.app_context():
        details = evaluate_filename(
            "Hamilton (2025) {tmdb-999999999} - [Bluray-1080p].mkv", log=False
        )
    assert not details
    assert details is not False
    assert details.reason == "id not found"


def test_tv_tag_adopts_existing_series_record(app):
    """Test that a series id tag adopts the existing series record.

    A series id tag puts the file on the record that owns the id. The
    name of the show in the filename is not important."""

    from tests.factories import make_tv_series

    with app.app_context():
        make_tv_series("Doctor Who (2005)", tmdb_id=57243)
        details = evaluate_filename(
            "Doctor Who {tmdb-57243} - S01E01 - [DVD].mkv", log=False
        )
        assert details["title"] == "Doctor Who (2005)"
        assert details["file_path"] == (
            "TV Shows/Doctor Who (2005) {tmdb-57243}/Season 01/"
            "Doctor Who (2005) - S01E01 - [DVD].mkv"
        )


def test_reconstruct_filename_carries_id_tag(app):
    """Test that a reconstructed filename keeps its id tag.

    An untouched name that came in with an id tag keeps a tag when
    Fitzflix reconstructs it. The tag is upgraded to the current tmdb id
    of the record (#155)."""

    from app.importing import reconstruct_filename
    from tests.factories import make_movie, make_movie_file

    with app.app_context():
        movie = make_movie("Hamilton", 2020, tmdb_id=556574)
        tagged = make_movie_file(
            movie,
            "Bluray-1080p",
            untouched_basename="Hamilton (2025) {tmdb-556574} - [Bluray-1080p].mkv",
        )
        assert reconstruct_filename(tagged.id) == (
            "Hamilton (2020) {tmdb-556574} - [Bluray-1080p].mkv"
        )

        # A name that came in without a tag does not get one.

        plain = make_movie_file(
            movie, "DVD", untouched_basename="Hamilton (2020) - [DVD].mkv"
        )
        assert reconstruct_filename(plain.id) == "Hamilton (2020) - [DVD].mkv"


def test_tv_tvdb_tag_resolves_through_library_record(app):
    """Test that a tvdb tag resolves through the series record.

    A tvdb tag on a show folder resolves through the stored external ids
    of the series record. It normalizes to the tmdb form."""

    from tests.factories import make_tv_series

    with app.app_context():
        make_tv_series("Doctor Who (2005)", tmdb_id=57243, tvdb_id=78804)
        details = evaluate_filename(
            "Doctor Who (2005) {tvdb-78804} - S01E01 - [DVD].mkv", log=False
        )
        assert details["tmdb_id"] == 57243
        assert details["file_path"] == (
            "TV Shows/Doctor Who (2005) {tmdb-57243}/Season 01/"
            "Doctor Who (2005) - S01E01 - [DVD].mkv"
        )


def test_quiet_mode_emits_no_log_lines(app, fake_tmdb, log_capture):
    """Test that log=False (the admin filename tester) writes no import log lines."""

    with app.app_context():
        quiet = evaluate_filename("Jaws (1975) - [DVD].mkv", log=False)
        quiet_lines = [r for r in log_capture if r.levelno >= logging.INFO]
        loud = evaluate_filename("Jaws (1975) - [DVD].mkv")
        loud_lines = [r for r in log_capture if r.levelno >= logging.INFO]

    assert quiet and loud and quiet["file_path"] == loud["file_path"]
    assert not quiet_lines
    assert loud_lines


def test_yeared_name_attaches_to_bare_series_when_year_matches(app):
    """Test that a yeared name attaches to the bare series record.

    Sonarr now names new files "Title (Year)". The yeared form must go
    to the existing bare-titled record when the year matches its
    first-air year. It must not split into a second series."""

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

        # A different year is a different show. It keeps its own name.
        wrong_year = evaluate_filename(
            "Top Gear (1978) - S01E01 - [DVD].mkv", log=False
        )
        assert wrong_year["title"] == "Top Gear (1978)"


def test_bare_name_attaches_to_unique_year_suffixed_series(app):
    """Test that a bare name attaches to the only year-suffixed series.

    This is the inverse direction after a series rename. A stray
    bare-named file goes to the year-suffixed record, but only when
    exactly 1 candidate exists."""

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
    """Test that the import sweeps skip hidden and transient names.

    The import sweeps enqueue only finished files (#244). Hidden names
    and the intermediate files of transfer tools stay invisible. The
    rename to the real name starts the import."""

    from app import importable_basename

    assert importable_basename("Jaws (1975) - [Bluray-1080p].mkv")
    assert not importable_basename(".Jaws (1975) - [Bluray-1080p].mkv")
    assert not importable_basename("Jaws (1975) - [Bluray-1080p].mkv.staged")
    assert not importable_basename("Jaws (1975) - [Bluray-1080p].mkv.partial")
    assert not importable_basename("Jaws (1975) - [Bluray-1080p].mkv.part")
    assert not importable_basename("Jaws (1975) - [Bluray-1080p].mkv.tmp")
    assert not importable_basename("Jaws (1975) - [Bluray-1080p].mkv.filepart")
    assert not importable_basename("Jaws (1975) - [Bluray-1080p].mkv.crdownload")
