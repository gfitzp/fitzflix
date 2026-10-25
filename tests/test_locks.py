"""Test the cross-task lock contract.

The localization task builds its lock identifier from the parsed filename
details (lock_key_for_details). The later tasks build their lock
identifiers from database rows through File.file_identifier(). Both go
through title_lock_key and must serialize to the same string. If they
do not, two tasks that work on the same title do not contend for the
same lock.
"""

import json

import pytest

from app.videos import evaluate_filename

from tests.factories import make_movie, make_movie_file, make_tv_file, make_tv_series


def localization_identifier(details):
    """Return the lock key that localization_task builds from the details."""

    from app.importing import lock_key_for_details

    return lock_key_for_details(details)


def test_title_lock_key_shapes():
    from app.models import title_lock_key

    movie = json.loads(title_lock_key("Movies", "Jaws", year=1975))
    assert list(movie) == ["title", "year", "feature_type", "plex_title", "edition"]
    assert movie["year"] == 1975 and movie["edition"] is None
    tv = json.loads(title_lock_key("TV Shows", "Doctor Who", season=1, episode=1))
    assert tv == {"title": "Doctor Who", "season": 1, "episode": 1}
    with pytest.raises(ValueError):
        title_lock_key("Music", "x")


def test_movie_identifiers_match(app):
    with app.app_context():
        movie = make_movie("Jaws", 1975)
        file = make_movie_file(movie, "DVD")

        details = evaluate_filename("Jaws (1975) - [DVD].mkv", log=False)
        assert localization_identifier(details) == file.file_identifier()


def test_movie_special_feature_identifiers_match(app):
    with app.app_context():
        movie = make_movie("Jaws", 1975)
        file = make_movie_file(
            movie,
            "DVD",
            feature_type_name="Trailers",
            plex_title="Theatrical Trailer",
        )

        details = evaluate_filename(
            "Jaws (1975) - Trailers - Theatrical Trailer [DVD].mkv", log=False
        )
        assert localization_identifier(details) == file.file_identifier()


def test_movie_edition_identifiers_match(app):
    with app.app_context():
        movie = make_movie("Blade Runner", 1982)
        file = make_movie_file(
            movie,
            "DVD",
            edition="Final Cut",
            plex_title="Blade Runner (1982) {edition-Final Cut}",
        )

        details = evaluate_filename(
            "Blade Runner (1982) {edition-Final Cut} - [DVD].mkv", log=False
        )
        assert localization_identifier(details) == file.file_identifier()


def test_tv_identifiers_match(app):
    with app.app_context():
        series = make_tv_series("Doctor Who (2005)")
        file = make_tv_file(series, 1, 1, "DVD")

        details = evaluate_filename("Doctor Who (2005) - S01E01 - [DVD].mkv", log=False)
        assert localization_identifier(details) == file.file_identifier()


def test_different_editions_do_not_share_a_lock(app):
    """Editions rank and process independently. Thus, they must not contend."""

    with app.app_context():
        plain = evaluate_filename("Blade Runner (1982) - [DVD].mkv", log=False)
        final_cut = evaluate_filename(
            "Blade Runner (1982) {edition-Final Cut} - [DVD].mkv", log=False
        )
        assert localization_identifier(plain) != localization_identifier(final_cut)
