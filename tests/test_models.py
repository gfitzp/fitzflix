"""Test the ranking builders: the row_number windows that select the best file."""

import os

from app import db
from app.file_ranking import find_better_files, find_worse_files
from app.models import File, Movie, RefQuality, TVSeries, movie_file_rank, tv_file_rank

from tests.factories import make_movie, make_movie_file, make_tv_file, make_tv_series


def ranked_movie_files(session):
    ranked = (
        session.query(File.id, movie_file_rank())
        .join(Movie, Movie.id == File.movie_id)
        .join(RefQuality, RefQuality.id == File.quality_id)
        .subquery()
    )
    return {row.id: row.rank for row in session.query(ranked.c.id, ranked.c.rank).all()}


def test_best_quality_ranks_first(app):
    with app.app_context():
        movie = make_movie("Jaws", 1975)
        dvd = make_movie_file(movie, "DVD")
        bluray = make_movie_file(movie, "Bluray-1080p")
        ranks = ranked_movie_files(db.session)
        assert ranks[bluray.id] == 1
        assert ranks[dvd.id] == 2


def test_fullscreen_ranks_below_widescreen(app):
    with app.app_context():
        movie = make_movie("The Terminator", 1984)
        widescreen_dvd = make_movie_file(movie, "DVD")
        fullscreen_bluray = make_movie_file(movie, "Bluray-1080p", fullscreen=True)
        ranks = ranked_movie_files(db.session)
        # A widescreen DVD outranks a fullscreen file, even one of better quality.
        assert ranks[widescreen_dvd.id] == 1
        assert ranks[fullscreen_bluray.id] == 2


def test_special_features_rank_in_their_own_groups(app):
    with app.app_context():
        movie = make_movie("Jaws", 1975)
        main = make_movie_file(movie, "DVD")
        trailer = make_movie_file(
            movie, "DVD", feature_type_name="Trailers", plex_title="Theatrical Trailer"
        )
        interview = make_movie_file(
            movie, "DVD", feature_type_name="Interviews", plex_title="Spielberg"
        )
        ranks = ranked_movie_files(db.session)
        # Different features do not compete with the main film. They do not
        # compete with each other.
        assert ranks[main.id] == ranks[trailer.id] == ranks[interview.id] == 1


def test_editions_rank_in_their_own_groups(app):
    with app.app_context():
        movie = make_movie("Blade Runner", 1982)
        theatrical = make_movie_file(movie, "DVD")
        final_cut = make_movie_file(
            movie,
            "DVD",
            edition="Final Cut",
            plex_title="Blade Runner (1982) {edition-Final Cut}",
        )
        ranks = ranked_movie_files(db.session)
        assert ranks[theatrical.id] == ranks[final_cut.id] == 1


def ranked_tv_files(session):
    ranked = (
        session.query(File.id, tv_file_rank())
        .join(TVSeries, TVSeries.id == File.series_id)
        .join(RefQuality, RefQuality.id == File.quality_id)
        .subquery()
    )
    return {row.id: row.rank for row in session.query(ranked.c.id, ranked.c.rank).all()}


def test_tv_best_quality_ranks_first(app):
    with app.app_context():
        series = make_tv_series("Doctor Who (2005)")
        dvd = make_tv_file(series, 1, 1, "DVD")
        bluray = make_tv_file(series, 1, 1, "Bluray-1080p")
        other_episode = make_tv_file(series, 1, 2, "DVD")
        ranks = ranked_tv_files(db.session)
        assert ranks[bluray.id] == 1
        assert ranks[dvd.id] == 2
        assert ranks[other_episode.id] == 1


def test_tv_multi_episode_file_wins_quality_ties(app):
    with app.app_context():
        series = make_tv_series("Planet Earth")
        single = make_tv_file(series, 1, 5, "Bluray-1080p")
        double = make_tv_file(series, 1, 5, "Bluray-1080p", last_episode=6)
        ranks = ranked_tv_files(db.session)
        assert ranks[double.id] == 1
        assert ranks[single.id] == 2


def test_a_wider_span_replaces_a_narrower_file_only_at_the_same_quality(app):
    """A DVD of episodes 1-6 must not delete a Bluray of episode 1."""

    with app.app_context():
        series = make_tv_series("Doctor Who (1975)")
        bluray_single = make_tv_file(series, 13, 1, "Bluray-1080p")
        dvd_single = make_tv_file(series, 13, 1, "SDTV")
        dvd_span = make_tv_file(series, 13, 1, "DVD", last_episode=6)

        worse = find_worse_files(dvd_span)
        assert dvd_single in worse
        assert bluray_single not in worse


