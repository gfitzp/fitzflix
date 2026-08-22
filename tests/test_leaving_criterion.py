"""The leaving-Criterion shelf: the sanctioned scrape of the official
leaving page, TMDb matching, the monthly refresh task, and the
taste-ranked landing-page shelf with its shopping-list urgency badge."""

import json

from datetime import date, timedelta

from tests.factories import make_movie, make_movie_file

LEAVING_HTML = """
<li class="js-collection-item collection-item-1 item-type-video"></li>
<div class="tooltip background-white" id="collection-tooltip-1">
  <h3 class="tooltip-item-title site-font-primary-family"><strong>The Searchers</strong></h3>
  <p>Directed by John Ford • 1956 • United States<br />Starring John Wayne, Jeffrey Hunter</p>
</div>
<div class="tooltip background-white" id="collection-tooltip-2">
  <h3 class="tooltip-item-title"><strong>Love &amp; Mercy</strong></h3>
  <p>Directed by Bill Pohlad •&nbsp;2015 • United States</p>
</div>
"""


class FakeResponse:
    """Canned HTTP response."""

    def __init__(self, text="", status_code=200, payload=None):
        self.text = text
        self.status_code = status_code
        self._payload = payload or {}

    def raise_for_status(self):
        """Never an HTTP error."""

    def json(self):
        """The canned payload."""

        return self._payload


def test_leaving_page_candidates_roll_over_year_boundaries(app):
    from app.leaving_criterion import leaving_page_candidates

    candidates = leaving_page_candidates(date(2026, 12, 15))
    urls = [url for url, _ in candidates]
    departures = [departs for _, departs in candidates]

    assert urls == [
        "https://www.criterionchannel.com/leaving-december-31",
        "https://www.criterionchannel.com/leaving-january-31",
        "https://www.criterionchannel.com/leaving-november-30",
    ]
    assert departures == [date(2026, 12, 31), date(2027, 1, 31), date(2026, 11, 30)]


def test_parse_leaving_page_reads_tooltips(app):
    from app.leaving_criterion import parse_leaving_page

    films = parse_leaving_page(LEAVING_HTML)
    assert films == [
        {"title": "The Searchers", "director": "John Ford", "year": 1956},
        {"title": "Love & Mercy", "director": "Bill Pohlad", "year": 2015},
    ]
    assert parse_leaving_page("<html>nothing here</html>") == []


def test_match_tmdb_id_searches_by_year_and_caches(app, monkeypatch):
    import app.leaving_criterion as leaving_criterion

    calls = []

    def fake_tmdb_get(url, params=None, **kwargs):
        calls.append(params)
        if params.get("primary_release_year"):
            return FakeResponse(payload={"results": [{"id": 3110}]})
        return FakeResponse(payload={"results": [{"id": 9999}]})

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(leaving_criterion, "tmdb_get", fake_tmdb_get)

    with app.app_context():
        assert leaving_criterion.match_tmdb_id("The Searchers", 1956) == 3110
        assert leaving_criterion.match_tmdb_id("The Searchers", 1956) == 3110

    # One search, cached thereafter

    assert len(calls) == 1
    assert calls[0]["primary_release_year"] == 1956


