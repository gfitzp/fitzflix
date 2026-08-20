"""The poster popover's card (#45c): the /movie_card fragment for
library records and bare TMDb ids, the tile-side actions and their
batched /movie_states hydration (the Aug 2026 revision moved the
ladder and watchlist toggle out of the card and the badges in), and
the data-card-url wiring on the gallery surfaces."""

import json
from datetime import datetime, timedelta

from app import db
from app.models import Movie, UserMovieReview, UserMovieStatus, UserWatchlist
from app.videos import star_rating_fields
from tests.factories import make_movie, make_movie_file
from tests.test_elicitation import csrf_token_from, make_candidate
from tests.test_recommendations import admin_id, make_cast, make_person


def test_movie_card_for_a_library_film(app, admin_client):
    """A library record's card: linked credits, runtime, synopsis, the
    In-library badge, and the quality badge in shopping colors — no
    forms at all, the actions live on the tile."""

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

    # The Bluray-1080p copy meets the upgrade threshold, so the badge
    # is green; the empty slot waits for the tile's own labels

    assert 'text-bg-success me-1 mb-1">Bluray-1080p' in page
    assert "data-card-reasons" in page

    # Informational only: no ladder, no watchlist toggle, no forms

    assert "quick_rating" not in page
    assert "star-row" not in page
    assert "add_watchlist_submit" not in page
    assert "<form" not in page


def test_movie_card_badges_watchlist_and_amber_quality(app, admin_client):
    """A watchlisted film whose best copy lags the threshold badges
    both facts: the amber quality tier and the watchlist badge."""

    with app.app_context():
        user_id = admin_id()
        movie = make_movie("Card Verdict", 1970)
        make_movie_file(movie, "DVD")
        db.session.add(UserWatchlist(user_id=user_id, movie_id=movie.id))
        db.session.commit()
        movie_id = movie.id

    page = admin_client.get(f"/movie_card?movie_id={movie_id}").get_data(as_text=True)
    assert 'text-bg-warning me-1 mb-1">DVD' in page
    assert "On your watchlist" in page

    # An excluded film's badge goes green even below the threshold —
    # the shopping answer, not the raw tier

    with app.app_context():
        db.session.get(Movie, movie_id).shopping_list_exclude = True
        db.session.commit()
    page = admin_client.get(f"/movie_card?movie_id={movie_id}").get_data(as_text=True)
    assert 'text-bg-success me-1 mb-1">DVD' in page


def test_movie_states_batch_hydration_payload(app, admin_client):
    """/movie_states answers ladder-and-watchlist state for many films
    in one fetch: verdicts, flags, stored estimates, watchlist faces —
    with tmdb ids answered under their own key, mapped through a local
    record when one exists."""

    from app.recommendations import PROFILE_KEY, SCORES_KEY

    with app.app_context():
        user_id = admin_id()
        rated = make_candidate("States Rated", 1980)
        estimated = make_candidate("States Estimated", 1981)
        flagged = make_candidate("States Flagged", 1982)
        listed = make_movie("States Listed", 1983, tmdb_id=777001)
        db.session.add(
            UserMovieReview(
                user_id=user_id,
                movie_id=rated.id,
                liked=True,
                date_reviewed=datetime.now(),
                **star_rating_fields(4.0),
            )
        )
        db.session.add(
            UserMovieStatus(user_id=user_id, movie_id=flagged.id, kind="not_interested")
        )
        db.session.add(UserWatchlist(user_id=user_id, movie_id=listed.id))
        db.session.commit()
        rated_id, estimated_id, flagged_id = rated.id, estimated.id, flagged.id
        listed_tmdb = listed.tmdb_id

    app.redis.set(
        SCORES_KEY.format(user_id=user_id), json.dumps({str(estimated_id): 9.0})
    )
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

    response = admin_client.get(
        f"/movie_states?movie_ids={rated_id},{estimated_id},{flagged_id}"
        f"&tmdb_ids={listed_tmdb},999999"
    )
    assert response.status_code == 200
    payload = response.get_json()

    assert payload["movies"][str(rated_id)]["rating"] == 4.0
    assert payload["movies"][str(rated_id)]["has_review"] is True
    assert payload["movies"][str(estimated_id)]["rating"] is None
    assert payload["movies"][str(estimated_id)]["estimated"] == 4.5
    assert payload["movies"][str(flagged_id)]["flagged"] is True
    assert payload["tmdb"][str(listed_tmdb)]["on_watchlist"] is True

    # An unknown tmdb id answers the empty state instead of erroring

    assert payload["tmdb"]["999999"]["has_review"] is False
    assert payload["tmdb"]["999999"]["on_watchlist"] is False


