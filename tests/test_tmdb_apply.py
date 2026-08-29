"""TMDB apply methods: payload fields must land on mapped columns.

Regression home for the tvdb_id persist bug where tmdb_tv_apply wrote the
TheTVDB cross-reference to an unmapped attribute and it never persisted,
and for the #251 empty-payload guard: a glitched TMDB payload with no
genres or keywords must not wipe the rows a record already has.
"""

from app import db
from app.models import Movie, TMDBGenre, TMDBKeyword, TVSeries

from tests.factories import make_movie, make_tv_series


def _movie_with_associations(title, year):
    movie = make_movie(title, year, tmdb_id=990001)
    movie.genres.append(TMDBGenre(id=35, name="Comedy"))
    movie.keywords.append(TMDBKeyword(id=4344, name="musical"))
    db.session.commit()
    return movie


def test_movie_apply_empty_genre_list_keeps_stored_rows(app):
    """The Aug 7-13 2026 TMDB glitch served details payloads with the
    genre list empty; the wipe-then-rewrite apply then erased 943
    films' genres permanently (#251). An empty incoming list keeps
    what's stored; a populated one still replaces it."""

    with app.app_context():
        movie = _movie_with_associations("Glitch Victim", 1983)
        movie.tmdb_movie_apply(
            {"id": 990001, "genres": [], "keywords": {"keywords": []}}
        )
        db.session.commit()

        stored = db.session.get(Movie, movie.id)
        assert [g.name for g in stored.genres] == ["Comedy"]
        assert [k.name for k in stored.keywords] == ["musical"]


def test_movie_apply_populated_lists_still_replace(app):
    with app.app_context():
        movie = _movie_with_associations("Full Payload", 1984)
        movie.tmdb_movie_apply(
            {
                "id": 990001,
                "genres": [{"id": 18, "name": "Drama"}],
                "keywords": {"keywords": [{"id": 9714, "name": "remake"}]},
            }
        )
        db.session.commit()

        stored = db.session.get(Movie, movie.id)
        assert [g.name for g in stored.genres] == ["Drama"]
        assert [k.name for k in stored.keywords] == ["remake"]


def test_tv_apply_empty_genre_list_keeps_stored_rows(app):
    """Same guard on the TV side (#251) — TV keywords ride in
    "results", not "keywords"."""

    with app.app_context():
        series = make_tv_series("Glitched Series")
        series.genres.append(TMDBGenre(id=10765, name="Sci-Fi & Fantasy"))
        series.keywords.append(TMDBKeyword(id=310, name="time travel"))
        db.session.commit()

        series.tmdb_tv_apply({"id": 990002, "genres": [], "keywords": {"results": []}})
        db.session.commit()

        stored = db.session.get(TVSeries, series.id)
        assert [g.name for g in stored.genres] == ["Sci-Fi & Fantasy"]
        assert [k.name for k in stored.keywords] == ["time travel"]


def test_tv_apply_persists_external_ids(app):
    with app.app_context():
        series = make_tv_series("Doctor Who")
        series.tmdb_tv_apply(
            {
                "id": 57243,
                "name": "Doctor Who",
                "external_ids": {"imdb_id": "tt0436992", "tvdb_id": 78804},
            }
        )
        db.session.commit()
        db.session.expire_all()

        stored = db.session.get(TVSeries, series.id)
        assert stored.tvdb_id == 78804
        assert stored.imdb_id == "tt0436992"
        assert stored.tmdb_id == 57243