def test_match_tmdb_id_verifies_the_director(app, monkeypatch):
    """A generic title's popular first result loses to the candidate
    whose credits name the scraped director — Bas Devos's "Here"
    (2023), not "Right Here, Right Now" (the Aug 2026 shelf mistake).
    A candidate with no director credited passes on an exact title
    and year; a director TMDb can't corroborate leaves the film
    unmatched; and the director-aware cache key is its own."""

    import app.leaving_criterion as leaving_criterion

    credits = {
        1083055: [{"name": "Jak Hutchcraft", "job": "Director"}],
        1463730: [{"name": "Bas Devos", "job": "Director"}],
        162480: [{"name": "Hal Hartley", "job": "Director"}],
        555: [],
    }
    calls = []

    def fake_tmdb_get(url, params=None, **kwargs):
        calls.append(url)
        if "/search/movie" in url:
            if params.get("query") == "Here":
                return FakeResponse(
                    payload={
                        "results": [
                            {
                                "id": 1083055,
                                "title": "Right Here, Right Now",
                                "release_date": "2023-02-04",
                            },
                            {
                                "id": 1463730,
                                "title": "here",
                                "release_date": "2023-10-24",
                            },
                        ]
                    }
                )
            if params.get("query") == "Kid":
                return FakeResponse(
                    payload={
                        "results": [
                            {
                                "id": 1885,
                                "title": "The Karate Kid",
                                "release_date": "1984-06-22",
                            }
                        ]
                    }
                )
            if params.get("query") == "Ambition" and not params.get(
                "primary_release_year"
            ):
                if params.get("page", 1) == 1:
                    return FakeResponse(
                        payload={
                            "results": [
                                {
                                    "id": 9000 + n,
                                    "title": f"Stranger {n}",
                                    "release_date": "2010-01-01",
                                }
                                for n in range(20)
                            ]
                        }
                    )
                return FakeResponse(
                    payload={
                        "results": [
                            {
                                "id": 162480,
                                "title": "Ambition",
                                "release_date": "1992-01-31",
                            }
                        ]
                    }
                )
            if params.get("query") == "Opera No. 1":
                return FakeResponse(
                    payload={
                        "results": [
                            {
                                "id": 555,
                                "title": "Opera No. 1",
                                "release_date": "1994-01-01",
                            }
                        ]
                    }
                )
            return FakeResponse(payload={"results": []})
        for tmdb_id, crew in credits.items():
            if url.endswith(f"/movie/{tmdb_id}/credits"):
                return FakeResponse(payload={"crew": crew})
        return FakeResponse(payload={})

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(leaving_criterion, "tmdb_get", fake_tmdb_get)

    with app.app_context():
        # Exact-title candidate first, so only one credits call
        assert leaving_criterion.match_tmdb_id("Here", 2023, "Bas Devos") == 1463730
        assert [c for c in calls if "/credits" in c] == [
            app.config["TMDB_API_URL"] + "/movie/1463730/credits"
        ]

        # Hal Hartley's "Kid" is not The Karate Kid: unmatched, and the
        # verdict is cached so the next lookup costs nothing
        calls.clear()
        assert leaving_criterion.match_tmdb_id("Kid", 1984, "Hal Hartley") is None
        assert leaving_criterion.match_tmdb_id("Kid", 1984, "Hal Hartley") is None
        assert (
            len([c for c in calls if "/search/movie" in c]) == 2
        )  # with year, then without

        # No director credited on TMDb: the exact title and year carry it
        assert leaving_criterion.match_tmdb_id("Opera No. 1", 1994, "Jane Doe") == 555

        # Criterion dates Hal Hartley's "Ambition" 1991, TMDb 1992: the
        # year search finds nothing, the year-less fallback reads a
        # second page, and only the exact-title candidate there costs
        # a credits call — none of the twenty strangers on page one
        calls.clear()
        assert (
            leaving_criterion.match_tmdb_id("Ambition", 1991, "Hal Hartley") == 162480
        )
        assert [c for c in calls if "/credits" in c] == [
            app.config["TMDB_API_URL"] + "/movie/162480/credits"
        ]

        # Without a director the exact-title candidate still beats the
        # popular first result, and the lookup keeps its own cache
        # entry — a title-only verdict never answers a director-aware
        # query
        assert leaving_criterion.match_tmdb_id("Here", 2023) == 1463730
        assert (
            len(list(app.redis.scan_iter("fitzflix:criterion:match:here-2023*"))) == 2
        )


