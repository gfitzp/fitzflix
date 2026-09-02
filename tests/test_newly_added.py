"""Test the newly-added feeds (#246).

The tests cover the generic collection scraper, the first-seen snapshot
diff, the per-provider discovery shelves, and the "added" availability
badge."""

import json

from datetime import date, timedelta

from tests.conftest import dvr_rebuild_jobs
from tests.factories import make_movie, make_movie_file
from tests.test_leaving_criterion import (
    LEAVING_HTML,
    FakeResponse,
    shelf_item,
    subscribe_criterion,
)


def new_item(tmdb_id, title, first_seen=None, runtime=100, scraped_title=None):
    """Return a stored newly-added item.

    The item is the trimmed enriched payload plus the diff bookkeeping."""

    return {
        **shelf_item(tmdb_id, title, runtime=runtime),
        "first_seen": first_seen,
        "scraped_title": scraped_title or title,
        "scraped_year": 1956,
    }


def plant_feed(app, items, provider_id=None):
    """Store a newly-added feed for a provider (default Criterion)."""

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
            # The same films again. This is the duplicate case
            return FakeResponse(text=LEAVING_HTML)
        return FakeResponse(text="<html>empty</html>")

    monkeypatch.setattr(leaving_criterion.requests, "get", fake_requests_get)

    with app.app_context():
        from app.leaving_criterion import fetch_collection_films

        films = fetch_collection_films("https://www.criterionchannel.com/newly-added")

    assert [film["title"] for film in films] == ["The Searchers", "Love & Mercy"]
    assert calls == [1, 2, 3]


def test_refresh_plants_first_then_stamps_and_prunes(app, monkeypatch):
    """Test the diff semantics of the refresh.

    The first run only plants the films (null first_seen). A later run
    stamps the date of today on the films that are new to the page. It
    keeps the stamps and the payloads of the earlier films. The films
    that left the page drop out. The run carries the matched films over
    and does not match them again."""

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

    # The first run only plants the films

    assert refresh_newly_added() is True
    stored = json.loads(app.redis.get(key))
    assert [item["first_seen"] for item in stored["items"]] == [None]
    assert stored["items"][0]["scraped_title"] == "The Searchers"
    # Planting stamps nothing new, so the DVR dial has nothing to learn
    assert dvr_rebuild_jobs(app) == []

    # The second run has 1 new film and 1 unmatched newcomer. The
    # earlier film keeps its null stamp, and the run does not match it
    # again. The new films get the date of today. The unmatched film
    # keeps its scraped facts

    scraped.append({"title": "Love & Mercy", "director": "Bill Pohlad", "year": 1956})
    scraped.append({"title": "Obscurity", "director": "Jane Doe", "year": 1956})
    match_calls.clear()
    from app import db

    with app.app_context():
        make_movie_file(make_movie("Love & Mercy", 2015, tmdb_id=26302), "DVD")
        db.session.commit()
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
    # An OWNED fresh arrival joins the Criterion channel via the
    # synthesized match, so the dial is rebuilt the same day
    assert len(dvr_rebuild_jobs(app)) == 1

    # In the third run, the planted film is gone. The run removes it.
    # The stamp of the kept film survives

    scraped[:] = scraped[1:]
    refresh_newly_added()
    stored = json.loads(app.redis.get(key))
    assert [item["scraped_title"] for item in stored["items"]] == [
        "Love & Mercy",
        "Obscurity",
    ]
    assert stored["items"][0]["first_seen"] == today

    # A scrape outage does not change the previous snapshot

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
            # Planted on the first run. It is on the page, but it is not news
            new_item(9105, "Arrival Planted", first_seen=None),
            # Stamped before the window opened. It is no longer news
            new_item(9106, "Arrival Stale", first_seen=stale),
            # An unmatched film. The shelf skips it quietly
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
    arrivals_section = body.split('id="newly-added-shelf-258"')[1].split("<h4 ")[0]
    assert "Arrival Fresh (1956)" in arrivals_section
    # A new film on the watchlist belongs to the watchlist shelf, never
    # to this shelf. The feed itself is first-party proof that the film
    # is watchable. Thus, the synthesized Criterion match shows the film
    # there even with no cached TMDB availability
    assert "Arrival Wanted" not in arrivals_section
    watchlist_section = body.split('id="watchlist-shelf"')[1].split("<h4 ")[0]
    assert "Arrival Wanted (1956)" in watchlist_section
    assert "Arrival Owned" not in body
    assert "Arrival Dismissed" not in body
    assert "Arrival Planted" not in body
    assert "Arrival Stale" not in body
    assert "Arrival Unmatched" not in body

    # "See more…" opens the in-app arrival inventory, at the section
    # of this provider

    assert 'href="/newly-added#newly-added-258"' in body

    # The runtime filter applies here as it does everywhere else. This
    # includes the emptied-not-hidden message (#198)

    filtered = admin_client.get("/?minutes=100").get_data(as_text=True)
    assert "Arrival Fresh (1956)" in filtered
    emptied = admin_client.get("/?minutes=10").get_data(as_text=True)
    assert 'id="newly-added-shelf-258"' in emptied
    assert "Nothing newly added fits in 10 minutes" in emptied

    # The shelf hides the green corner fold. Each film here is newly
    # added by definition

    assert "data-no-new-fold" in body


