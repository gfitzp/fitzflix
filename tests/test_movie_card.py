"""Test the card of the poster popover (#45c).

These tests cover the /movie_card fragment for library records and for
bare TMDB ids. They cover the tile-side actions and their batched
/movie_states hydration. The 2026-08 revision moved the ladder and the
watchlist toggle out of the card, and moved the badges in. They also
cover the data-card-url wiring on the gallery surfaces."""

import json
from datetime import datetime, timedelta

from app import db
from app.models import Movie, UserMovieReview, UserMovieStatus, UserWatchlist
from app.videos import star_rating_fields
from tests.factories import make_movie, make_movie_file
from tests.test_elicitation import csrf_token_from, make_candidate
from tests.test_recommendations import admin_id, make_cast, make_person


def test_movie_card_for_a_library_film(app, admin_client):
    """Test the card of a library record.

    The card shows the linked credits, the meta line (runtime, genres,
    and the US rating in its box), the synopsis, and the In-library badge
    in shopping colors. It has no forms. The actions are on the tile."""

    from app.models import RefTMDBCertification, TMDBGenre

    with app.app_context():
        director = make_person(888001, "Card Director")
        star = make_person(888002, "Card Star")
        movie = make_candidate("Card Film", 1968, director=director)
        movie.tmdb_runtime = 101
        movie.tmdb_overview = "A film about cards."
        movie.genres.append(TMDBGenre(id=888003, name="Card Drama"))
        movie.genres.append(TMDBGenre(id=888004, name="Card Mystery"))
        movie.certifications.append(
            RefTMDBCertification(country="US", certification="PG-13")
        )
        # Only the US rating reaches the card
        movie.certifications.append(
            RefTMDBCertification(country="GB", certification="15")
        )
        make_cast(star, movie)
        db.session.commit()
        movie_id = movie.id

    page = admin_client.get(f"/movie_card?movie_id={movie_id}").get_data(as_text=True)
    assert "Card Film (1968)" in page
    assert f'href="/movie/{movie_id}"' in page
    assert "Card Director" in page and "credit=888001" in page
    assert "Card Star" in page and "credit=888002" in page
    assert "101 min" in page
    assert "Card Drama, Card Mystery" in page
    assert ">PG-13</span>" in page
    assert ">15</span>" not in page
    assert "A film about cards." in page
    assert "In library" in page

    # The Bluray-1080p copy meets the upgrade threshold. Thus, the
    # In-library badge is green (the quality tier itself left the card in
    # 2026-08). The empty slot waits for the labels of the tile

    assert 'text-bg-success align-middle me-1" title="In your Fitzflix library' in page
    assert "Bluray-1080p" not in page
    assert "data-card-reasons" in page

    # Information only: no ladder, no watchlist toggle, no forms

    assert "quick_rating" not in page
    assert "star-row" not in page
    assert "add_watchlist_submit" not in page
    assert "<form" not in page


def test_movie_card_badges_watchlist_and_amber_library(app, admin_client):
    """Test a watchlisted film with a best copy below the threshold.

    The card shows both facts: the amber In-library badge and the
    watchlist badge."""

    with app.app_context():
        user_id = admin_id()
        movie = make_movie("Card Verdict", 1970)
        make_movie_file(movie, "DVD")
        db.session.add(UserWatchlist(user_id=user_id, movie_id=movie.id))
        db.session.commit()
        movie_id = movie.id

    page = admin_client.get(f"/movie_card?movie_id={movie_id}").get_data(as_text=True)
    assert 'text-bg-warning align-middle me-1" title="In your Fitzflix library' in page
    assert "On your watchlist" in page

    # The badge of an excluded film is green, also below the threshold.
    # This is the shopping answer, not the raw tier

    with app.app_context():
        db.session.get(Movie, movie_id).shopping_list_exclude = True
        db.session.commit()
    page = admin_client.get(f"/movie_card?movie_id={movie_id}").get_data(as_text=True)
    assert 'text-bg-success align-middle me-1" title="In your Fitzflix library' in page