def test_refresh_task_scrapes_matches_and_stores(app, monkeypatch):
    import app.leaving_criterion as leaving_criterion

    def fake_requests_get(url, params=None, timeout=None):
        if params and params.get("page", 1) == 1:
            return FakeResponse(text=LEAVING_HTML)
        return FakeResponse(text="<html>empty</html>")

    def fake_tmdb_get(url, params=None, **kwargs):
        if "/search/movie" in url:
            # Only The Searchers matches: Love & Mercy exercises the
            # unmatched-films path
            if params.get("query") == "The Searchers":
                return FakeResponse(
                    payload={
                        "results": [
                            {
                                "id": 3110,
                                "title": "The Searchers",
                                "release_date": "1956-05-16",
                            }
                        ]
                    }
                )
            return FakeResponse(payload={"results": []})
        if url.endswith("/movie/3110/credits"):
            # The scraped director, verified against the credits
            return FakeResponse(
                payload={"crew": [{"name": "John Ford", "job": "Director"}]}
            )
        if url.endswith("/movie/3110"):
            return FakeResponse(
                payload={
                    "id": 3110,
                    "title": "The Searchers",
                    "release_date": "1956-05-16",
                    "poster_path": "/3110.jpg",
                    "runtime": 119,
                    "overview": "An obsessive frontier search.",
                    "original_language": "en",
                    "genres": [{"id": 37, "name": "Western"}],
                    "keywords": {"keywords": []},
                    "credits": {"cast": [], "crew": []},
                }
            )
        return FakeResponse(payload={"results": []})

    import app.streaming_rail as streaming_rail

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(leaving_criterion.requests, "get", fake_requests_get)
    monkeypatch.setattr(leaving_criterion, "tmdb_get", fake_tmdb_get)
    monkeypatch.setattr(streaming_rail, "tmdb_get", fake_tmdb_get)

    assert leaving_criterion.refresh_leaving_criterion() is True

    stored = json.loads(app.redis.get(leaving_criterion.LEAVING_KEY))
    assert stored["departs"]
    assert stored["source"].startswith("https://www.criterionchannel.com/leaving-")
    ids = [item["tmdb_id"] for item in stored["items"]]
    assert ids == [3110, None]
    assert stored["items"][0]["genres"] == [{"id": 37, "name": "Western"}]
    assert stored["items"][0]["overview"] == "An obsessive frontier search."

    # The unmatched film keeps its scraped facts so the /leaving page
    # can still list it

    assert stored["items"][1]["title"] == "Love & Mercy"
    assert stored["items"][1]["director"] == "Bill Pohlad"
    assert stored["items"][1]["year"] == 2015


def shelf_item(tmdb_id, title, runtime=100, genre=(37, "Western")):
    """A stored leaving-set item: the trimmed enriched payload."""

    genre_id, genre_name = genre
    return {
        "tmdb_id": tmdb_id,
        "title": title,
        "year": "1956",
        "poster_path": None,
        "runtime": runtime,
        "original_language": "en",
        "genres": [{"id": genre_id, "name": genre_name}],
        "keywords": [],
        "cast": [],
        "crew": [],
    }


def plant_shelf(app, items, departs=None):
    """Store a leaving set with a future departure date."""

    from app.leaving_criterion import LEAVING_KEY

    app.redis.set(
        LEAVING_KEY,
        json.dumps(
            {
                "fetched_at": "2026-08-01 03:30",
                "departs": (departs or date.today() + timedelta(days=10)).isoformat(),
                "source": "https://www.criterionchannel.com/leaving-august-31",
                "items": items,
            }
        ),
    )


def subscribe_criterion(app):
    """Subscribe the admin user to the Criterion Channel and plant a
    Western-leaning taste profile."""

    from app import db
    from app.leaving_criterion import CRITERION_PROVIDER_ID
    from app.models import User, UserStreamingProvider

    with app.app_context():
        user = User.query.filter_by(admin=True).first()
        db.session.add(
            UserStreamingProvider(
                user_id=user.id,
                provider_id=CRITERION_PROVIDER_ID,
                name="Criterion Channel",
                logo_path="/criterion.jpg",
            )
        )
        db.session.commit()
        user_id = user.id

    app.redis.set(
        f"fitzflix:recs:profile:{user_id}",
        json.dumps(
            {
                "affinities": {
                    "genre:37": {
                        "class": "genre",
                        "label": "Western",
                        "count": 3,
                        "score": 0.5,
                    }
                },
                "movies": 3,
            }
        ),
    )
    return user_id


