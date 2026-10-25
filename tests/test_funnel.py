"""Test app/funnel.py: the film sets of a user and the one marker rule.

Six routes built the sets by hand, and each decided the "Might
interest you" marker on its own. The route tests in test_filmography,
test_criterion, test_search, test_pages and test_frozen_shelves cover
the pages. These tests cover the helper itself."""

import pytest

from app import db
from app.models import User, UserMovieStatus, UserWatchlist
from tests.factories import make_movie, make_movie_file
from tests.test_recommendations import log_watch


def test_the_two_set_builders_agree(app, admin_id):
    """The TMDB-id sets and the Movie-id sets name the same films."""

    from app.funnel import user_film_sets, user_film_sets_by_id

    with app.app_context():
        owned = make_movie("Owned Film", 2001, tmdb_id=9001)
        make_movie_file(owned, "DVD")
        extra = make_movie("Extra Only", 2002, tmdb_id=9002)
        make_movie_file(extra, "DVD", feature_type_name="Trailers")
        logged = make_movie("Logged Film", 2003, tmdb_id=9003)
        log_watch(admin_id, logged, rating=4)
        listed = make_movie("Listed Film", 2004, tmdb_id=9004)
        db.session.add(UserWatchlist(user_id=admin_id, movie_id=listed.id))
        refused = make_movie("Refused Film", 2005, tmdb_id=9005)
        db.session.add(
            UserMovieStatus(
                user_id=admin_id, movie_id=refused.id, kind="not_interested"
            )
        )
        unseen = make_movie("Unseen Film", 2006, tmdb_id=9006)
        db.session.add(
            UserMovieStatus(user_id=admin_id, movie_id=unseen.id, kind="unseen")
        )
        db.session.commit()
        films = [owned, extra, logged, listed, refused, unseen]
        user = db.session.get(User, admin_id)

        by_tmdb = user_film_sets(user, [film.tmdb_id for film in films])
        by_id = user_film_sets_by_id(admin_id, [film.id for film in films])

        assert by_tmdb.owned == {9001}
        assert by_tmdb.logged == {9003}
        assert by_tmdb.watchlisted == {9004}
        assert by_tmdb.refused == {9005}
        ids = {film.tmdb_id: film.id for film in films}
        for name in ("owned", "logged", "watchlisted", "refused"):
            assert getattr(by_id, name) == {
                ids[tmdb_id] for tmdb_id in getattr(by_tmdb, name)
            }

        # Another user has nothing in the funnel

        member_id = User.query.filter_by(admin=False).first().id
        assert user_film_sets_by_id(member_id, [film.id for film in films]) == (
            {owned.id},
            set(),
            set(),
            set(),
        )
        assert user_film_sets(user, []) == (set(), set(), set(), set())
        assert user_film_sets_by_id(admin_id, []) == (set(), set(), set(), set())


@pytest.mark.parametrize(
    "seen, refused, recommended, score, bar, expected",
    [
        # A seen film never gets the marker, not even in the ranking
        (True, False, True, 9.0, 1.0, False),
        # A refused film never gets it
        (False, True, True, 9.0, 1.0, False),
        # A film in the stored ranking gets it
        (False, False, True, None, None, True),
        # A scored film above the bar gets it
        (False, False, False, 2.0, 1.0, True),
        # At the bar is not above it
        (False, False, False, 1.0, 1.0, False),
        # No profile (no bar) and no ranking: no marker
        (False, False, False, 2.0, None, False),
        (False, False, False, None, 1.0, False),
    ],
)
def test_might_interest(seen, refused, recommended, score, bar, expected):
    from app.funnel import might_interest

    assert might_interest(seen, refused, recommended, score, bar) is expected