def test_movie_states_estimates_record_less_tmdb_ids(app, admin_client):
    """A tmdb id with no local record — most of a filmography page —
    answers with an estimate from the shared source's tmdb lane,
    scored from the cached enriched payload with nothing persisted to
    the database; ids TMDb can't supply stay at the empty state."""

    from app.models import Movie
    from app.recommendations import PROFILE_KEY

    with app.app_context():
        user_id = admin_id()

    app.redis.set(
        PROFILE_KEY.format(user_id=user_id),
        json.dumps(
            {
                "affinities": {
                    "genre:35": {
                        "class": "genre",
                        "label": "Comedy",
                        "count": 3,
                        "score": 0.5,
                    }
                },
                "movies": 3,
                "calibration": {
                    "scores": [0.0, 0.1, 0.2, 0.3],
                    "stars": [1.0, 2.0, 4.0, 4.5],
                },
            }
        ),
    )
    app.redis.set(
        "fitzflix:tmdb:movie:888777:enriched",
        json.dumps(
            {
                "tmdb_id": 888777,
                "title": "Filmography Ghost",
                "year": "1994",
                "original_language": "en",
                "genres": [{"id": 35, "name": "Comedy"}],
                "keywords": [],
                "cast": [],
                "crew": [],
            }
        ),
    )

    payload = admin_client.get("/movie_states?tmdb_ids=888777,999999").get_json()
    estimated = payload["tmdb"]["888777"]["estimated"]
    assert estimated is not None
    assert payload["tmdb"]["999999"]["estimated"] is None

    # Repeats answer from the overlay with the same number, and the
    # film still has no database record

    again = admin_client.get("/movie_states?tmdb_ids=888777").get_json()
    assert again["tmdb"]["888777"]["estimated"] == estimated
    with app.app_context():
        assert Movie.query.filter_by(tmdb_id=888777).first() is None


def test_movie_states_live_scores_films_the_nightly_map_missed(app, admin_client):
    """A record created after the last recompute — no stored score —
    still estimates in a tile batch: /movie_states scores it live
    through the shared resolver and patches the map, so the movie page
    shows the very same number (the So I Married an Axe Murderer bug:
    3 stars on the film's page, a blank ladder on the watchlist)."""

    from app.recommendations import PROFILE_KEY, stored_scores

    with app.app_context():
        user_id = admin_id()
        fresh = make_movie("States Fresh Add", 1993, tmdb_data_as_of=datetime.now())
        db.session.commit()
        fresh_id = fresh.id

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

    payload = admin_client.get(f"/movie_states?movie_ids={fresh_id}").get_json()
    estimated = payload["movies"][str(fresh_id)]["estimated"]
    assert estimated is not None

    # The live score was patched into the shared map, and the movie
    # page reads the identical estimate from it

    assert fresh_id in stored_scores(app.redis, user_id)
    page = admin_client.get(f"/movie/{fresh_id}").get_data(as_text=True)
    assert f"Estimated {estimated} for you" in page