def test_movie_page_library_badge_wears_shopping_colors(app, admin_client):
    """Test that the In-library badge on the movie page uses the shopping
    colors.

    The colors are the same as on the popover (revision requested by
    Glenn, 2026-08). Amber is for a copy that needs an upgrade. Green is
    for a settled copy. A sub-threshold copy of an excluded film is also
    green."""

    from tests.test_streaming import subscribe

    subscribe(app, 8, "Netflix")
    with app.app_context():
        lagging = make_movie("Page Lagging", 1971, tmdb_id=7101)
        make_movie_file(lagging, "DVD")
        settled = make_movie("Page Settled", 1972, tmdb_id=7102)
        make_movie_file(settled, "Bluray-1080p")
        excluded = make_movie(
            "Page Excluded", 1973, tmdb_id=7103, shopping_list_exclude=True
        )
        make_movie_file(excluded, "DVD")
        db.session.commit()
        lagging_id, settled_id, excluded_id = lagging.id, settled.id, excluded.id

    page = admin_client.get(f"/movie/{lagging_id}").get_data(as_text=True)
    assert 'text-bg-warning align-middle me-1" title="In your Fitzflix library' in page
    page = admin_client.get(f"/movie/{settled_id}").get_data(as_text=True)
    assert 'text-bg-success align-middle me-1" title="In your Fitzflix library' in page
    page = admin_client.get(f"/movie/{excluded_id}").get_data(as_text=True)
    assert 'text-bg-success align-middle me-1" title="In your Fitzflix library' in page


def test_movie_page_meta_line_leads_in_the_popup_order(app, admin_client):
    """Test that the movie page reads like its popover card.

    It has 1 meta line: directed by, runtime, genres (with their
    library-filter links), and the US rating in its bordered box. The
    synopsis comes after."""

    from app.models import RefTMDBCertification, TMDBGenre

    with app.app_context():
        director = make_person(888011, "Page Director")
        movie = make_candidate("Page Ordered", 1969, director=director)
        movie.tmdb_runtime = 95
        movie.tmdb_overview = "A film about ordering."
        genre = TMDBGenre(id=888012, name="Page Drama")
        movie.genres.append(genre)
        movie.certifications.append(
            RefTMDBCertification(country="US", certification="R")
        )
        db.session.commit()
        movie_id = movie.id

    page = admin_client.get(f"/movie/{movie_id}").get_data(as_text=True)
    assert "Page Director" in page
    assert "95&nbsp;minutes" in page
    assert 'genre=888012" class="link-secondary text-secondary">Page Drama</a>' in page
    assert ">R</span>" in page
    assert (
        page.index("Page Director")
        < page.index("95&nbsp;minutes")
        < page.index("Page Drama</a>")
        < page.index(">R</span>")
        < page.index("A film about ordering.")
    )


def test_movie_states_batch_hydration_payload(app, admin_client):
    """Test that /movie_states answers the state of many films in 1 fetch.

    The state is the ladder and watchlist state: verdicts, flags, stored
    estimates, and watchlist faces. It answers tmdb ids under their own
    key. It maps a tmdb id through a local record when one exists."""

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

    # An unknown tmdb id answers the empty state. It does not cause an
    # error

    assert payload["tmdb"]["999999"]["has_review"] is False
    assert payload["tmdb"]["999999"]["on_watchlist"] is False


def test_movie_states_estimates_record_less_tmdb_ids(app, admin_client):
    """Test a tmdb id with no local record (most of a filmography page).

    It answers with an estimate from the tmdb lane of the shared source.
    The score comes from the cached enriched payload. Nothing goes into
    the database. An id that TMDB cannot supply stays at the empty
    state."""

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

    # A repeat answers from the overlay with the same number. The film
    # still has no database record

    again = admin_client.get("/movie_states?tmdb_ids=888777").get_json()
    assert again["tmdb"]["888777"]["estimated"] == estimated
    with app.app_context():
        assert Movie.query.filter_by(tmdb_id=888777).first() is None


def test_movie_states_live_scores_films_the_nightly_map_missed(app, admin_client):
    """Test the estimate for a record created after the last recompute.

    Such a record has no stored score. It still gets an estimate in a
    tile batch. /movie_states scores it live through the shared resolver
    and patches the map. Thus, the movie page shows the same number. This
    was the So I Married an Axe Murderer bug: 3 stars on the page of the
    film, a blank ladder on the watchlist."""

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
    """Test that a landing-rail tile renders its actions under the poster.

    The actions are the blank ladder and the watchlist toggle. They are
    wired for hydration. The state container names the movie. The forms
    post to the route of the film with the from_card marker. The badges
    are gone from the tile."""

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

    # Variant-4 tiles: the cell pins its actions to the bottom
    # (poster-cell flex column). The spacing is pt-1. A Bootstrap mt-*
    # would defeat the margin-top:auto pin

    assert "poster-cell" in body
    assert 'class="poster-actions pt-1"' in body

    # The last-watched label goes with the anchor for the card to show.
    # The badges themselves left the tiles

    assert "data-card-reasons='[\"Last watched" in body
    assert ">Last watched" not in body
    assert "On your watchlist" not in body