def test_shelf_hides_without_subscription_or_arrivals(app, admin_client):
    today = date.today().isoformat()

    # No Criterion subscription means no shelf, even with a stored feed

    plant_feed(app, [new_item(9201, "Arrival Hidden", first_seen=today)])
    body = admin_client.get("/").get_data(as_text=True)
    assert 'id="newly-added-shelf-258"' not in body

    # Subscribed, but no film in the window. This also means no shelf

    subscribe_criterion(app)
    plant_feed(app, [new_item(9201, "Arrival Hidden", first_seen=None)])
    body = admin_client.get("/").get_data(as_text=True)
    assert 'id="newly-added-shelf-258"' not in body


def test_new_arrival_lights_its_availability_badge(app, admin_client, monkeypatch):
    """Test that a new arrival turns the availability badge green.

    The availability badge of an unowned film can render in many
    places. A recent arrival on the newly-added feed of that provider
    turns the badge green and adds the date. A film can be on both the
    leaving set and the feed. Then the leaving badge outranks the added
    badge. The strip of an OWNED film shows neither annotation. The copy
    on the shelf stays (decided by Glenn, 2026-08-27)."""

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
        both = make_movie("Blink And Miss", 1957, tmdb_id=9402)
        owned = make_movie("Arrived Owned", 1958, tmdb_id=9403)
        make_movie_file(owned, "Bluray-1080p")
        db.session.commit()
        arrived_id, both_id, owned_id = arrived.id, both.id, owned.id
    for tmdb_id in (9401, 9402, 9403):
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
            new_item(9403, "Arrived Owned", first_seen=first_seen.isoformat()),
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

    # The departure urgency wins on the film that has both marks

    page = admin_client.get(f"/movie/{both_id}").get_data(as_text=True)
    leaving_label = departs.strftime("%B %-d")
    assert f"Criterion Channel &middot; leaving {leaving_label}" in page
    assert f"Criterion Channel &middot; added {label}" not in page

    # The strip of the owned film stays plain, even with the feed entry

    page = admin_client.get(f"/movie/{owned_id}").get_data(as_text=True)
    assert 'title="Streaming on Criterion Channel"' in page
    assert f"Criterion Channel &middot; added {label}" not in page


def test_newly_added_page_lists_the_complete_inventory(app, admin_client):
    """Test that /newly-added lists the complete inventory.

    Unlike the home shelf, /newly-added excludes nothing. The owned and
    the seen films stay in the list. The unmatched films come last as
    plain rows. The section links to the page of the provider. The page
    needs no subscription. Like /leaving, it renders from the stored
    feeds alone."""

    from app import db
    from app.models import UserMovieReview, UserWatchlist
    from app.videos import star_rating_fields

    # A taste profile but no subscription. The page must still render

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
            # One planted on the first run and one aged out. Neither is news
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

    # The unmatched film comes last as a plain row. The section links
    # to the page of the provider

    assert "Also new" in body
    assert "Inventory Unmatched (1962)" in body
    assert "Directed by Jane Doe" in body
    assert 'href="https://www.criterionchannel.com/newly-added"' in body

    # The films on the watchlist are first. The owned films are last

    assert body.index("Inventory Wanted") < body.index("Inventory Fresh")
    assert body.index("Inventory Fresh") < body.index("Inventory Owned")

    # The standing nav link reaches the page from each page. This page
    # hides the green corner fold, as the shelf does

    assert 'href="/newly-added"' in body
    assert "data-no-new-fold" in body