def test_gallery_tiles_carry_the_actions(app, admin_client):
    """A landing-rail tile renders the blank ladder and the watchlist
    toggle under the poster, wired for hydration: the state container
    names the movie, the forms post to the film's route with the
    from_card marker, and the badges are gone from the tile."""

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
    assert f'data-state-movie="{favorite_id}"' in body
    assert 'data-ladder-live="1"' in body
    assert 'name="from_card"' in body
    assert 'name="add_watchlist_submit"' in body

    # Variant-4 tiles (#77): the cell pins its actions to the bottom
    # (poster-cell flex column; spacing is pt-1 — a Bootstrap mt-*
    # would defeat the margin-top:auto pin)

    assert "poster-cell" in body
    assert 'class="poster-actions pt-1"' in body

    # The last-watched label rides the anchor for the card to display;
    # the badges themselves left the tiles

    assert "data-card-reasons='[\"Last watched" in body
    assert ">Last watched" not in body
    assert "On your watchlist" not in body


def test_movie_card_for_a_bare_tmdb_id(app, admin_client, monkeypatch):
    """A film with no local record renders from TMDb — informational
    only, linking to the TMDb log page."""

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
    assert "In library" not in page
    assert "<form" not in page

    # Once a record exists for the id, the same request serves the
    # local card, linking to the movie page

    with app.app_context():
        movie = make_movie("Jaws 2", 1978, tmdb_id=579)
        db.session.commit()
        movie_id = movie.id

    page = admin_client.get("/movie_card?tmdb_id=579").get_data(as_text=True)
    assert f'href="/movie/{movie_id}"' in page


def test_movie_card_requires_a_known_film(app, admin_client):
    """No key, an unknown movie_id, and a bare tmdb_id without an API
    key all 404 — the popover simply doesn't show."""

    assert admin_client.get("/movie_card").status_code == 404
    assert admin_client.get("/movie_card?movie_id=99999").status_code == 404
    # TestConfig has no TMDB_API_KEY, so a record-less tmdb_id can't
    # be looked up
    assert admin_client.get("/movie_card?tmdb_id=579").status_code == 404


def test_tile_watchlist_toggle_round_trips_as_json(app, admin_client):
    """The tile's watchlist toggle posts with the card marker and gets
    {on_watchlist} back — no redirect, no flash — in both directions."""

    with app.app_context():
        movie = make_candidate("Card Toggled", 1974)
        db.session.commit()
        movie_id = movie.id
        user_id = admin_id()

    # The card is form-less now; the token comes from the tile forms
    # on any gallery page

    token = csrf_token_from(
        admin_client.get(f"/movie/{movie_id}").get_data(as_text=True)
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


def test_tile_watchlist_add_creates_the_record_for_a_tmdb_film(
    app, admin_client, monkeypatch
):
    """Banking a record-less rail film from its tile posts to the TMDb
    log route, which creates the record and answers the same JSON."""

    import app.main.discover as discover
    from tests.test_reviews import JAWS_2_DETAILS, FakeTMDbDetails

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(
        discover, "tmdb_get", lambda *a, **k: FakeTMDbDetails(JAWS_2_DETAILS)
    )

    token = csrf_token_from(admin_client.get("/review/tmdb/579").get_data(as_text=True))
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

    # With the record in place, tile posts still aimed at the log route
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


def test_movie_states_estimates_bare_unrated_watches(app, admin_client):
    """A logged-but-unrated viewing — a Plex watch — keeps its tile's
    estimate: the guess previews until the user's own stars exist, and
    only a real rating (or the ✕) retires it."""

    from app.recommendations import PROFILE_KEY

    with app.app_context():
        user_id = admin_id()
        watched = make_movie("States Bare Watch", 1994, tmdb_data_as_of=datetime.now())
        db.session.add(
            UserMovieReview(
                user_id=user_id, movie_id=watched.id, date_watched=datetime.now()
            )
        )
        db.session.commit()
        watched_id = watched.id

    app.redis.set(
        PROFILE_KEY.format(user_id=user_id),
        json.dumps(
            {
                "affinities": {},
                "movies": 3,
                "calibration": {
                    "scores": [0.0, 0.1, 0.2, 0.3],
                    "stars": [1.0, 2.0, 4.0, 4.5],
                },
            }
        ),
    )

    payload = admin_client.get(f"/movie_states?movie_ids={watched_id}").get_json()
    state = payload["movies"][str(watched_id)]
    assert state["has_review"] is True
    assert state["rating"] is None
    assert state["estimated"] is not None
