"""The newly-added feeds (#246): the generic collection scraper, the
first-seen snapshot diff, the per-provider discovery shelves, and the
"added" availability badge."""

import json

from datetime import date, timedelta

from tests.factories import make_movie, make_movie_file
from tests.test_leaving_criterion import (
    LEAVING_HTML,
    FakeResponse,
    shelf_item,
    subscribe_criterion,
)


def new_item(tmdb_id, title, first_seen=None, runtime=100, scraped_title=None):
    """A stored newly-added item: the trimmed enriched payload plus
    the diff bookkeeping."""

    return {
        **shelf_item(tmdb_id, title, runtime=runtime),
        "first_seen": first_seen,
        "scraped_title": scraped_title or title,
        "scraped_year": 1956,
    }


def plant_feed(app, items, provider_id=None):
    """Store a newly-added feed for a provider (Criterion unless
    said otherwise)."""

    from app.leaving_criterion import CRITERION_PROVIDER_ID
    from app.newly_added import NEWLY_ADDED_KEY

    app.redis.set(
        NEWLY_ADDED_KEY.format(provider_id=provider_id or CRITERION_PROVIDER_ID),
        json.dumps(
            {
                "fetched_at": "2026-08-27 05:00",
                "source": "https://www.criterionchannel.com/newly-added",
                "items": items,
            }
        ),
    )


def test_fetch_collection_films_paginates_and_dedupes(app, monkeypatch):
    import app.leaving_criterion as leaving_criterion

    calls = []

    def fake_requests_get(url, params=None, timeout=None):
        calls.append(params.get("page"))
        if params.get("page") == 1:
            return FakeResponse(text=LEAVING_HTML)
        if params.get("page") == 2:
            # The same films again: the dedup case
            return FakeResponse(text=LEAVING_HTML)
        return FakeResponse(text="<html>empty</html>")

    monkeypatch.setattr(leaving_criterion.requests, "get", fake_requests_get)

    with app.app_context():
        from app.leaving_criterion import fetch_collection_films

        films = fetch_collection_films("https://www.criterionchannel.com/newly-added")

    assert [film["title"] for film in films] == ["The Searchers", "Love & Mercy"]
    assert calls == [1, 2, 3]


def test_refresh_plants_first_then_stamps_and_prunes(app, monkeypatch):
    """The diff semantics: the first run only plants (null first_seen),
    a later run stamps today's date on films new to the page and keeps
    prior films' stamps and payloads, and films gone from the page
    drop out. Matched films are carried over without re-matching."""

    import app.newly_added as newly_added

    scraped = [{"title": "The Searchers", "director": "John Ford", "year": 1956}]
    matches = {"The Searchers": 3110, "Love & Mercy": 26302}
    match_calls = []

    def fake_match(title, year, director=None):
        match_calls.append(title)
        return matches.get(title)

    def fake_enriched(tmdb_id):
        return shelf_item(tmdb_id, f"Film {tmdb_id}")

    monkeypatch.setattr(newly_added, "fetch_collection_films", lambda url: scraped)
    monkeypatch.setattr(newly_added, "match_tmdb_id", fake_match)
    monkeypatch.setattr(newly_added, "enriched_movie", fake_enriched)

    from app.leaving_criterion import CRITERION_PROVIDER_ID
    from app.newly_added import NEWLY_ADDED_KEY, refresh_newly_added

    key = NEWLY_ADDED_KEY.format(provider_id=CRITERION_PROVIDER_ID)

    # First run: plants only

    assert refresh_newly_added() is True
    stored = json.loads(app.redis.get(key))
    assert [item["first_seen"] for item in stored["items"]] == [None]
    assert stored["items"][0]["scraped_title"] == "The Searchers"

    # Second run, one new film and one unmatched newcomer: the prior
    # film keeps its null stamp and isn't re-matched, the arrivals
    # get today's date, the unmatched one keeps its scraped facts

    scraped.append({"title": "Love & Mercy", "director": "Bill Pohlad", "year": 1956})
    scraped.append({"title": "Obscurity", "director": "Jane Doe", "year": 1956})
    match_calls.clear()
    refresh_newly_added()
    stored = json.loads(app.redis.get(key))
    today = date.today().isoformat()
    by_title = {item["scraped_title"]: item for item in stored["items"]}
    assert by_title["The Searchers"]["first_seen"] is None
    assert by_title["Love & Mercy"]["first_seen"] == today
    assert by_title["Obscurity"]["first_seen"] == today
    assert by_title["Obscurity"]["tmdb_id"] is None
    assert by_title["Obscurity"]["director"] == "Jane Doe"
    assert match_calls == ["Love & Mercy", "Obscurity"]

    # Third run with the planted film gone: it's pruned, the kept
    # film's stamp survives

    scraped[:] = scraped[1:]
    refresh_newly_added()
    stored = json.loads(app.redis.get(key))
    assert [item["scraped_title"] for item in stored["items"]] == [
        "Love & Mercy",
        "Obscurity",
    ]
    assert stored["items"][0]["first_seen"] == today

    # A scrape outage keeps the previous snapshot untouched

    monkeypatch.setattr(newly_added, "fetch_collection_films", lambda url: [])
    refresh_newly_added()
    assert json.loads(app.redis.get(key))["items"] == stored["items"]