def test_shelf_ranks_excludes_and_badges(app, admin_client):
    from app import db
    from app.models import UserMovieReview, UserWatchlist
    from app.videos import star_rating_fields

    user_id = subscribe_criterion(app)

    with app.app_context():
        owned = make_movie("Shelf Owned", 1956, tmdb_id=8102)
        make_movie_file(owned, "Bluray-1080p")

        # Watchlisted (and even previously logged): the urgency case —
        # watch it before it leaves, or buy the disc

        wanted = make_movie("Shelf Wanted", 1956, tmdb_id=8103)
        db.session.add(
            UserMovieReview(
                user_id=user_id, movie_id=wanted.id, **star_rating_fields(4.0)
            )
        )
        db.session.add(UserWatchlist(user_id=user_id, movie_id=wanted.id))

        # Logged but not watchlisted: seen, not wanted — drops entirely

        dismissed = make_movie("Shelf Dismissed", 1956, tmdb_id=8104)
        db.session.add(
            UserMovieReview(
                user_id=user_id, movie_id=dismissed.id, **star_rating_fields(3.0)
            )
        )
        db.session.commit()

    plant_shelf(
        app,
        [
            shelf_item(8101, "Shelf Fresh", runtime=95),
            shelf_item(8102, "Shelf Owned"),
            shelf_item(8103, "Shelf Wanted", runtime=200),
            shelf_item(8104, "Shelf Dismissed"),
            # An unmatched film: the shelf must skip it quietly
            {
                "title": "Shelf Unmatched",
                "director": "Jane Doe",
                "year": 1962,
                "tmdb_id": None,
            },
        ],
    )

    body = admin_client.get("/").get_data(as_text=True)
    assert 'id="leaving-shelf"' in body
    assert "Shelf Fresh (1956)" in body
    assert "Shelf Wanted (1956)" in body
    # The watchlist badge lives in the popover since Aug 2026; the
    # tile carries the hydrated actions instead
    assert "On your watchlist" not in body
    assert 'data-state-tmdb="8103"' in body
    assert "Shelf Owned" not in body
    assert "Shelf Dismissed" not in body
    assert "Western" in body
    assert "criterionchannel.com" in body

    # The heading's "See more…" opens the in-app departure inventory

    assert 'href="/leaving"' in body

    # The watchlisted film sorts first — it's the urgency case — and
    # the runtime filter applies like everywhere else

    assert body.index("Shelf Wanted") < body.index("Shelf Fresh")
    filtered = admin_client.get("/?minutes=100").get_data(as_text=True)
    assert "Shelf Fresh (1956)" in filtered
    assert "Shelf Wanted" not in filtered


def test_leaving_page_source_link_survives_pre_url_payloads(app, admin_client):
    """A stored set from before the source key existed still gets a
    source link on /leaving, reconstructed from the departure date."""

    import calendar

    from app.leaving_criterion import LEAVING_KEY

    subscribe_criterion(app)
    departs = date.today() + timedelta(days=10)
    app.redis.set(
        LEAVING_KEY,
        json.dumps(
            {
                "fetched_at": "2026-08-01 03:30",
                "departs": departs.isoformat(),
                "items": [shelf_item(8301, "Shelf Linkless")],
            }
        ),
    )

    body = admin_client.get("/leaving").get_data(as_text=True)
    expected = (
        "https://www.criterionchannel.com/leaving-"
        f"{calendar.month_name[departs.month].lower()}-{departs.day}"
    )
    assert f'<a href="{expected}"' in body


def test_leaving_page_lists_the_complete_inventory(app, admin_client):
    """/leaving shows every departing film — owned with the library
    badge, seen badged, watchlisted first, owned last — plus unmatched
    scraped rows and overview excerpts; empty state without a set."""

    from app import db
    from app.models import UserMovieReview, UserWatchlist
    from app.videos import star_rating_fields

    empty = admin_client.get("/leaving").get_data(as_text=True)
    assert "Nothing is currently scheduled to leave." in empty

    user_id = subscribe_criterion(app)
    with app.app_context():
        owned = make_movie("Shelf Owned", 1956, tmdb_id=8102)
        make_movie_file(owned, "Bluray-1080p")
        owned_id = owned.id
        wanted = make_movie("Shelf Wanted", 1956, tmdb_id=8103)
        db.session.add(UserWatchlist(user_id=user_id, movie_id=wanted.id))
        dismissed = make_movie("Shelf Dismissed", 1956, tmdb_id=8104)
        db.session.add(
            UserMovieReview(
                user_id=user_id, movie_id=dismissed.id, **star_rating_fields(3.0)
            )
        )
        db.session.commit()

    plant_shelf(
        app,
        [
            {
                **shelf_item(8101, "Shelf Fresh"),
                "overview": "A stranger rides into town.",
            },
            shelf_item(8102, "Shelf Owned"),
            shelf_item(8103, "Shelf Wanted"),
            shelf_item(8104, "Shelf Dismissed"),
            {
                "title": "Unmatched Gem",
                "director": "Jane Doe",
                "year": 1962,
                "tmdb_id": None,
            },
        ],
    )

    body = admin_client.get("/leaving").get_data(as_text=True)

    # Everything departing is listed — including owned and seen films
    # the home shelf hides — with the funnel vocabulary

    for title in ("Shelf Fresh", "Shelf Owned", "Shelf Wanted", "Shelf Dismissed"):
        assert f"{title} (1956)" in body
    # The funnel vocabulary moved into the popovers and the hydrated
    # widgets (Aug 2026): no badges on the tiles, actions instead
    assert 'title="In your Fitzflix library"' not in body
    assert 'text-bg-info me-1">Seen' not in body
    assert "On your watchlist" not in body
    assert f'data-state-movie="{owned_id}"' in body
    assert 'data-state-tmdb="8101"' in body
    # The synopsis lives in the poster popover now (#45d)
    assert "A stranger rides into town." not in body
    assert "data-card-url" in body

    # Watchlisted films lead, owned films trail; owned rows open their
    # movie page while the rest open the log page

    assert body.index("Shelf Wanted (1956)") < body.index("Shelf Fresh (1956)")
    assert body.index("Shelf Fresh (1956)") < body.index("Shelf Owned (1956)")
    assert f'href="/movie/{owned_id}"' in body
    assert 'href="/review/tmdb/8101"' in body

    # The unmatched film appears from the scrape alone

    assert "Also leaving" in body
    assert "Unmatched Gem (1962)" in body
    assert "Directed by Jane Doe" in body


