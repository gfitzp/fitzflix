"""Test that the TMDB apply methods put the payload fields into mapped columns.

This file holds the regression tests for 2 bugs. In the tvdb_id persist
bug, tmdb_tv_apply wrote the TheTVDB cross-reference to an unmapped
attribute, and the value never persisted. The #251 empty-payload guard
covers a glitched TMDB payload with no genres or keywords. Such a
payload must not delete the rows that a record already has.
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
    """Test that an empty genre list keeps the stored rows.

    From 2026-08-07 to 2026-08-13, a TMDB glitch served details payloads
    with an empty genre list. The delete-then-rewrite apply then erased
    the genres of 943 films permanently (#251). An empty incoming list
    keeps the stored rows. A populated list still replaces them."""

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
    """Test the same guard on the TV side (#251).

    TV keywords come in "results", not in "keywords"."""

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


def test_tv_apply_stores_the_us_content_rating(app):
    """Test that the TV apply stores the US content rating.

    The appended content_ratings block puts its US rating on the series.
    A payload with no US entry keeps the stored rating, like the
    empty-list guards (#251)."""

    with app.app_context():
        series = make_tv_series("Rated Series")
        series.tmdb_tv_apply(
            {
                "id": 990005,
                "content_ratings": {
                    "results": [
                        {"iso_3166_1": "DE", "rating": "12"},
                        {"iso_3166_1": "US", "rating": "TV-MA"},
                    ]
                },
            }
        )
        db.session.commit()
        assert db.session.get(TVSeries, series.id).tmdb_content_rating == "TV-MA"

        series.tmdb_tv_apply({"id": 990005, "content_ratings": {"results": []}})
        db.session.commit()
        assert db.session.get(TVSeries, series.id).tmdb_content_rating == "TV-MA"


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


def test_movie_apply_empty_credits_keeps_stored_cast(app):
    """Test that empty credits keep the stored cast (#252).

    The bulk delete of the cast and crew ran before any payload check
    in the past. Thus, a glitched payload with an empty credits section
    deleted the cast of a film permanently. Empty incoming lists keep
    the stored rows. Populated lists still replace them."""

    from app.models import MovieCast, TMDBCredit

    with app.app_context():
        movie = make_movie("Cast Victim", 1957, tmdb_id=990003)
        credit = TMDBCredit(id=7001, name="Lead Actor")
        db.session.add(credit)
        db.session.add(MovieCast(movie_id=movie.id, credit_id=7001, character="Hero"))
        db.session.commit()

        movie.tmdb_movie_apply({"id": 990003, "credits": {"cast": [], "crew": []}})
        db.session.commit()
        assert MovieCast.query.filter_by(movie_id=movie.id).count() == 1

        movie.tmdb_movie_apply(
            {
                "id": 990003,
                "credits": {
                    "cast": [{"id": 7002, "name": "New Lead", "character": "Villain"}],
                    "crew": [],
                },
            }
        )
        db.session.commit()
        rows = MovieCast.query.filter_by(movie_id=movie.id).all()
        assert [r.credit_id for r in rows] == [7002]


def test_movie_apply_absent_scalar_keys_keep_stored_values(app):
    """Test that absent scalar keys keep the stored values (#252).

    A partial payload (the key is missing) must not null a populated
    column. A key that is present with null still clears the column,
    because that is TMDB that removes real data."""

    with app.app_context():
        movie = make_movie("Scalar Victim", 1960, tmdb_id=990004)
        movie.tmdb_overview = "A stored overview."
        movie.tmdb_runtime = 96
        movie.tmdb_poster_path = "/stored.jpg"
        db.session.commit()

        movie.tmdb_movie_apply({"id": 990004, "poster_path": None})
        db.session.commit()

        assert movie.tmdb_overview == "A stored overview."
        assert movie.tmdb_runtime == 96
        assert movie.tmdb_poster_path is None


def test_tv_apply_empty_aggregate_cast_keeps_stored_rows(app):
    """Test the #252 guard on the TV side.

    An aggregate_credits section that is present with empty cast and
    crew lists keeps the stored rows."""

    from app.models import TMDBCredit, TVCast

    with app.app_context():
        series = make_tv_series("Cast Series")
        db.session.add(TMDBCredit(id=7003, name="Series Lead"))
        db.session.add(TVCast(tv_id=series.id, credit_id=7003, character="Captain"))
        db.session.commit()

        series.tmdb_tv_apply(
            {"id": 990005, "aggregate_credits": {"cast": [], "crew": []}}
        )
        db.session.commit()
        assert TVCast.query.filter_by(tv_id=series.id).count() == 1


def _count_queries(work):
    """Run work and return the number of SQL statements that it sent."""

    from sqlalchemy import event

    statements = []

    def record(conn, cursor, statement, *args):
        statements.append(statement)

    engine = db.engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        work()
    finally:
        event.remove(engine, "before_cursor_execute", record)
    return len(statements)


def test_the_clear_out_does_not_scan_unrelated_rows(app):
    """The clear-out goes through the links of the film only (#295).

    The old clear-out sent 1 query for each keyword of the whole library.
    Thus, the query count of an apply must not grow with unrelated rows."""

    from app.models import TMDBProductionCompany

    payload = {
        "id": 990001,
        "genres": [{"id": 18, "name": "Drama"}],
        "keywords": {"keywords": [{"id": 9714, "name": "remake"}]},
    }
    with app.app_context():
        movie = _movie_with_associations("Few Rows", 1990)
        before = _count_queries(lambda: movie.tmdb_movie_apply(payload))
        db.session.commit()

        for n in range(300):
            db.session.add(TMDBKeyword(id=500000 + n, name=f"unrelated {n}"))
            db.session.add(TMDBProductionCompany(id=500000 + n, name=f"studio {n}"))
        db.session.commit()

        after = _count_queries(lambda: movie.tmdb_movie_apply(payload))
        db.session.commit()
        assert after <= before + 5

        stored = db.session.get(Movie, movie.id)
        assert [g.name for g in stored.genres] == ["Drama"]
        assert [k.name for k in stored.keywords] == ["remake"]


def test_a_namesake_with_another_tmdb_id_is_not_adopted(app):
    """A review or a watchlist row must never go to a different film."""

    from app.tmdb_refresh import find_or_create_tmdb_movie

    with app.app_context():
        namesake = make_movie("Same Name", 1999, tmdb_id=501)
        unmatched = make_movie("Unmatched Name", 1999)
        db.session.commit()

        assert find_or_create_tmdb_movie(502, "Same Name", 1999) == (None, False)
        assert namesake.tmdb_id == 501

        movie, created = find_or_create_tmdb_movie(503, "Unmatched Name", 1999)
        assert (movie, created) == (unmatched, False)
        assert movie.tmdb_id == 503


def test_a_movie_delete_keeps_the_shared_tmdb_rows(app):
    """A delete removes only the link rows of the film, never a shared genre."""

    with app.app_context():
        drama = TMDBGenre(id=18, name="Drama")
        doomed = make_movie("Doomed", 2001)
        kept = make_movie("Kept", 2002)
        series = make_tv_series("Kept Series")
        db.session.add(drama)
        doomed.genres.append(drama)
        kept.genres.append(drama)
        series.genres.append(drama)
        db.session.commit()
        kept_id, series_id = kept.id, series.id

        db.session.delete(doomed)
        db.session.commit()

        assert db.session.get(TMDBGenre, 18) is not None
        assert db.session.get(Movie, kept_id).genres.all() == [drama]
        assert db.session.get(TVSeries, series_id).genres.all() == [drama]


def test_a_merge_moves_the_watchlist_status_and_award_rows(app):
    """The survivor keeps the user rows of the merged record."""

    from app.models import (
        ArchivedOriginal,
        DVRChannel,
        MovieAward,
        User,
        UserMovieStatus,
        UserWatchlist,
    )
    from app.tmdb_refresh import move_movie_rows

    with app.app_context():
        user = User.query.first()
        old = make_movie("Old Record", 2003)
        new = make_movie("New Record", 2003, tmdb_id=900)
        channel = DVRChannel(number=310, name="Picks", slug="picks")
        channel.movies.append(old)
        db.session.add_all(
            [
                channel,
                ArchivedOriginal(
                    media_library="Movies",
                    movie_id=old.id,
                    quality_title="Bluray-2160p",
                    untouched_basename="Old Record (2003) - [Bluray-2160p].mkv",
                    aws_key="untouched/Old Record (2003) - [Bluray-2160p].mkv",
                    reason="archive only",
                ),
                UserWatchlist(user_id=user.id, movie_id=old.id),
                UserWatchlist(user_id=user.id, movie_id=new.id),
                UserMovieStatus(
                    user_id=user.id, movie_id=old.id, kind="not_interested"
                ),
                MovieAward(movie_id=old.id, award_id="oscar", win=True, year=2004),
            ]
        )
        db.session.commit()

        move_movie_rows(old.id, new.id)
        db.session.flush()

        assert UserWatchlist.query.filter_by(movie_id=new.id).count() == 1
        assert UserMovieStatus.query.filter_by(movie_id=new.id).count() == 1
        assert MovieAward.query.filter_by(movie_id=new.id).count() == 1
        db.session.delete(old)
        db.session.commit()
        assert UserWatchlist.query.filter_by(movie_id=new.id).count() == 1
        assert UserMovieStatus.query.filter_by(movie_id=new.id).count() == 1
        assert ArchivedOriginal.query.filter_by(movie_id=new.id).count() == 1
        assert channel.movies.all() == [new]


def test_a_refresh_that_tmdb_answers_with_nothing_says_so(app, monkeypatch):
    """The page must not report a match when TMDB sent no data."""

    from app.models import Movie
    from app.tmdb_refresh import TMDB_NO_MATCH, refresh_tmdb_info

    with app.app_context():
        movie = make_movie("No Such Film", 1999)
        db.session.commit()
        movie_id = movie.id

    monkeypatch.setattr(Movie, "tmdb_movie_fetch", lambda self, tmdb_id=None: None)
    assert refresh_tmdb_info("Movies", movie_id, 999999, apply_at_front=True) == (
        TMDB_NO_MATCH
    )
    with app.app_context():
        (apply_job,) = [
            job
            for job in app.sql_queue.jobs
            if job.func_name == "app.videos.apply_tmdb_refresh"
        ]
        assert apply_job.kwargs["tmdb_payload"] is None


def test_movie_apply_skips_the_blank_premiere_certification(app):
    """The premiere entry of a country usually has a blank certification (#303)."""

    from app.models import RefTMDBCertification

    with app.app_context():
        db.session.add(RefTMDBCertification(country="US", certification="PG-13"))
        db.session.add(RefTMDBCertification(country="US", certification="R"))
        db.session.add(RefTMDBCertification(country="GB", certification="15"))
        movie = make_movie("Premiere First", 2012, tmdb_id=990010)
        db.session.commit()
        movie.tmdb_movie_apply(
            {
                "id": 990010,
                "release_dates": {
                    "results": [
                        {
                            "iso_3166_1": "US",
                            "release_dates": [
                                {"type": 1, "certification": ""},
                                {"type": 4, "certification": "R"},
                                {"type": 3, "certification": "PG-13"},
                            ],
                        },
                        {
                            "iso_3166_1": "GB",
                            "release_dates": [
                                {"type": 1, "certification": ""},
                                {"type": 5, "certification": "15"},
                            ],
                        },
                        {
                            "iso_3166_1": "FR",
                            "release_dates": [{"type": 1, "certification": ""}],
                        },
                    ]
                },
            }
        )
        db.session.commit()

        stored = db.session.get(Movie, movie.id)
        assert sorted((c.country, c.certification) for c in stored.certifications) == [
            ("GB", "15"),
            ("US", "PG-13"),
        ]


def test_tv_seasons_are_shared_and_pruned(app):
    """2 records with 1 TMDB id share season rows. A delete keeps them (#305)."""

    from app.models import TMDBSeason

    payload = {
        "id": 990020,
        "seasons": [
            {"id": 77001, "season_number": 1, "name": "Season 1"},
            {"id": 77002, "season_number": 2, "name": "Season 2"},
        ],
    }
    with app.app_context():
        first = make_tv_series("Shared Seasons", tmdb_id=990020)
        second = make_tv_series("Shared Seasons Copy", tmdb_id=990020)
        db.session.commit()
        first.tmdb_tv_apply(payload)
        second.tmdb_tv_apply(payload)
        db.session.commit()

        # Deleting 1 record keeps the rows that the other links to.
        db.session.delete(second)
        db.session.commit()
        assert sorted(s.id for s in first.seasons) == [77001, 77002]

        # TMDB renumbers season 2. Its old row has no link and goes.
        first.tmdb_tv_apply(
            {
                "id": 990020,
                "seasons": [
                    {"id": 77001, "season_number": 1, "name": "Season 1"},
                    {"id": 77003, "season_number": 2, "name": "Season 2"},
                ],
            }
        )
        db.session.commit()
        assert sorted(s.id for s in TMDBSeason.query.all()) == [77001, 77003]


def test_the_certification_sync_adds_the_missing_values(app, monkeypatch):
    """The 1-time seed had no US "NR". An NR film showed no rating (#303)."""

    import app.tmdb_refresh as tmdb_refresh
    from app.models import RefTMDBCertification

    class Answer:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "certifications": {
                    "US": [
                        {"certification": "NR", "meaning": "Not rated", "order": 0},
                        {"certification": "R", "meaning": "Restricted", "order": 4},
                    ],
                    "GB": [{"certification": "12A", "meaning": "", "order": 3}],
                }
            }

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(tmdb_refresh, "tmdb_get", lambda url, **kwargs: Answer())
    with app.app_context():
        db.session.add(RefTMDBCertification(country="US", certification="R"))
        movie = make_movie("Unrated Classic", 1951, tmdb_id=990030)
        db.session.commit()

        assert tmdb_refresh.sync_certification_list() is True
        assert tmdb_refresh.sync_certification_list() is True
        rows = {
            (c.country, c.certification): c.order
            for c in RefTMDBCertification.query.all()
        }
        assert rows == {("US", "NR"): 0, ("US", "R"): 4, ("GB", "12A"): 3}

        movie.tmdb_movie_apply(
            {
                "id": 990030,
                "release_dates": {
                    "results": [
                        {
                            "iso_3166_1": "US",
                            "release_dates": [
                                {"type": 1, "certification": ""},
                                {"type": 3, "certification": "NR"},
                            ],
                        }
                    ]
                },
            }
        )
        db.session.commit()
        assert [c.certification for c in movie.certifications] == ["NR"]
