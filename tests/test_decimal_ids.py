"""Test the one id parser of the main blueprint, decimal_id (#307, #310).

Before this helper, 10 routes parsed a user id with isdigit() and then
int(). "²" passes isdigit(), and int() refuses it. Each route is now 1
call. The 2 suggestion-card posts of the movie page refuse an unknown
movie_id instead of acting on the film of the page."""

import pytest

from tests.factories import make_movie
from tests.conftest import csrf_token_from

LADDER = {"X-Requested-With": "ladder"}
CARD = {"X-Requested-With": "card"}


@pytest.mark.parametrize(
    "value, expected",
    [
        ("12", 12),
        (" 12 ", 12),
        (12, 12),
        ("", None),
        (None, None),
        ("²", None),
        ("-1", None),
        ("1.5", None),
        ("abc", None),
    ],
)
def test_decimal_id(value, expected):
    from app.main.helpers import decimal_id

    assert decimal_id(value) == expected


def test_decimal_ids_reads_a_csv_or_a_list():
    from app.main.helpers import decimal_ids

    assert decimal_ids("1, 2,²,,x,3") == [1, 2, 3]
    assert decimal_ids(["4", "²", "5"]) == [4, 5]
    assert decimal_ids(None) == []
    assert decimal_ids("") == []


def _movie(app, title, tmdb_id):
    from app import db

    with app.app_context():
        movie = make_movie(title, 1970, tmdb_id=tmdb_id)
        db.session.commit()
        return movie.id


def _review_count(app, movie_id):
    from app.models import UserMovieReview

    with app.app_context():
        return UserMovieReview.query.filter_by(movie_id=movie_id).count()


@pytest.mark.parametrize("movie_id", ["999999", "²"])
def test_a_quick_rating_for_an_unknown_film_refuses(app, admin_client, movie_id):
    """A suggestion-card tap with an unknown movie_id must not rate this page."""

    page_id = _movie(app, "Unknown Suggestion Page", 880201)
    token = csrf_token_from(
        admin_client.get(f"/movie/{page_id}").get_data(as_text=True)
    )
    data = {"csrf_token": token, "quick_rating": "4", "movie_id": movie_id}

    response = admin_client.post(f"/movie/{page_id}", data=data)
    assert response.status_code == 302
    assert _review_count(app, page_id) == 0
    with admin_client.session_transaction() as session:
        flashes = session.get("_flashes") or []
    assert any("not in the library" in message for _, message in flashes)

    response = admin_client.post(f"/movie/{page_id}", data=data, headers=LADDER)
    assert response.status_code == 404
    assert _review_count(app, page_id) == 0


def test_a_watchlist_bank_of_an_unknown_film_refuses(app, admin_client):
    """A bank with an unknown movie_id must not add the film of this page."""

    from app.models import UserWatchlist

    page_id = _movie(app, "Unknown Bank Page", 880202)
    token = csrf_token_from(
        admin_client.get(f"/movie/{page_id}").get_data(as_text=True)
    )
    data = {"csrf_token": token, "add_watchlist_submit": "1", "movie_id": "999999"}

    assert admin_client.post(f"/movie/{page_id}", data=data).status_code == 302
    assert (
        admin_client.post(f"/movie/{page_id}", data=data, headers=CARD).status_code
        == 404
    )
    with app.app_context():
        assert UserWatchlist.query.filter_by(movie_id=page_id).count() == 0


def test_a_known_suggested_film_is_rated_not_the_page(app, admin_client):
    page_id = _movie(app, "Known Suggestion Page", 880203)
    other_id = _movie(app, "Known Suggestion Other", 880204)
    token = csrf_token_from(
        admin_client.get(f"/movie/{page_id}").get_data(as_text=True)
    )
    state = admin_client.post(
        f"/movie/{page_id}",
        data={"csrf_token": token, "quick_rating": "4", "movie_id": str(other_id)},
        headers=LADDER,
    ).get_json()
    assert state["rating"] == 4.0
    assert _review_count(app, other_id) == 1
    assert _review_count(app, page_id) == 0