def test_new_arrival_feeds_the_green_poster_fold(app, admin_client):
    """Test that a new arrival feeds the green poster fold.

    /movie_states answers fold_new with the label of the feed for a
    recent arrival on a subscribed provider. This applies to the
    movie-keyed and the tmdb-keyed entries. The recently-available
    record of the alert diff outranks the feed label. A user without a
    subscription gets no fold."""

    import json as jsonlib

    from app import db
    from app.models import User, UserStreamingProvider

    subscribe_criterion(app)
    first_seen = date.today() - timedelta(days=3)
    with app.app_context():
        movie = make_movie("Folded Arrival", 1956, tmdb_id=9701)
        owned = make_movie("Folded Owned", 1958, tmdb_id=9703)
        make_movie_file(owned, "Bluray-1080p")
        db.session.commit()
        movie_id = movie.id
        owned_id = owned.id
        user_id = User.query.filter_by(admin=True).first().id
    plant_feed(
        app,
        [
            new_item(9701, "Folded Arrival", first_seen=first_seen.isoformat()),
            new_item(9702, "Folded Stranger", first_seen=first_seen.isoformat()),
            new_item(9703, "Folded Owned", first_seen=first_seen.isoformat()),
        ],
    )
    label = f"Added to the Criterion Channel {first_seen.strftime('%B %-d')}"

    payload = admin_client.get(
        f"/movie_states?movie_ids={movie_id},{owned_id}&tmdb_ids=9702,9703"
    ).get_json()
    assert payload["movies"][str(movie_id)]["fold_new"] == label
    assert payload["tmdb"]["9702"]["fold_new"] == label

    # An OWNED film never folds for a feed arrival. This applies to the
    # movie key and to the tmdb id (decided by Glenn, 2026-08-27)

    assert payload["movies"][str(owned_id)]["fold_new"] is None
    assert payload["tmdb"]["9703"]["fold_new"] is None

    # The record of the alert diff wins over the feed label. On an
    # owned film, only the local-arrival label folds. A service arrival
    # does not fold

    recent_key = f"fitzflix:availability:recent:{user_id}"
    app.redis.hset(
        recent_key,
        str(movie_id),
        jsonlib.dumps(
            {"date": date.today().isoformat(), "label": "New on Criterion Channel"}
        ),
    )
    app.redis.hset(
        recent_key,
        str(owned_id),
        jsonlib.dumps(
            {"date": date.today().isoformat(), "label": "New on Criterion Channel"}
        ),
    )
    payload = admin_client.get(
        f"/movie_states?movie_ids={movie_id},{owned_id}"
    ).get_json()
    assert payload["movies"][str(movie_id)]["fold_new"] == "New on Criterion Channel"
    assert payload["movies"][str(owned_id)]["fold_new"] is None

    app.redis.hset(
        recent_key,
        str(owned_id),
        jsonlib.dumps({"date": date.today().isoformat(), "label": "New in library"}),
    )
    payload = admin_client.get(f"/movie_states?movie_ids={owned_id}").get_json()
    assert payload["movies"][str(owned_id)]["fold_new"] == "New in library"

    # No subscription means no feed fold

    app.redis.delete(recent_key)
    with app.app_context():
        UserStreamingProvider.query.delete()
        db.session.commit()
    payload = admin_client.get(
        f"/movie_states?movie_ids={movie_id}&tmdb_ids=9702"
    ).get_json()
    assert payload["movies"][str(movie_id)]["fold_new"] is None
    assert payload["tmdb"]["9702"]["fold_new"] is None