def test_shelf_hides_for_nonsubscribers_and_after_departure(app, admin_client):
    plant_shelf(app, [shelf_item(8201, "Shelf Hidden")])

    # No Criterion subscription: no shelf, even with a stored set

    body = admin_client.get("/").get_data(as_text=True)
    assert 'id="leaving-shelf"' not in body

    # The standing nav link to the departure inventory stays either way

    assert 'href="/leaving"' in body

    # Subscribed, but the departure date has passed: also no shelf

    subscribe_criterion(app)
    plant_shelf(
        app,
        [shelf_item(8201, "Shelf Hidden")],
        departs=date.today() - timedelta(days=1),
    )
    body = admin_client.get("/").get_data(as_text=True)
    assert 'id="leaving-shelf"' not in body


def test_leaving_film_lights_its_criterion_badge(app, admin_client, monkeypatch):
    """Wherever a film's Criterion Channel availability badge renders,
    it lights up red with the departure date while the film is on the
    month's leaving set — the movie page's strip and the poster
    popover here; search results and filmography rows share the
    macro. Other Criterion films keep the plain badge, and the
    highlight clears once the departure date has passed."""

    from app import db
    from app.leaving_criterion import CRITERION_PROVIDER_ID
    from tests.test_streaming import plant_availability

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    subscribe_criterion(app)
    criterion = {
        "provider_id": CRITERION_PROVIDER_ID,
        "provider_name": "Criterion Channel",
        "logo_path": "/criterion.jpg",
    }
    with app.app_context():
        leaving = make_movie("Leaving Soon", 1956, tmdb_id=8401)
        make_movie_file(leaving, "Bluray-1080p")
        staying = make_movie("Staying Put", 1957, tmdb_id=8402)
        make_movie_file(staying, "Bluray-1080p")
        db.session.commit()
        leaving_id, staying_id = leaving.id, staying.id
    for tmdb_id in (8401, 8402):
        plant_availability(
            app,
            tmdb_id,
            {"link": None, "flatrate": [criterion], "ads": [], "rent": [], "buy": []},
        )
    departs = date.today() + timedelta(days=10)
    plant_shelf(app, [shelf_item(8401, "Leaving Soon")], departs=departs)
    label = departs.strftime("%B %-d")

    page = admin_client.get(f"/movie/{leaving_id}").get_data(as_text=True)
    assert f'title="Leaving Criterion Channel {label}"' in page
    assert f"Criterion Channel &middot; leaving {label}" in page
    assert "badge text-bg-danger" in page

    card = admin_client.get(f"/movie_card?movie_id={leaving_id}").get_data(as_text=True)
    assert f"Criterion Channel &middot; leaving {label}" in card

    # A Criterion film that isn't departing keeps the plain badge

    page = admin_client.get(f"/movie/{staying_id}").get_data(as_text=True)
    assert 'title="Streaming on Criterion Channel"' in page
    assert (
        "leaving"
        not in page.split("Streaming data by JustWatch")[0].split("In library")[1]
    )

    # Once the departure date passes, the highlight clears

    plant_shelf(
        app,
        [shelf_item(8401, "Leaving Soon")],
        departs=date.today() - timedelta(days=1),
    )
    page = admin_client.get(f"/movie/{leaving_id}").get_data(as_text=True)
    assert 'title="Streaming on Criterion Channel"' in page
    assert "badge text-bg-danger" not in page
