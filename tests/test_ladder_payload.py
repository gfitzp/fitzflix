"""Test the one builder of the star-row state, ladder_payload (#335).

4 surfaces answer the state of a star row: the live ladder post of the
movie page, the batched hydration of /movie_states, the history row
edit and the Criterion 24/7 card. Before the builder, each one wrote
its own dict, and the row edit lacked 2 keys. Each answer now carries
the same shared keys."""

import re
from datetime import datetime

from tests.conftest import ADMIN_EMAIL
from tests.factories import make_movie

LADDER = {"X-Requested-With": "ladder"}


def _csrf(page):
    return re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page).group(1)


def test_ladder_payload_reads_the_row():
    from app.main.helpers import LADDER_KEYS, ladder_payload

    class Row:
        rating = 3

    empty = ladder_payload(None, False, None, False)
    assert set(empty) == LADDER_KEYS
    assert empty == {
        "rating": None,
        "has_review": False,
        "flagged": False,
        "estimated": None,
        "on_watchlist": False,
    }
    rated = ladder_payload(Row(), True, 4.5, True, fold_new="x")
    assert rated["rating"] == 3.0
    assert rated["has_review"] is True
    assert rated["flagged"] is True
    assert rated["estimated"] == 4.5
    assert rated["on_watchlist"] is True
    assert rated["fold_new"] == "x"
    assert set(rated) == LADDER_KEYS | {"fold_new"}


def test_the_four_ladder_responses_share_their_keys(app, admin_client):
    from app import db
    from app.criterion_now import _ladder_state_for
    from app.main.helpers import LADDER_KEYS
    from app.models import User, UserMovieReview
    from app.videos import star_rating_fields

    with app.app_context():
        user = User.query.filter_by(email=ADMIN_EMAIL).first()
        movie = make_movie("Ladder Keys", 1971, tmdb_id=880301)
        row = UserMovieReview(
            user_id=user.id,
            movie_id=movie.id,
            review="",
            date_watched=datetime(2026, 8, 9, 17, 49, 14),
            rewatch=False,
            **star_rating_fields(3),
        )
        db.session.add(row)
        db.session.commit()
        movie_id, row_id, user_id = movie.id, row.id, user.id

    # 1. The live ladder post of the movie page

    token = _csrf(admin_client.get(f"/movie/{movie_id}").get_data(as_text=True))
    live = admin_client.post(
        f"/movie/{movie_id}",
        data={"csrf_token": token, "quick_rating": "4"},
        headers=LADDER,
    ).get_json()

    # 2. The batched hydration, with its 2 fold keys

    states = admin_client.get(
        f"/movie_states?movie_ids={movie_id}&tmdb_ids=880301,999999"
    ).get_json()
    hydrated = states["movies"][str(movie_id)]
    hydrated_tmdb = states["tmdb"]["880301"]
    hydrated_unknown = states["tmdb"]["999999"]

    # 3. The history row edit

    token = _csrf(admin_client.get(f"/review/{row_id}/edit").get_data(as_text=True))
    edited = admin_client.post(
        f"/review/{row_id}/edit",
        data={"csrf_token": token, "quick_rating": "2"},
        headers=LADDER,
    ).get_json()

    # 4. The Criterion 24/7 card, with movie_id and upgradable

    with app.app_context():
        user = db.session.get(User, user_id)
        card = _ladder_state_for(user, 880301, {})
        card_unknown = _ladder_state_for(user, None, {})

    assert set(live) == LADDER_KEYS
    assert set(edited) == LADDER_KEYS
    folds = LADDER_KEYS | {"fold_new", "fold_leaving"}
    assert set(hydrated) == set(hydrated_tmdb) == set(hydrated_unknown) == folds
    extras = LADDER_KEYS | {"movie_id", "upgradable"}
    assert set(card) == set(card_unknown) == extras

    assert live["rating"] == 4.0 and live["has_review"] is True
    assert edited["rating"] == 2.0 and edited["has_review"] is True
    assert card["movie_id"] == movie_id and card["has_review"] is True
    assert card_unknown["movie_id"] is None and card_unknown["has_review"] is False


def test_a_ladder_fetch_on_a_guid_row_gets_json(app, admin_client):
    """A stale tab's ladder post on a Letterboxd row gets JSON (#512).

    Before, it got the redirect and the flash of a normal submission."""

    from app import db
    from app.models import User, UserMovieReview

    with app.app_context():
        user = User.query.filter_by(email=ADMIN_EMAIL).first()
        movie = make_movie("Guid Row", 1972)
        row = UserMovieReview(
            user_id=user.id,
            movie_id=movie.id,
            review="",
            date_watched=datetime(2026, 8, 9),
            rewatch=False,
            letterboxd_guid="letterboxd-review-1",
        )
        db.session.add(row)
        db.session.commit()
        movie_id, row_id = movie.id, row.id

    token = _csrf(admin_client.get(f"/movie/{movie_id}").get_data(as_text=True))
    response = admin_client.post(
        f"/review/{row_id}/edit",
        data={"csrf_token": token, "quick_rating": "2"},
        headers=LADDER,
    )
    assert response.status_code == 409
    assert response.get_json() == {"error": "syncs from Letterboxd"}