def test_movie_page_poster_wears_the_fold(app, admin_client):
    """Test that the movie-page poster shows the corner fold.

    The server renders the fold on the poster of an unowned record. The
    fold is green for a feed arrival. It becomes red when the film
    joins the leaving set. There is one fold, and the departure urgency
    is first. The poster of an OWNED film ignores both signals. Only
    the recent arrival of the local file folds it (decided by Glenn,
    2026-08-27)."""

    import json as jsonlib

    from app import db
    from app.models import User
    from tests.test_leaving_criterion import plant_shelf

    subscribe_criterion(app)
    with app.app_context():
        movie = make_movie("Folded Poster", 1956, tmdb_id=9801)
        owned = make_movie("Folded Poster Owned", 1958, tmdb_id=9803)
        make_movie_file(owned, "Bluray-1080p")
        db.session.commit()
        movie_id = movie.id
        owned_id = owned.id
        user_id = User.query.filter_by(admin=True).first().id
    first_seen = date.today() - timedelta(days=2)
    plant_feed(
        app,
        [
            new_item(9801, "Folded Poster", first_seen=first_seen.isoformat()),
            new_item(9803, "Folded Poster Owned", first_seen=first_seen.isoformat()),
        ],
    )

    page = admin_client.get(f"/movie/{movie_id}").get_data(as_text=True)
    label = f"Added to the Criterion Channel {first_seen.strftime('%B %-d')}"
    assert f'aria-label="{label}"' in page
    assert "poster-fold poster-fold-new" in page

    departs = date.today() + timedelta(days=5)
    plant_shelf(
        app,
        [shelf_item(9801, "Folded Poster"), shelf_item(9803, "Folded Poster Owned")],
        departs=departs,
    )
    page = admin_client.get(f"/movie/{movie_id}").get_data(as_text=True)
    assert "poster-fold poster-fold-leaving" in page
    assert "poster-fold poster-fold-new" not in page

    # The owned film is on the feed AND the leaving set. It folds for
    # neither. Only a "New in library" record gets the green fold

    page = admin_client.get(f"/movie/{owned_id}").get_data(as_text=True)
    assert "poster-fold poster-fold-leaving" not in page
    assert "poster-fold poster-fold-new" not in page

    app.redis.hset(
        f"fitzflix:availability:recent:{user_id}",
        str(owned_id),
        jsonlib.dumps({"date": date.today().isoformat(), "label": "New in library"}),
    )
    page = admin_client.get(f"/movie/{owned_id}").get_data(as_text=True)
    assert "poster-fold poster-fold-new" in page
    assert 'aria-label="New in library"' in page


def test_log_page_poster_wears_the_fold(app, admin_client, monkeypatch):
    """Test that the TMDB log page (an unowned film) shows the same fold.

    The server renders the fold on the poster."""

    import app.main.discover as discover

    from tests.test_streaming import plant_availability

    subscribe_criterion(app)
    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")

    def fake_tmdb_get(url, params=None, **kwargs):
        if url.endswith("/movie/9802"):
            return FakeResponse(
                payload={
                    "id": 9802,
                    "title": "Folded Log",
                    "release_date": "1956-01-01",
                    "poster_path": "/9802.jpg",
                    "overview": "",
                    "runtime": 90,
                    "genres": [],
                    "credits": {"cast": [], "crew": []},
                    "release_dates": {"results": []},
                }
            )
        return FakeResponse(payload={})

    monkeypatch.setattr(discover, "tmdb_get", fake_tmdb_get)
    plant_availability(
        app, 9802, {"link": None, "flatrate": [], "ads": [], "rent": [], "buy": []}
    )
    plant_feed(app, [new_item(9802, "Folded Log", first_seen=date.today().isoformat())])

    page = admin_client.get("/review/tmdb/9802").get_data(as_text=True)
    assert "poster-fold poster-fold-new" in page
    assert "Added to the Criterion Channel" in page


def test_newly_added_page_says_when_nothing_is_new(app, admin_client):
    body = admin_client.get("/newly-added").get_data(as_text=True)
    assert "There is nothing new now." in body

    # A stored feed can have only arrivals from before the tracking or
    # the window. The page reads it the same as no feed

    plant_feed(app, [new_item(9601, "Inventory Quiet", first_seen=None)])
    body = admin_client.get("/newly-added").get_data(as_text=True)
    assert "There is nothing new now." in body
    assert "Inventory Quiet" not in body
