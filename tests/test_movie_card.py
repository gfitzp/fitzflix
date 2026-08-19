"""The poster popover's card (#45c): the /movie_card fragment for
library records and bare TMDb ids, its live watchlist toggle, and the
data-card-url wiring on the gallery surfaces."""

import json
import re
from datetime import datetime, timedelta

from app import db
from app.models import Movie, UserMovieReview, UserWatchlist
from app.videos import star_rating_fields
from tests.factories import make_movie, make_movie_file
from tests.test_elicitation import csrf_token_from, make_candidate
from tests.test_recommendations import admin_id, make_cast, make_person


def button_tag(page, name):
    """The full <button> tag whose name attribute matches."""

    match = re.search(rf'<button[^>]*name="{name}"[^>]*>', page)
    assert match, f"no {name} button in the card"
    return match.group(0)


def test_movie_card_for_a_library_film(app, admin_client):
    """A library record's card: linked credits, runtime, synopsis, the
    In-library badge, a live ladder posting to the film's own page with
    the from_card marker, and the Add face of the watchlist toggle."""

    with app.app_context():
        director = make_person(888001, "Card Director")
        star = make_person(888002, "Card Star")
        movie = make_candidate("Card Film", 1968, director=director)
        movie.tmdb_runtime = 101
        movie.tmdb_overview = "A film about cards."
        make_cast(star, movie)
        db.session.commit()
        movie_id = movie.id

    page = admin_client.get(f"/movie_card?movie_id={movie_id}").get_data(as_text=True)
    assert "Card Film (1968)" in page
    assert f'href="/movie/{movie_id}"' in page
    assert "Card Director" in page and "credit=888001" in page
    assert "Card Star" in page and "credit=888002" in page
    assert "101 min" in page
    assert "A film about cards." in page
    assert "In library" in page

    # The live ladder posts to the film's own movie route, marked so
    # the drive's anchor never moves; ✕ is offered (no diary row yet)

    assert f'action="/movie/{movie_id}"' in page
    assert 'name="from_card"' in page
    assert 'data-ladder-live="1"' in page
    assert 'name="quick_rating" value="0"' in page

    # Both watchlist faces render, Add showing and Remove hidden

    assert "d-none" not in button_tag(page, "add_watchlist_submit")
    assert "d-none" in button_tag(page, "remove_watchlist_submit")


def test_movie_card_reflects_verdict_and_watchlist(app, admin_client):
    """A rated, watchlisted film's card shows the gold verdict (no
    estimate, no ✕ — seen films can't be waved off) and the Remove face
    of the toggle."""

    with app.app_context():
        user_id = admin_id()
        movie = make_candidate("Card Verdict", 1970)
        db.session.add(
            UserMovieReview(
                user_id=user_id,
                movie_id=movie.id,
                liked=True,
                date_reviewed=datetime.now(),
                **star_rating_fields(4.0),
            )
        )
        db.session.add(UserWatchlist(user_id=user_id, movie_id=movie.id))
        db.session.commit()
        movie_id = movie.id

    page = admin_client.get(f"/movie_card?movie_id={movie_id}").get_data(as_text=True)
    assert "star filled" in page
    assert "estimated" not in page
    assert 'name="quick_rating" value="0"' not in page
    assert "d-none" in button_tag(page, "add_watchlist_submit")
    assert "d-none" not in button_tag(page, "remove_watchlist_submit")


def test_movie_card_previews_the_estimate(app, admin_client):
    """An unlogged film's card previews the engine's estimate in gray
    stars, the same recipe as the movie page (#45a)."""

    from app.recommendations import PROFILE_KEY, SCORES_KEY

    with app.app_context():
        user_id = admin_id()
        movie = make_candidate("Card Estimated", 1972)
        db.session.commit()
        movie_id = movie.id

    app.redis.set(SCORES_KEY.format(user_id=user_id), json.dumps({str(movie_id): 9.0}))
    app.redis.set(
        PROFILE_KEY.format(user_id=user_id),
        json.dumps(
            {
                "affinities": {},
                "movies": 3,
                "calibration": {
                    "scores": [0.0, 1.0, 2.0, 3.0],
                    "stars": [1.0, 2.0, 4.0, 4.5],
                },
            }
        ),
    )

    page = admin_client.get(f"/movie_card?movie_id={movie_id}").get_data(as_text=True)
    assert "star estimated" in page
    assert "Estimated 4.5 for you" in page


def test_movie_card_for_a_bare_tmdb_id(app, admin_client, monkeypatch):
    """A film with no local record renders from TMDb, and its forms
    post to the TMDb log route — whose first tap creates the record."""

    import app.main.discover as discover
    from tests.test_reviews import JAWS_2_DETAILS, FakeTMDbDetails

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(
        discover, "tmdb_get", lambda *a, **k: FakeTMDbDetails(JAWS_2_DETAILS)
    )

    page = admin_client.get("/movie_card?tmdb_id=579").get_data(as_text=True)
    assert "Jaws 2 (1978)" in page
    assert "116 min" in page
    assert "The shark is back." in page
    assert "Roy Scheider" in page and "credit=4430" in page
    assert 'href="/review/tmdb/579"' in page
    assert 'action="/review/tmdb/579"' in page
    assert 'name="from_card"' in page
    assert "In library" not in page
    assert "estimated" not in page

    # Once a record exists for the id, the same request serves the
    # local card, aimed at the movie route

    with app.app_context():
        movie = make_movie("Jaws 2", 1978, tmdb_id=579)
        db.session.commit()
        movie_id = movie.id

    page = admin_client.get("/movie_card?tmdb_id=579").get_data(as_text=True)
    assert f'action="/movie/{movie_id}"' in page