def test_tv_replacement_ignores_the_edition_title(app):
    """Identify a TV episode by series, season, and episode span, not by edition.

    For a TV file, the edition holds the episode-title segment of the
    filename. Two releases can give the same episode different titles.
    Example from Glenn, the Seeds of Doom case: the Blu-ray special gave
    the extra a different name than the DVD did. Thus, neither replacement
    query saw the two files as the same episode."""

    with app.app_context():
        series = make_tv_series("Doctor Who (1963)")
        dvd = make_tv_file(
            series, 0, 85007, "DVD", edition="The Seeds of Doom - Photo Gallery"
        )
        bluray = make_tv_file(
            series,
            0,
            85007,
            "Bluray-480p",
            edition="The Seeds of Doom - Graeme Harper Featurette",
        )

        # The better release with the new title prunes the older release.
        assert dvd in find_worse_files(bluray)

        # The worse release can never prune the better release.
        assert bluray not in find_worse_files(dvd)

        # A better release blocks an incoming file of the same episode.
        # The title that each disc gave the extra is not important.
        blockers = find_better_files(
            {
                "media_library": "TV Shows",
                "dirname": dvd.dirname,
                "title": "Doctor Who (1963)",
                "season": 0,
                "episode": 85007,
                "last_episode": 85007,
                "edition": "Yet Another Title",
                "quality_title": "DVD",
                "fullscreen": False,
            }
        )
        assert bluray in blockers


def test_movie_replacement_still_respects_editions(app):
    """Keep the edition as part of the identity of a movie.

    Different cuts of the same film coexist. They never prune each other."""

    with app.app_context():
        movie = make_movie("Blade Runner", 1982)
        theatrical = make_movie_file(movie, "Bluray-1080p")
        final_cut = make_movie_file(movie, "DVD", edition="Final Cut")
        assert final_cut not in find_worse_files(theatrical)
        assert theatrical not in find_worse_files(final_cut)


def test_a_tie_goes_to_the_lowest_file_id(app):
    """2 copies of the same quality never share rank 1 (#273).

    The lowest file id wins, in the app and in the ranked_files view."""

    from tests.factories import make_file

    with app.app_context():
        movie = make_movie("Jaws", 1975)
        first = make_movie_file(movie, "Bluray-1080p")
        second = make_file(
            "Jaws (1975) - [Bluray-1080p].mp4",
            "Movies/Jaws (1975)",
            "Jaws (1975)",
            "Movies",
            "Bluray-1080p",
            movie_id=movie.id,
        )
        ranks = ranked_movie_files(db.session)
        assert (ranks[first.id], ranks[second.id]) == (1, 2)

        series = make_tv_series("Columbo")
        episode = make_tv_file(series, 1, 1, "Bluray-1080p")
        copy = make_file(
            "Columbo - S01E01 - [Bluray-1080p].mp4",
            "TV Shows/Columbo/Season 01",
            "Columbo - S01E01",
            "TV Shows",
            "Bluray-1080p",
            series_id=series.id,
            season=1,
            episode=1,
            last_episode=1,
        )
        tv_ranks = ranked_tv_files(db.session)
        assert (tv_ranks[episode.id], tv_ranks[copy.id]) == (1, 2)


def test_a_file_from_details_keeps_only_the_columns(app):
    """File.from_details drops the keys that belong to other rows (#367)."""

    with app.app_context():
        file = File.from_details(
            {
                "basename": "Kept Wide (2001) - [DVD].mkv",
                "media_library": "Movies",
                "title": "Kept Wide",
                "year": 2001,
                "quality_title": "DVD",
                "replace_existing": True,
            }
        )
    assert file.basename == "Kept Wide (2001) - [DVD].mkv"
    assert file.media_library == "Movies"
    assert not hasattr(file, "title")
    assert not hasattr(file, "replace_existing")


def test_a_widescreen_copy_blocks_a_full_screen_import(app):
    """A full screen file has the plex title and the edition of its widescreen copy."""

    from app.videos import evaluate_filename

    with app.app_context():
        movie = make_movie("Kept Wide", 2001)
        widescreen = make_movie_file(movie, "SDTV")
        db.session.commit()
        details = evaluate_filename(
            "Kept Wide (2001) - Full Screen [DVD].mkv", log=False
        )
        assert find_better_files(details) == [widescreen]