def test_movie_card_for_a_bare_tmdb_id(app, admin_client, monkeypatch):
    """Test that a film with no local record renders from TMDB.

    The card is information only. It links to the TMDB log page."""

    import app.main.discover as discover
    from tests.test_reviews import JAWS_2_DETAILS, FakeTMDBDetails

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(
        discover, "tmdb_get", lambda *a, **k: FakeTMDBDetails(JAWS_2_DETAILS)
    )

    page = admin_client.get("/movie_card?tmdb_id=579").get_data(as_text=True)
    assert "Jaws 2 (1978)" in page
    assert "116 min" in page
    assert "Horror, Thriller" in page
    assert ">PG</span>" in page
    assert "The shark is back." in page
    assert "Roy Scheider" in page and "credit=4430" in page
    assert 'href="/review/tmdb/579"' in page
    assert "In library" not in page
    assert "<form" not in page

    # After a record exists for the id, the same request serves the local
    # card. It links to the movie page

    with app.app_context():
        movie = make_movie("Jaws 2", 1978, tmdb_id=579)
        db.session.commit()
        movie_id = movie.id

    page = admin_client.get("/movie_card?tmdb_id=579").get_data(as_text=True)
    assert f'href="/movie/{movie_id}"' in page


def test_movie_card_requires_a_known_film(app, admin_client):
    """Test that a bad request answers 404.

    No key, an unknown movie_id, and a bare tmdb_id without an API key
    all answer 404. The popover does not show."""

    assert admin_client.get("/movie_card").status_code == 404
    assert admin_client.get("/movie_card?movie_id=99999").status_code == 404
    # TestConfig has no TMDB_API_KEY. Thus, Fitzflix cannot look up a
    # tmdb_id without a record
    assert admin_client.get("/movie_card?tmdb_id=579").status_code == 404


def test_tile_watchlist_toggle_round_trips_as_json(app, admin_client):
    """Test the watchlist toggle of the tile in both directions.

    The toggle posts with the card marker and gets {on_watchlist} back.
    There is no redirect and no flash."""

    with app.app_context():
        movie = make_candidate("Card Toggled", 1974)
        db.session.commit()
        movie_id = movie.id
        user_id = admin_id()

    # The card has no forms now. The token comes from the tile forms on
    # any gallery page

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
    """Test the bank of a rail film without a record from its tile.

    The tile posts to the TMDB log route. The route creates the record
    and answers the same JSON."""

    import app.main.discover as discover
    from tests.test_reviews import JAWS_2_DETAILS, FakeTMDBDetails

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(
        discover, "tmdb_get", lambda *a, **k: FakeTMDBDetails(JAWS_2_DETAILS)
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

    # With the record in place, a tile post that still points at the log
    # route forwards to the movie route. The method, the body, and the
    # headers are unchanged

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
    """Test that a logged but unrated viewing (a Plex watch) keeps the
    estimate on its tile.

    The tile shows the guess until the stars of the user exist. Only a
    real rating (or the ✕) removes it."""

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


def test_movie_states_fills_a_whole_page_in_one_pass(app, admin_client):
    """Test that 1 hydration pass covers every film without a record on a
    page.

    Fitzflix never caps the cached payloads. Thus, a filmography fills on
    the first visit, not 20 films for each reload."""

    from app.recommendations import PROFILE_KEY

    with app.app_context():
        user_id = admin_id()

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
    tmdb_ids = list(range(870001, 870031))
    for tmdb_id in tmdb_ids:
        app.redis.set(
            f"fitzflix:tmdb:movie:{tmdb_id}:enriched",
            json.dumps(
                {
                    "tmdb_id": tmdb_id,
                    "title": f"Career Film {tmdb_id}",
                    "year": "1994",
                    "original_language": "en",
                    "genres": [{"id": 35, "name": "Comedy"}],
                    "keywords": [],
                    "cast": [],
                    "crew": [],
                }
            ),
        )

    payload = admin_client.get(
        "/movie_states?tmdb_ids=" + ",".join(str(t) for t in tmdb_ids)
    ).get_json()
    estimated = [
        tmdb_id
        for tmdb_id in tmdb_ids
        if payload["tmdb"][str(tmdb_id)]["estimated"] is not None
    ]
    assert len(estimated) == 30
