"""Test the leaving-Criterion shelf.

These tests cover the sanctioned scrape of the official leaving page,
the TMDB matching, and the monthly refresh task. They also cover the
taste-ranked landing-page shelf with its shopping-list urgency badge."""

import json

from datetime import date, timedelta

from tests.conftest import dvr_rebuild_jobs
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
    """A canned HTTP response."""

    def __init__(self, text="", status_code=200, payload=None):
        self.text = text
        self.status_code = status_code
        self._payload = payload or {}

    def raise_for_status(self):
        """Do nothing. The canned response is never an HTTP error."""

    def json(self):
        """Return the canned payload."""

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

    # One search occurs. The result is cached after that.

    assert len(calls) == 1
    assert calls[0]["primary_release_year"] == 1956


def test_match_tmdb_id_verifies_the_director(app, monkeypatch):
    """Test that the match checks the scraped director against the credits.

    For a generic title, the popular first result loses to the candidate
    whose credits name the scraped director. The example is "Here" (2023)
    by Bas Devos, not "Right Here, Right Now" (the shelf mistake of
    2026-08). A candidate with no credited director passes on an exact
    title and year. A director that TMDB cannot confirm leaves the film
    unmatched. The director-aware cache key is separate."""

    import app.leaving_criterion as leaving_criterion

    credits = {
        1083055: [{"name": "Jak Hutchcraft", "job": "Director"}],
        1463730: [{"name": "Bas Devos", "job": "Director"}],
        162480: [{"name": "Hal Hartley", "job": "Director"}],
        366144: [{"name": "Richard Sylvarnes", "job": "Director"}],
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
            if params.get("query") == "Regarding Soon":
                return FakeResponse(
                    payload={
                        "results": [
                            {
                                "id": 366144,
                                "title": "Regarding Soon",
                                "release_date": "2004-01-01",
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
        # The exact-title candidate comes first. Thus, only 1 credits call
        # occurs.
        assert leaving_criterion.match_tmdb_id("Here", 2023, "Bas Devos") == 1463730
        assert [c for c in calls if "/credits" in c] == [
            app.config["TMDB_API_URL"] + "/movie/1463730/credits"
        ]

        # "Kid" by Hal Hartley is not The Karate Kid: unmatched. The
        # verdict is cached. Thus, the next lookup costs nothing.
        calls.clear()
        assert leaving_criterion.match_tmdb_id("Kid", 1984, "Hal Hartley") is None
        assert leaving_criterion.match_tmdb_id("Kid", 1984, "Hal Hartley") is None
        assert (
            len([c for c in calls if "/search/movie" in c]) == 2
        )  # first with the year, then without it

        # TMDB credits "Regarding Soon" to Richard Sylvarnes. Criterion
        # says Hal Hartley. The 1 exact title-and-year result still
        # passes, although the directors disagree.
        assert (
            leaving_criterion.match_tmdb_id("Regarding Soon", 2004, "Hal Hartley")
            == 366144
        )

        # TMDB credits no director: the exact title and year are sufficient.
        assert leaving_criterion.match_tmdb_id("Opera No. 1", 1994, "Jane Doe") == 555

        # Criterion dates "Ambition" by Hal Hartley 1991. TMDB dates it
        # 1992. The year search finds nothing. The fallback without a
        # year reads a second page. Only the exact-title candidate there
        # costs a credits call. None of the 20 strangers on page 1 do.
        calls.clear()
        assert (
            leaving_criterion.match_tmdb_id("Ambition", 1991, "Hal Hartley") == 162480
        )
        assert [c for c in calls if "/credits" in c] == [
            app.config["TMDB_API_URL"] + "/movie/162480/credits"
        ]

        # Without a director, the exact-title candidate still wins over
        # the popular first result. The lookup keeps its own cache entry.
        # A title-only verdict never answers a director-aware query.
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
            # Only The Searchers matches. Love & Mercy tests the path
            # for unmatched films.
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
            # This is the scraped director, checked against the credits.
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
    from app import db

    with app.app_context():
        make_movie_file(make_movie("The Searchers", 1956, tmdb_id=3110), "DVD")
        db.session.commit()

    assert leaving_criterion.refresh_leaving_criterion() is True

    stored = json.loads(app.redis.get(leaving_criterion.LEAVING_KEY))
    assert stored["departs"]
    assert stored["source"].startswith("https://www.criterionchannel.com/leaving-")
    ids = [item["tmdb_id"] for item in stored["items"]]
    assert ids == [3110, None]
    assert stored["items"][0]["genres"] == [{"id": 37, "name": "Western"}]
    assert stored["items"][0]["overview"] == "An obsessive frontier search."

    # The unmatched film keeps its scraped facts. Thus, the /leaving page
    # can still list it.

    assert stored["items"][1]["title"] == "Love & Mercy"
    assert stored["items"][1]["director"] == "Bill Pohlad"
    assert stored["items"][1]["year"] == 2015

    # The new set reaches the DVR dial the same day, because Fitzflix
    # owns The Searchers. A set with no owned film changes no program.
    # On 2026-09-01, the 6:30 build ran before the set arrived at 11:38.
    # Thus, the Leaving Soon channel stayed dark until the next morning.
    assert len(dvr_rebuild_jobs(app)) == 1


def test_refresh_task_noops_while_stored_set_is_current(app, monkeypatch):
    # The daily schedule exists only to retry across the month boundary.
    # While the departure of the stored set is still ahead, the task
    # must not touch the network or the stored payload.

    import app.leaving_criterion as leaving_criterion

    planted = json.dumps({"departs": date.today().isoformat(), "items": []})
    app.redis.set(leaving_criterion.LEAVING_KEY, planted)

    def unexpected_get(*args, **kwargs):
        raise AssertionError("a current stored set must not be re-scraped")

    monkeypatch.setattr(leaving_criterion.requests, "get", unexpected_get)

    assert leaving_criterion.refresh_leaving_criterion() is True
    assert app.redis.get(leaving_criterion.LEAVING_KEY).decode() == planted
    assert dvr_rebuild_jobs(app) == []


def test_refresh_task_retries_once_stored_set_has_departed(app, monkeypatch):
    import app.leaving_criterion as leaving_criterion

    app.redis.set(
        leaving_criterion.LEAVING_KEY,
        json.dumps(
            {"departs": (date.today() - timedelta(days=1)).isoformat(), "items": []}
        ),
    )

    calls = []

    def fake_requests_get(url, params=None, timeout=None):
        calls.append(url)
        return FakeResponse(text="<html>empty</html>")

    monkeypatch.setattr(leaving_criterion.requests, "get", fake_requests_get)

    assert leaving_criterion.refresh_leaving_criterion() is True
    assert calls  # the set departed, so the scrape ran


def shelf_item(tmdb_id, title, runtime=100, genre=(37, "Western")):
    """Return a stored leaving-set item, the trimmed enriched payload."""

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
    """Subscribe the admin user to the Criterion Channel.

    Also plant a taste profile that leans to Western."""

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


def test_shelf_ranks_and_excludes(app, admin_client):
    from app import db
    from app.models import UserMovieReview, UserWatchlist
    from app.videos import star_rating_fields

    user_id = subscribe_criterion(app)

    with app.app_context():
        owned = make_movie("Shelf Owned", 1956, tmdb_id=8102)
        make_movie_file(owned, "Bluray-1080p")

        # Watchlisted (and also logged before): this film belongs to the
        # watchlist shelf of the landing page now, never to this shelf.
        # The leaving match makes it watchable tonight, even with no
        # availability cached. Thus, it appears on the watchlist shelf.

        wanted = make_movie("Shelf Wanted", 1956, tmdb_id=8103)
        db.session.add(
            UserMovieReview(
                user_id=user_id, movie_id=wanted.id, **star_rating_fields(4.0)
            )
        )
        db.session.add(UserWatchlist(user_id=user_id, movie_id=wanted.id))

        # Logged but not watchlisted: seen, not wanted. It drops fully.

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
            # An unmatched film: the shelf must skip it without an error.
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
    leaving_section = body.split('id="leaving-shelf"')[1].split("<h4 ")[0]
    assert "Shelf Fresh (1956)" in leaving_section
    assert "Shelf Wanted" not in leaving_section
    assert "Shelf Owned" not in body
    assert "Shelf Dismissed" not in body
    assert "Western" in body
    assert "criterionchannel.com" in body

    # The watchlisted departure appears on the watchlist shelf instead.
    # The leaving store is first-party proof that the film is watchable
    # tonight. Thus, the synthesized Criterion match makes the film lead
    # the landing page, even with no TMDB availability cached.

    watchlist_section = body.split('id="watchlist-shelf"')[1].split("<h4 ")[0]
    assert "Shelf Wanted (1956)" in watchlist_section

    # The "See more…" link of the heading opens the in-app departure
    # inventory.

    assert 'href="/leaving"' in body

    # The runtime filter applies here as on every other page.

    filtered = admin_client.get("/?minutes=100").get_data(as_text=True)
    assert "Shelf Fresh (1956)" in filtered


def test_watchlist_shelf_leads_with_departures(app, admin_client):
    """Test that a watchlisted departure leads the top watchlist shelf.

    A watchlisted film that leaves the Criterion Channel is the most
    urgent card on the page. It heads the top watchlist shelf, ahead of
    the calm watchlisted films. Its card reason is the departure. The
    leaving shelf stays only discovery."""

    from app import db
    from app.leaving_criterion import CRITERION_PROVIDER_ID
    from app.models import UserWatchlist
    from app.streaming import AVAILABILITY_KEY

    user_id = subscribe_criterion(app)
    with app.app_context():
        urgent = make_movie("Urgent Departure", 1956, tmdb_id=8301)
        db.session.add(UserWatchlist(user_id=user_id, movie_id=urgent.id))
        calm = make_movie("Calm Wanted", 1956, tmdb_id=8302)
        db.session.add(UserWatchlist(user_id=user_id, movie_id=calm.id))
        db.session.commit()

    criterion = {
        "provider_id": CRITERION_PROVIDER_ID,
        "provider_name": "The Criterion Channel",
        "logo_path": "/criterion.jpg",
    }
    for tmdb_id in (8301, 8302):
        app.redis.set(
            AVAILABILITY_KEY.format(tmdb_id=tmdb_id),
            json.dumps(
                {
                    "link": None,
                    "flatrate": [criterion],
                    "ads": [],
                    "rent": [],
                    "buy": [],
                }
            ),
        )
    plant_shelf(app, [shelf_item(8301, "Urgent Departure")])

    body = admin_client.get("/").get_data(as_text=True)
    assert "From your watchlist" in body
    assert body.index("Urgent Departure (1956)") < body.index("Calm Wanted (1956)")
    assert "Leaving the Criterion Channel" in body

    # Both films are watchlisted. Thus, the leaving shelf has nothing to
    # show, and its heading does not render.

    assert 'id="leaving-shelf"' not in body


def test_page_never_repeats_a_film_across_shelves(app, admin_client):
    """Test that a film shows only 1 time across the shelves.

    A film that is on the streaming rail and in the leaving set shows
    exactly 1 time on the page. The shelves claim their films from 1
    shared no-repeat pool, in an order that is stable for the day."""

    from app.streaming_rail import RAIL_KEY

    user_id = subscribe_criterion(app)
    plant_shelf(
        app,
        [shelf_item(8401, "Overlap Film"), shelf_item(8402, "Departure Only")],
    )

    def rail_item(tmdb_id, title, score):
        """A minimal stored rail entry."""

        return {
            "tmdb_id": tmdb_id,
            "title": title,
            "year": "1956",
            "poster_path": None,
            "runtime": 95,
            "providers": [],
            "because": [],
            "score": score,
        }

    app.redis.set(
        RAIL_KEY.format(user_id=user_id),
        json.dumps(
            {
                "computed_at": "2026-08-12 02:15",
                "items": [
                    rail_item(8401, "Overlap Film", 2.0),
                    rail_item(8403, "Rail Only", 1.0),
                ],
            }
        ),
    )

    body = admin_client.get("/").get_data(as_text=True)
    assert body.count("Overlap Film (1956)") == 1
    assert "Departure Only (1956)" in body
    assert "Rail Only (1956)" in body


def test_runtime_filter_says_when_the_shelf_empties(app, admin_client):
    """Test that the shelf explains itself when the filter empties it.

    A filter that removes every departing film keeps the shelf heading,
    with its date and inventory link. The shelf says why the grid is
    empty (GitHub #198)."""

    subscribe_criterion(app)
    plant_shelf(app, [shelf_item(8201, "Shelf Nothing Fits", runtime=200)])

    body = admin_client.get("/?minutes=10").get_data(as_text=True)
    assert 'id="leaving-shelf"' in body
    assert 'href="/leaving"' in body
    assert "Nothing leaving fits in 10 minutes" in body
    assert "Shelf Nothing Fits" not in body

    body = admin_client.get("/").get_data(as_text=True)
    assert "Shelf Nothing Fits (1956)" in body
    assert "Nothing leaving fits" not in body


def test_leaving_page_source_link_survives_pre_url_payloads(app, admin_client):
    """Test that /leaving links a stored set that has no source key.

    A stored set from before the source key existed still gets a source
    link on /leaving. Fitzflix rebuilds the link from the departure
    date."""

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
    """Test that /leaving lists the complete departure inventory.

    /leaving shows every departing film. Owned films show the library
    badge. Seen films show a badge. Watchlisted films come first, and
    owned films come last. The page also shows the unmatched scraped
    rows and the overview excerpts. Without a set, it shows the empty
    state."""

    from app import db
    from app.models import UserMovieReview, UserWatchlist
    from app.videos import star_rating_fields

    empty = admin_client.get("/leaving").get_data(as_text=True)
    assert "No film is scheduled to leave now." in empty

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

    # Every departing film is listed with the funnel vocabulary. This
    # includes the owned and seen films that the home shelf hides.

    for title in ("Shelf Fresh", "Shelf Owned", "Shelf Wanted", "Shelf Dismissed"):
        assert f"{title} (1956)" in body
    # The funnel vocabulary moved into the popovers and the hydrated
    # widgets (2026-08). The tiles show no badges. They show actions.
    assert 'title="In your Fitzflix library"' not in body
    assert 'text-bg-info me-1">Seen' not in body
    assert "On your watchlist" not in body
    assert f'data-state-movie="{owned_id}"' in body
    assert 'data-state-tmdb="8101"' in body
    # The synopsis is in the poster popover now (#45d).
    assert "A stranger rides into town." not in body
    assert "data-card-url" in body

    # Watchlisted films lead. Owned films come last. An owned row opens
    # its movie page. The other rows open the log page.

    assert body.index("Shelf Wanted (1956)") < body.index("Shelf Fresh (1956)")
    assert body.index("Shelf Fresh (1956)") < body.index("Shelf Owned (1956)")
    assert f'href="/movie/{owned_id}"' in body
    assert 'href="/review/tmdb/8101"' in body

    # The unmatched film appears from the scrape alone.

    assert "Also leaving" in body
    assert "Unmatched Gem (1962)" in body
    assert "Directed by Jane Doe" in body


def test_shelf_hides_for_nonsubscribers_and_after_departure(app, admin_client):
    plant_shelf(app, [shelf_item(8201, "Shelf Hidden")])

    # No Criterion subscription: no shelf, even with a stored set.

    body = admin_client.get("/").get_data(as_text=True)
    assert 'id="leaving-shelf"' not in body

    # The permanent nav link to the departure inventory stays in both
    # cases.

    assert 'href="/leaving"' in body

    # Subscribed, but the departure date has passed: also no shelf.

    subscribe_criterion(app)
    plant_shelf(
        app,
        [shelf_item(8201, "Shelf Hidden")],
        departs=date.today() - timedelta(days=1),
    )
    body = admin_client.get("/").get_data(as_text=True)
    assert 'id="leaving-shelf"' not in body


def test_leaving_film_lights_its_criterion_badge(app, admin_client, monkeypatch):
    """Test that a departing film lights its Criterion Channel badge.

    The Criterion Channel availability badge of a film can render in
    many places. While the film is on the leaving set of the month, the
    badge shows red with the departure date in each of those places.
    This test covers the strip of the movie
    page and the poster popover. The search results and the filmography
    rows share the macro. Other Criterion films keep the plain badge.
    The highlight clears after the departure date has passed."""

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
        # The departing film is record-only. An OWNED film never shows
        # the warning (requested by Glenn, 2026-08-27). See owned_leaving
        # below.
        leaving = make_movie("Leaving Soon", 1956, tmdb_id=8401)
        staying = make_movie("Staying Put", 1957, tmdb_id=8402)
        make_movie_file(staying, "Bluray-1080p")
        owned_leaving = make_movie("Shelf Safe", 1958, tmdb_id=8403)
        make_movie_file(owned_leaving, "Bluray-1080p")
        db.session.commit()
        leaving_id, staying_id = leaving.id, staying.id
        owned_leaving_id = owned_leaving.id
    for tmdb_id in (8401, 8402, 8403):
        plant_availability(
            app,
            tmdb_id,
            {"link": None, "flatrate": [criterion], "ads": [], "rent": [], "buy": []},
        )
    departs = date.today() + timedelta(days=10)
    plant_shelf(
        app,
        [shelf_item(8401, "Leaving Soon"), shelf_item(8403, "Shelf Safe")],
        departs=departs,
    )
    label = departs.strftime("%B %-d")

    page = admin_client.get(f"/movie/{leaving_id}").get_data(as_text=True)
    assert f'title="Leaving Criterion Channel {label}"' in page
    assert f"Criterion Channel &middot; leaving {label}" in page
    assert "badge text-bg-danger" in page

    card = admin_client.get(f"/movie_card?movie_id={leaving_id}").get_data(as_text=True)
    assert f"Criterion Channel &middot; leaving {label}" in card

    # A Criterion film that is not departing keeps the plain badge.

    page = admin_client.get(f"/movie/{staying_id}").get_data(as_text=True)
    assert 'title="Streaming on Criterion Channel"' in page
    assert (
        "leaving"
        not in page.split("Streaming data by JustWatch")[0].split("In library")[1]
    )

    # An OWNED film on the leaving set also keeps the plain badge. The
    # copy on the shelf stays (requested by Glenn, 2026-08-27).

    page = admin_client.get(f"/movie/{owned_leaving_id}").get_data(as_text=True)
    assert 'title="Streaming on Criterion Channel"' in page
    assert (
        "leaving"
        not in page.split("Streaming data by JustWatch")[0].split("In library")[1]
    )

    # After the departure date passes, the highlight clears.

    plant_shelf(
        app,
        [shelf_item(8401, "Leaving Soon")],
        departs=date.today() - timedelta(days=1),
    )
    page = admin_client.get(f"/movie/{leaving_id}").get_data(as_text=True)
    assert 'title="Streaming on Criterion Channel"' in page
    assert "badge text-bg-danger" not in page