def test_shelf_surfaces_recent_arrivals_and_excludes(app, admin_client):
    from app import db
    from app.models import UserMovieReview, UserWatchlist
    from app.videos import star_rating_fields

    user_id = subscribe_criterion(app)
    today = date.today().isoformat()
    stale = (date.today() - timedelta(days=40)).isoformat()

    with app.app_context():
        owned = make_movie("Arrival Owned", 1956, tmdb_id=9102)
        make_movie_file(owned, "Bluray-1080p")

        wanted = make_movie("Arrival Wanted", 1956, tmdb_id=9103)
        db.session.add(UserWatchlist(user_id=user_id, movie_id=wanted.id))

        dismissed = make_movie("Arrival Dismissed", 1956, tmdb_id=9104)
        db.session.add(
            UserMovieReview(
                user_id=user_id, movie_id=dismissed.id, **star_rating_fields(3.0)
            )
        )
        db.session.commit()

    plant_feed(
        app,
        [
            new_item(9101, "Arrival Fresh", first_seen=today, runtime=95),
            new_item(9102, "Arrival Owned", first_seen=today),
            new_item(9103, "Arrival Wanted", first_seen=today, runtime=200),
            new_item(9104, "Arrival Dismissed", first_seen=today),
            # Planted on the first run: on the page, but not news
            new_item(9105, "Arrival Planted", first_seen=None),
            # Stamped before the window opened: no longer news
            new_item(9106, "Arrival Stale", first_seen=stale),
            # An unmatched film: skipped quietly
            {
                "title": "Arrival Unmatched",
                "director": "Jane Doe",
                "year": 1962,
                "tmdb_id": None,
                "first_seen": today,
                "scraped_title": "Arrival Unmatched",
                "scraped_year": 1962,
            },
        ],
    )

    body = admin_client.get("/").get_data(as_text=True)
    assert 'id="newly-added-shelf-258"' in body
    assert "Newly added to the Criterion Channel" in body
    assert "Arrival Fresh (1956)" in body
    assert "Arrival Wanted (1956)" in body
    assert "Arrival Owned" not in body
    assert "Arrival Dismissed" not in body
    assert "Arrival Planted" not in body
    assert "Arrival Stale" not in body
    assert "Arrival Unmatched" not in body

    # "See more…" opens the in-app arrival inventory, at this
    # provider's section

    assert 'href="/newly-added#newly-added-258"' in body

    # The watchlisted arrival sorts first, and the runtime filter
    # applies like everywhere else — including the emptied-not-hidden
    # message (#198)

    assert body.index("Arrival Wanted") < body.index("Arrival Fresh")
    filtered = admin_client.get("/?minutes=100").get_data(as_text=True)
    assert "Arrival Fresh (1956)" in filtered
    assert "Arrival Wanted" not in filtered
    emptied = admin_client.get("/?minutes=10").get_data(as_text=True)
    assert 'id="newly-added-shelf-258"' in emptied
    assert "Nothing newly added fits in 10 minutes" in emptied

    # The shelf suppresses the green corner fold — everything here is
    # newly added by definition

    assert "data-no-new-fold" in body


