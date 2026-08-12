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


def test_refresh_task_scrapes_matches_and_stores(app, monkeypatch):
    import app.leaving_criterion as leaving_criterion

    def fake_requests_get(url, params=None, timeout=None):
        if params and params.get("page", 1) == 1:
            return FakeResponse(text=LEAVING_HTML)
        return FakeResponse(text="<html>empty</html>")

    def fake_tmdb_get(url, params=None, **kwargs):
        if "/search/movie" in url:
            if params.get("query") == "The Searchers":
                return FakeResponse(payload={"results": [{"id": 3110}]})
            return FakeResponse(payload={"results": [{"id": 3111}]})
        for tmdb_id, title, year in (
            (3110, "The Searchers", "1956-05-16"),
            (3111, "Love & Mercy", "2015-06-05"),
        ):
            if url.endswith(f"/movie/{tmdb_id}"):
                return FakeResponse(
                    payload={
                        "id": tmdb_id,
                        "title": title,
                        "release_date": year,
                        "poster_path": f"/{tmdb_id}.jpg",
                        "runtime": 119,
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
    assert ids == [3110, 3111]
    assert stored["items"][0]["genres"] == [{"id": 37, "name": "Western"}]


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
        ],
    )

    body = admin_client.get("/").get_data(as_text=True)
    assert "Leaving the Criterion Channel" in body
    assert "Shelf Fresh (1956)" in body
    assert "Shelf Wanted (1956)" in body
    assert "On your watchlist" in body
    assert "Shelf Owned" not in body
    assert "Shelf Dismissed" not in body
    assert "Western" in body
    assert "criterionchannel.com" in body

    # The heading links to the month's own leaving page

    assert (
        '<a href="https://www.criterionchannel.com/leaving-august-31"'
        ' target="_blank" rel="noreferrer" class="text-body text-decoration-none">'
        "Leaving the Criterion Channel" in body
    )

    # The watchlisted film sorts first — it's the urgency case — and
    # the runtime filter applies like everywhere else

    assert body.index("Shelf Wanted") < body.index("Shelf Fresh")
    filtered = admin_client.get("/?minutes=100").get_data(as_text=True)
    assert "Shelf Fresh (1956)" in filtered
    assert "Shelf Wanted" not in filtered


def test_shelf_heading_link_survives_pre_url_payloads(app, admin_client):
    """A stored set from before the source key existed still gets a
    heading link, reconstructed from the departure date."""

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

    body = admin_client.get("/").get_data(as_text=True)
    expected = (
        "https://www.criterionchannel.com/leaving-"
        f"{calendar.month_name[departs.month].lower()}-{departs.day}"
    )
    assert f'<a href="{expected}"' in body


def test_shelf_hides_for_nonsubscribers_and_after_departure(app, admin_client):
    plant_shelf(app, [shelf_item(8201, "Shelf Hidden")])

    # No Criterion subscription: no shelf, even with a stored set

    body = admin_client.get("/").get_data(as_text=True)
    assert "Leaving the Criterion Channel" not in body

    # Subscribed, but the departure date has passed: also no shelf

    subscribe_criterion(app)
    plant_shelf(
        app,
        [shelf_item(8201, "Shelf Hidden")],
        departs=date.today() - timedelta(days=1),
    )
    body = admin_client.get("/").get_data(as_text=True)
    assert "Leaving the Criterion Channel" not in body
