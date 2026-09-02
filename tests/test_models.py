"""Test the ranking builders: the row_number windows that select the best file."""

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
        assert dvd in bluray.find_worse_files()

        # The worse release can never prune the better release.
        assert bluray not in dvd.find_worse_files()

        # A better release blocks an incoming file of the same episode.
        # The title that each disc gave the extra is not important.
        blockers = File(
            media_library="TV Shows",
            dirname=dvd.dirname,
            title="Doctor Who (1963)",
            season=0,
            episode=85007,
            last_episode=85007,
            edition="Yet Another Title",
            quality_title="DVD",
            fullscreen=False,
        ).find_better_files()
        assert bluray in blockers


def test_movie_replacement_still_respects_editions(app):
    """Keep the edition as part of the identity of a movie.

    Different cuts of the same film coexist. They never prune each other."""

    with app.app_context():
        movie = make_movie("Blade Runner", 1982)
        theatrical = make_movie_file(movie, "Bluray-1080p")
        final_cut = make_movie_file(movie, "DVD", edition="Final Cut")
        assert final_cut not in theatrical.find_worse_files()
        assert theatrical not in final_cut.find_worse_files()
