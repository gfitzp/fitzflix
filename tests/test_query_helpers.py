"""Test the shared query pieces of app/models.py: title_sort and the ranked files."""

from app import db


def test_title_sort_skips_the_article_and_prefers_the_tmdb_title(app):
    from app.models import Movie, title_sort
    from tests.factories import make_movie

    with app.app_context():
        make_movie("The Zebra", 2001)
        make_movie("Apple", 2002)
        make_movie("A Mango", 2003, tmdb_title="Aardvark")
        db.session.commit()
        by_tmdb = [m.year for m in Movie.query.order_by(title_sort(Movie).asc())]
        by_title = [
            m.year for m in Movie.query.order_by(title_sort(Movie, tmdb=False).asc())
        ]
    assert by_tmdb == [2003, 2002, 2001]  # Aardvark, Apple, Zebra
    assert by_title == [2002, 2003, 2001]  # Apple, Mango, Zebra


def test_ranked_files_mark_the_best_copy_as_rank_1(app):
    from app.file_ranking import ranked_movie_files, ranked_tv_files
    from tests.factories import (
        make_movie,
        make_movie_file,
        make_tv_file,
        make_tv_series,
    )

    with app.app_context():
        movie = make_movie("Ranked", 1999)
        dvd = make_movie_file(movie, "DVD")
        bluray = make_movie_file(movie, "Bluray-1080p", plex_title="Ranked (1999)")
        series = make_tv_series("Ranked Show")
        episode = make_tv_file(series, 1, 1, "DVD")
        db.session.commit()
        ranked = ranked_movie_files()
        ranks = dict(db.session.query(ranked.c.id, ranked.c.rank).all())
        tv_ranked = ranked_tv_files()
        tv_ranks = dict(db.session.query(tv_ranked.c.id, tv_ranked.c.rank).all())
        assert ranks == {bluray.id: 1, dvd.id: 2}
        assert tv_ranks == {episode.id: 1}