def test_shelf_hides_without_subscription_or_arrivals(app, admin_client):
    today = date.today().isoformat()

    # No Criterion subscription: no shelf, even with a stored feed

    plant_feed(app, [new_item(9201, "Arrival Hidden", first_seen=today)])
    body = admin_client.get("/").get_data(as_text=True)
    assert 'id="newly-added-shelf-258"' not in body

    # Subscribed, but nothing inside the window: also no shelf

    subscribe_criterion(app)
    plant_feed(app, [new_item(9201, "Arrival Hidden", first_seen=None)])
    body = admin_client.get("/").get_data(as_text=True)
    assert 'id="newly-added-shelf-258"' not in body


def test_new_arrival_lights_its_availability_badge(app, admin_client, monkeypatch):
    """Wherever a film's availability badge renders, a recent arrival
    on that provider's newly-added feed turns it green with the date;
    the leaving badge outranks it on a film that is somehow both."""

    from app import db
    from app.leaving_criterion import CRITERION_PROVIDER_ID
    from tests.test_leaving_criterion import plant_shelf
    from tests.test_streaming import plant_availability

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    subscribe_criterion(app)
    criterion = {
        "provider_id": CRITERION_PROVIDER_ID,
        "provider_name": "Criterion Channel",
        "logo_path": "/criterion.jpg",
    }
    with app.app_context():
        arrived = make_movie("Just Arrived", 1956, tmdb_id=9401)
        make_movie_file(arrived, "Bluray-1080p")
        both = make_movie("Blink And Miss", 1957, tmdb_id=9402)
        make_movie_file(both, "Bluray-1080p")
        db.session.commit()
        arrived_id, both_id = arrived.id, both.id
    for tmdb_id in (9401, 9402):
        plant_availability(
            app,
            tmdb_id,
            {"link": None, "flatrate": [criterion], "ads": [], "rent": [], "buy": []},
        )
    first_seen = date.today() - timedelta(days=3)
    plant_feed(
        app,
        [
            new_item(9401, "Just Arrived", first_seen=first_seen.isoformat()),
            new_item(9402, "Blink And Miss", first_seen=first_seen.isoformat()),
        ],
    )
    departs = date.today() + timedelta(days=10)
    plant_shelf(app, [shelf_item(9402, "Blink And Miss")], departs=departs)
    label = first_seen.strftime("%B %-d")

    page = admin_client.get(f"/movie/{arrived_id}").get_data(as_text=True)
    assert f'title="Added to Criterion Channel {label}"' in page
    assert f"Criterion Channel &middot; added {label}" in page
    assert "badge text-bg-success" in page

    card = admin_client.get(f"/movie_card?movie_id={arrived_id}").get_data(as_text=True)
    assert f"Criterion Channel &middot; added {label}" in card

    # Departure urgency wins on the film carrying both marks

    page = admin_client.get(f"/movie/{both_id}").get_data(as_text=True)
    leaving_label = departs.strftime("%B %-d")
    assert f"Criterion Channel &middot; leaving {leaving_label}" in page
    assert f"Criterion Channel &middot; added {label}" not in page


