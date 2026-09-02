"""Test the cross-task lock contract.

The localization task builds its lock identifier from the parsed filename
details. The later tasks build their lock identifiers from database rows
through File.file_identifier(). Both must serialize to the same string.
If they do not, two tasks that work on the same title do not contend for
the same lock.
"""

import json

from app.videos import evaluate_filename

from tests.factories import make_movie, make_movie_file, make_tv_file, make_tv_series


def localization_identifier(details):
    """Return the identifier dict that localization_task builds.

    The task builds it from the evaluate_filename details."""

    if details.get("media_library") == "Movies":
        return json.dumps(
            {
                "title": details.get("title"),
                "year": details.get("year"),
                "feature_type": details.get("feature_type_name"),
                "plex_title": details.get("plex_title"),
                "edition": details.get("edition"),
            }
        )
    return json.dumps(
        {
            "title": details.get("title"),
            "season": details.get("season"),
            "episode": details.get("episode"),
        }
    )


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