def test_display_title_prefers_the_tmdb_title_and_its_year(app):
    """Test the one display form of a film and a series.

    13 routes built this string inline before the property. The year
    follows the TMDB release date only when the TMDB title is known."""

    from datetime import datetime

    from app.models import Movie, TVSeries

    local = Movie(title="Local Title", year=1999)
    assert local.display_title == "Local Title (1999)"

    tmdb = Movie(
        title="Local Title",
        year=1999,
        tmdb_title="TMDB Title",
        tmdb_release_date=datetime(2000, 2, 1),
    )
    assert tmdb.display_title == "TMDB Title (2000)"

    undated = Movie(title="Local Title", year=1999, tmdb_title="TMDB Title")
    assert undated.display_title == "TMDB Title (1999)"

    assert TVSeries(title="Local Name").display_title == "Local Name"
    assert (
        TVSeries(title="Local Name", tmdb_name="TMDB Name").display_title == "TMDB Name"
    )


def test_no_many_to_many_relationship_carries_a_delete_cascade(app):
    """Test that no relationship with a secondary table cascades a delete.

    A delete cascade on a many-to-many removes the SHARED rows: the
    genre, the certification, the season (423b475, #305). The mapper
    registry is the one place that sees every relationship."""

    with app.app_context():
        offenders = []
        for mapper in db.Model.registry.mappers:
            for relationship in mapper.relationships:
                if relationship.secondary is None:
                    continue
                if relationship.cascade.delete or relationship.cascade.delete_orphan:
                    offenders.append(f"{mapper.class_.__name__}.{relationship.key}")
        assert offenders == []


def test_the_credit_rows_carry_no_delete_cascade(app):
    """Test that a delete of a credit cannot delete the cast and crew rows.

    The credit (a person) had 4 relationships to its join rows with a
    delete cascade. None was ever read. The join rows now point at the
    credit, and nothing points back. Thus, no delete cascade can reach
    the rows that the movie and series pages read."""

    from app.models import MovieCast, MovieCrew, TMDBCredit, TVCast, TVCrew

    with app.app_context():
        credit_mapper = db.inspect(TMDBCredit)
        assert list(credit_mapper.relationships) == []
        for cls, key in (
            (MovieCast, "starring"),
            (MovieCrew, "crewed"),
            (TVCast, "starring"),
            (TVCrew, "crewed"),
        ):
            relationship = db.inspect(cls).relationships[key]
            assert relationship.mapper.class_ is TMDBCredit
            assert not relationship.cascade.delete
            assert not relationship.cascade.delete_orphan


def test_a_movie_delete_removes_its_watchlist_rows(app):
    """Test the one reader of Movie.watchlist_entries: the delete cascade.

    No code reads the attribute. But the relationship gives UserWatchlist
    its movie backref (the availability digest reads it) and the junk
    delete of the CLI its cascade. Thus, it stays."""

    from app.models import Movie, User, UserWatchlist

    with app.app_context():
        movie = make_movie("Watchlisted Junk", 2001)
        user = User.query.first()
        db.session.add(UserWatchlist(user_id=user.id, movie_id=movie.id))
        db.session.commit()
        movie_id = movie.id
        assert UserWatchlist.query.filter_by(movie_id=movie_id).count() == 1

        db.session.delete(db.session.get(Movie, movie_id))
        db.session.commit()
        assert UserWatchlist.query.filter_by(movie_id=movie_id).count() == 0


def test_the_orphans_are_gone(app):
    """Pin the deletions of the second simplification pass."""

    import app.radarr_push as radarr_push
    import app.sonarr_push as sonarr_push
    import app.videos as videos
    from app.models import Movie

    assert not hasattr(radarr_push, "_radarr_rename_to_quality")
    assert not hasattr(sonarr_push, "_sonarr_rename_to_quality")
    for name in ("_normalize_title", "_pick_tmdb_match", "_extract_media_details"):
        assert name not in videos.__all__
    assert not hasattr(Movie, "sanitize_string")
    assert "Utilities" not in dir(__import__("app.models", fromlist=["x"]))


def test_the_library_names_are_constants(app):
    """Test MOVIES and TV_SHOWS, the 2 values of File.media_library."""

    import pathlib
    import re

    from app.models import MOVIES, TV_SHOWS

    assert os.path.basename(app.config["MOVIE_LIBRARY"]) == MOVIES
    assert os.path.basename(app.config["TV_LIBRARY"]) == TV_SHOWS
    literal = re.compile(r'[=!]= "(Movies|TV Shows)"')
    offenders = [
        str(path)
        for path in pathlib.Path("app").rglob("*.py")
        if literal.search(path.read_text())
    ]
    assert offenders == []


def test_a_file_from_details_refuses_an_unknown_key(app):
    """A misspelled column key must raise, not catalog a blank column."""

    import pytest

    with app.app_context():
        with pytest.raises(TypeError, match="converted_from_qualty"):
            File.from_details(
                {
                    "basename": "Typo (2001) - [DVD].mkv",
                    "converted_from_qualty": "Bluray-2160p",
                }
            )