def test_movie_card_requires_a_known_film(app, admin_client):
    """No key, an unknown movie_id, and a bare tmdb_id without an API
    key all 404 — the popover simply doesn't show."""

    assert admin_client.get("/movie_card").status_code == 404
    assert admin_client.get("/movie_card?movie_id=99999").status_code == 404
    # TestConfig has no TMDB_API_KEY, so a record-less tmdb_id can't
    # be looked up
    assert admin_client.get("/movie_card?tmdb_id=579").status_code == 404


def test_card_watchlist_toggle_round_trips_as_json(app, admin_client):
    """The card's watchlist toggle posts with the card marker and gets
    {on_watchlist} back — no redirect, no flash — in both directions."""

    with app.app_context():
        movie = make_candidate("Card Toggled", 1974)
        db.session.commit()
        movie_id = movie.id
        user_id = admin_id()

    token = csrf_token_from(
        admin_client.get(f"/movie_card?movie_id={movie_id}").get_data(as_text=True)
    )
    headers = {"X-Requested-With": "card"}

    response = admin_client.post(
        f"/movie/{movie_id}",
        data={
            "csrf_token": token,
            "from_card": "1",
            "add_watchlist_submit": "Add to Watchlist",
        },
        headers=headers,
    )
    assert response.status_code == 200
    assert response.get_json() == {"on_watchlist": True}
    with app.app_context():
        assert (
            UserWatchlist.query.filter_by(user_id=user_id, movie_id=movie_id).first()
            is not None
        )

    response = admin_client.post(
        f"/movie/{movie_id}",
        data={
            "csrf_token": token,
            "from_card": "1",
            "remove_watchlist_submit": "Remove from Watchlist",
        },
        headers=headers,
    )
    assert response.get_json() == {"on_watchlist": False}
    with app.app_context():
        assert (
            UserWatchlist.query.filter_by(user_id=user_id, movie_id=movie_id).first()
            is None
        )


def test_card_watchlist_add_creates_the_record_for_a_tmdb_film(
    app, admin_client, monkeypatch
):
    """Banking a record-less rail film from its card posts to the TMDb
    log route, which creates the record and answers the same JSON."""

    import app.main.discover as discover
    from tests.test_reviews import JAWS_2_DETAILS, FakeTMDbDetails

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(
        discover, "tmdb_get", lambda *a, **k: FakeTMDbDetails(JAWS_2_DETAILS)
    )

    token = csrf_token_from(
        admin_client.get("/movie_card?tmdb_id=579").get_data(as_text=True)
    )
    response = admin_client.post(
        "/review/tmdb/579",
        data={
            "csrf_token": token,
            "from_card": "1",
            "add_watchlist_submit": "Add to Watchlist",
        },
        headers={"X-Requested-With": "card"},
    )
    assert response.status_code == 200
    assert response.get_json() == {"on_watchlist": True}
    with app.app_context():
        user_id = admin_id()
        movie = Movie.query.filter_by(tmdb_id=579).one()
        assert (
            UserWatchlist.query.filter_by(user_id=user_id, movie_id=movie.id).first()
            is not None
        )
        movie_id = movie.id

    # With the record in place, card posts still aimed at the log route
    # forward — method, body, and headers intact — to the movie route

    response = admin_client.post(
        "/review/tmdb/579",
        data={
            "csrf_token": token,
            "from_card": "1",
            "remove_watchlist_submit": "Remove from Watchlist",
        },
        headers={"X-Requested-With": "card"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert response.get_json() == {"on_watchlist": False}
    with app.app_context():
        assert (
            UserWatchlist.query.filter_by(user_id=admin_id(), movie_id=movie_id).first()
            is None
        )


def test_index_posters_arm_the_popover(app, admin_client):
    """Landing-page rail posters carry data-card-url, so hovering (or
    tapping) any of them fetches the film's card."""

    with app.app_context():
        user_id = admin_id()
        favorite = make_movie("Popover Shelf Film", 1975)
        make_movie_file(favorite, "Bluray-1080p")
        db.session.add(
            UserMovieReview(
                user_id=user_id,
                movie_id=favorite.id,
                liked=True,
                date_watched=datetime.now() - timedelta(days=1500),
                **star_rating_fields(4.0),
            )
        )
        db.session.commit()
        favorite_id = favorite.id

    body = admin_client.get("/").get_data(as_text=True)
    assert f'data-card-url="/movie_card?movie_id={favorite_id}"' in body