def test_newly_added_page_lists_the_complete_inventory(app, admin_client):
    """Unlike the home shelf, /newly-added excludes nothing: owned and
    seen films stay listed, unmatched films trail as plain rows, and
    the section links to the provider's own page. No subscription
    required — like /leaving, the page renders from the stored feeds
    alone."""

    from app import db
    from app.models import UserMovieReview, UserWatchlist
    from app.videos import star_rating_fields

    # A taste profile but no subscription: the page must still render

    user_id = subscribe_criterion(app)
    from app.models import UserStreamingProvider

    with app.app_context():
        UserStreamingProvider.query.delete()
        db.session.commit()

    today = date.today().isoformat()
    stale = (date.today() - timedelta(days=40)).isoformat()

    with app.app_context():
        owned = make_movie("Inventory Owned", 1956, tmdb_id=9502)
        make_movie_file(owned, "Bluray-1080p")

        seen = make_movie("Inventory Seen", 1956, tmdb_id=9503)
        db.session.add(
            UserMovieReview(
                user_id=user_id, movie_id=seen.id, **star_rating_fields(3.0)
            )
        )

        wanted = make_movie("Inventory Wanted", 1956, tmdb_id=9504)
        db.session.add(UserWatchlist(user_id=user_id, movie_id=wanted.id))
        db.session.commit()

    plant_feed(
        app,
        [
            new_item(9501, "Inventory Fresh", first_seen=today),
            new_item(9502, "Inventory Owned", first_seen=today),
            new_item(9503, "Inventory Seen", first_seen=today),
            new_item(9504, "Inventory Wanted", first_seen=today),
            # Planted on the first run and aged out: neither is news
            new_item(9505, "Inventory Planted", first_seen=None),
            new_item(9506, "Inventory Stale", first_seen=stale),
            {
                "title": "Inventory Unmatched",
                "director": "Jane Doe",
                "year": 1962,
                "tmdb_id": None,
                "first_seen": today,
                "scraped_title": "Inventory Unmatched",
                "scraped_year": 1962,
            },
        ],
    )

    body = admin_client.get("/newly-added").get_data(as_text=True)
    assert 'id="newly-added-258"' in body
    assert "Newly added to the Criterion Channel" in body
    for title in (
        "Inventory Fresh",
        "Inventory Owned",
        "Inventory Seen",
        "Inventory Wanted",
    ):
        assert f"{title} (1956)" in body
    assert "Inventory Planted" not in body
    assert "Inventory Stale" not in body

    # The unmatched film trails as a plain row, and the section links
    # to the provider's own page

    assert "Also new" in body
    assert "Inventory Unmatched (1962)" in body
    assert "Directed by Jane Doe" in body
    assert 'href="https://www.criterionchannel.com/newly-added"' in body

    # Watchlisted films lead, owned films trail

    assert body.index("Inventory Wanted") < body.index("Inventory Fresh")
    assert body.index("Inventory Fresh") < body.index("Inventory Owned")

    # The standing nav link reaches the page from anywhere, and the
    # page suppresses the green corner fold like the shelf does

    assert 'href="/newly-added"' in body
    assert "data-no-new-fold" in body


def test_new_arrival_feeds_the_green_poster_fold(app, admin_client):
    """/movie_states answers fold_new with the feed's label for a
    subscribed provider's recent arrival — movie-keyed and tmdb-keyed
    alike; the alert diff's own recently-available record outranks
    it, and a non-subscriber gets no fold at all."""

    import json as jsonlib

    from app import db
    from app.models import User, UserStreamingProvider

    subscribe_criterion(app)
    first_seen = date.today() - timedelta(days=3)
    with app.app_context():
        movie = make_movie("Folded Arrival", 1956, tmdb_id=9701)
        make_movie_file(movie, "Bluray-1080p")
        db.session.commit()
        movie_id = movie.id
        user_id = User.query.filter_by(admin=True).first().id
    plant_feed(
        app,
        [
            new_item(9701, "Folded Arrival", first_seen=first_seen.isoformat()),
            new_item(9702, "Folded Stranger", first_seen=first_seen.isoformat()),
        ],
    )
    label = f"Added to the Criterion Channel {first_seen.strftime('%B %-d')}"

    payload = admin_client.get(
        f"/movie_states?movie_ids={movie_id}&tmdb_ids=9702"
    ).get_json()
    assert payload["movies"][str(movie_id)]["fold_new"] == label
    assert payload["tmdb"]["9702"]["fold_new"] == label

    # The alert diff's own record wins over the feed label

    app.redis.hset(
        f"fitzflix:availability:recent:{user_id}",
        str(movie_id),
        jsonlib.dumps(
            {"date": date.today().isoformat(), "label": "New on Criterion Channel"}
        ),
    )
    payload = admin_client.get(f"/movie_states?movie_ids={movie_id}").get_json()
    assert payload["movies"][str(movie_id)]["fold_new"] == "New on Criterion Channel"

    # No subscription, no fold

    app.redis.delete(f"fitzflix:availability:recent:{user_id}")
    with app.app_context():
        UserStreamingProvider.query.delete()
        db.session.commit()
    payload = admin_client.get(
        f"/movie_states?movie_ids={movie_id}&tmdb_ids=9702"
    ).get_json()
    assert payload["movies"][str(movie_id)]["fold_new"] is None
    assert payload["tmdb"]["9702"]["fold_new"] is None


def test_newly_added_page_says_when_nothing_is_new(app, admin_client):
    body = admin_client.get("/newly-added").get_data(as_text=True)
    assert "Nothing new right now." in body

    # A stored feed whose arrivals all predate tracking or the window
    # reads the same as no feed

    plant_feed(app, [new_item(9601, "Inventory Quiet", first_seen=None)])
    body = admin_client.get("/newly-added").get_data(as_text=True)
    assert "Nothing new right now." in body
    assert "Inventory Quiet" not in body
