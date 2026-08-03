"""Ranking builders: the row_number windows that pick each title's best file."""

from app import db
from app.models import File, Movie, RefQuality, TVSeries, movie_file_rank, tv_file_rank

from tests.factories import make_movie, make_movie_file, make_tv_file, make_tv_series


def ranked_movie_files(session):
    ranked = (
        session.query(File.id, movie_file_rank())
        .join(Movie, Movie.id == File.movie_id)
        .join(RefQuality, RefQuality.id == File.quality_id)
        .subquery()
    )
    return {
        row.id: row.rank for row in session.query(ranked.c.id, ranked.c.rank).all()
    }


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
        # A widescreen DVD beats even a better-quality fullscreen file
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
        # Different features never compete with the main film or each other
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
    return {
        row.id: row.rank for row in session.query(ranked.c.id, ranked.c.rank).all()
    }


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
