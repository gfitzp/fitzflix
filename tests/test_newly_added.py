"""Test the newly-added feeds (#246).

The tests cover the generic collection scraper, the first-seen snapshot
diff, the per-provider discovery shelves, and the "added" availability
badge."""

import json

from datetime import date, timedelta

from tests.conftest import dvr_rebuild_jobs
from tests.factories import make_movie, make_movie_file
from tests.test_leaving_criterion import (
    FILM_PAGES,
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
                "source": "https://www.criterionchannel.com/discover/newly-added",
                "items": items,
            }
        ),
    )


def test_fetch_collection_films_reads_directors_and_dedupes(app, monkeypatch):
    """Test the collection reader on the playlist page of the Channel.

    The director of each film comes from its film page. A second read
    takes the directors from the cache and reads only the collection
    page. A film that the playlist repeats shows 1 time."""

    import app.leaving_criterion as leaving_criterion

    from tests.test_leaving_criterion import collection_page

    source = "https://www.criterionchannel.com/discover/newly-added"
    entry = {
        "title": "The Searchers",
        "release_date": "1956-05-16",
        "contentType": "film",
        "deeplink": "https://www.criterionchannel.com/films/aaaa1111/the-searchers",
    }
    calls = []

    def fake_requests_get(url, timeout=None):
        calls.append(url)
        if url in FILM_PAGES:
            return FakeResponse(text=FILM_PAGES[url])
        # The same film 2 times. This is the duplicate case
        return FakeResponse(text=collection_page([entry, entry]))

    monkeypatch.setattr(leaving_criterion.requests, "get", fake_requests_get)

    with app.app_context():
        from app.leaving_criterion import fetch_collection_films

        films = fetch_collection_films(source)
        assert films == [
            {"title": "The Searchers", "director": "John Ford", "year": 1956}
        ]
        assert calls == [source, entry["deeplink"]]

        assert fetch_collection_films(source) == films
        assert calls == [source, entry["deeplink"], source]


def test_fetch_collection_films_survives_a_dead_page(app, monkeypatch):
    """Test the reader on pages that do not answer.

    A dead collection page gives no films. A dead film page gives a
    film with no director."""

    import app.leaving_criterion as leaving_criterion

    source = "https://www.criterionchannel.com/discover/newly-added"
    pages = {source: FakeResponse(status_code=404)}

    def fake_requests_get(url, timeout=None):
        if url not in pages:
            raise ConnectionError("no answer")
        return pages[url]

    monkeypatch.setattr(leaving_criterion.requests, "get", fake_requests_get)

    with app.app_context():
        from app.leaving_criterion import fetch_collection_films

        assert fetch_collection_films(source) == []

        pages[source] = FakeResponse(text=LEAVING_HTML)
        films = fetch_collection_films(source)

    assert [(film["title"], film["director"]) for film in films] == [
        ("The Searchers", None),
        ("Love & Mercy", None),
    ]


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
    # The first run stamps no new film. Thus, the DVR dial has nothing
    # to learn.
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
    # An OWNED new arrival joins the Criterion channel through the
    # synthesized match. Thus, Fitzflix rebuilds the dial the same day.
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


def test_a_match_without_a_payload_keeps_its_tmdb_id(app, monkeypatch):
    """A TMDB timeout must not drop the matched id from the store (#386).

    The fold, the DVR and the shelves read the id. The next run gets the
    payload and does not count the film as new again."""

    import app.newly_added as newly_added

    scraped = [{"title": "The Searchers", "director": "John Ford", "year": 1956}]
    payloads = {}
    monkeypatch.setattr(newly_added, "fetch_collection_films", lambda url: scraped)
    monkeypatch.setattr(newly_added, "match_tmdb_id", lambda *args: 3110)
    monkeypatch.setattr(newly_added, "enriched_movie", lambda i: payloads.get(i))

    from app.leaving_criterion import CRITERION_PROVIDER_ID
    from app.newly_added import NEWLY_ADDED_KEY, newly_added_since, refresh_newly_added

    from app import db

    key = NEWLY_ADDED_KEY.format(provider_id=CRITERION_PROVIDER_ID)
    app.redis.set(key, json.dumps({"items": []}))
    with app.app_context():
        make_movie_file(make_movie("The Searchers", 1956, tmdb_id=3110), "DVD")
        db.session.commit()

    # The day is read before the run. A midnight during the test then
    # still matches 1 of the 2 days (#447).
    days = {date.today().isoformat()}
    assert refresh_newly_added() is True
    days.add(date.today().isoformat())
    (item,) = json.loads(app.redis.get(key))["items"]
    assert item["tmdb_id"] == 3110
    assert item["first_seen"] in days
    first_seen = item["first_seen"]
    with app.app_context():
        assert newly_added_since(3110, CRITERION_PROVIDER_ID) is not None
    assert len(dvr_rebuild_jobs(app)) == 1

    payloads[3110] = shelf_item(3110, "The Searchers")
    assert refresh_newly_added() is True
    (item,) = json.loads(app.redis.get(key))["items"]
    assert item["tmdb_id"] == 3110
    assert not item.get("payload_missing")
    assert item["title"] == "The Searchers"
    assert item["first_seen"] == first_seen
    assert len(dvr_rebuild_jobs(app)) == 1


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
    assert 'href="https://www.criterionchannel.com/discover/newly-added"' in body

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

    monkeypatch.setattr("app.tmdb_apply.tmdb_get", fake_tmdb_get)
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


def test_a_film_without_its_payload_is_no_placeholder_card(app, admin_client):
    """A matched film whose payload failed stays off the shelf (#419).

    It has no poster and no features. The inventory lists it as a plain
    scraped row with the unmatched films."""

    subscribe_criterion(app)
    today = date.today().isoformat()
    plant_feed(
        app,
        [
            new_item(9701, "Payload Fresh", first_seen=today),
            {
                "title": "Payload Missing",
                "director": "Jane Doe",
                "year": 1963,
                "tmdb_id": 9702,
                "payload_missing": True,
                "first_seen": today,
                "scraped_title": "Payload Missing",
                "scraped_year": 1963,
            },
        ],
    )

    body = admin_client.get("/").get_data(as_text=True)
    shelf = body.split('id="newly-added-shelf-258"')[1].split("<h4 ")[0]
    assert "Payload Fresh (1956)" in shelf
    assert "Payload Missing" not in shelf

    body = admin_client.get("/newly-added").get_data(as_text=True)
    assert "Also new" in body
    unmatched = body.split("Also new")[1]
    assert "Payload Missing (1963)" in unmatched


def test_a_film_that_tmdb_deleted_goes_back_to_unmatched(app, monkeypatch):
    """A cached TMDB 404 never gets a payload. The id goes away (#419)."""

    import app.newly_added as newly_added

    from app.leaving_criterion import CRITERION_PROVIDER_ID
    from app.newly_added import NEWLY_ADDED_KEY, refresh_newly_added

    scraped = [
        {"title": "Gone Film", "director": "John Ford", "year": 1956},
        {"title": "Gone Later", "director": "John Ford", "year": 1957},
    ]
    monkeypatch.setattr(newly_added, "fetch_collection_films", lambda url: scraped)
    monkeypatch.setattr(newly_added, "match_tmdb_id", lambda *args: 3111)
    monkeypatch.setattr(newly_added, "enriched_movie", lambda i: None)
    monkeypatch.setattr(newly_added, "movie_gone", lambda i: i == 3111)

    key = NEWLY_ADDED_KEY.format(provider_id=CRITERION_PROVIDER_ID)
    app.redis.set(
        key,
        json.dumps(
            {
                "items": [
                    {
                        "title": "Gone Later",
                        "year": 1957,
                        "tmdb_id": 3111,
                        "payload_missing": True,
                        "first_seen": "2026-10-01",
                        "scraped_title": "Gone Later",
                        "scraped_year": 1957,
                    }
                ]
            }
        ),
    )

    assert refresh_newly_added() is True
    items = json.loads(app.redis.get(key))["items"]
    assert [item["tmdb_id"] for item in items] == [None, None]
    assert not any(item.get("payload_missing") for item in items)
    assert items[1]["first_seen"] == "2026-10-01"


def test_a_film_with_a_record_ranks_by_the_number_of_its_tile(app):
    """The tile reads the movie-id lane, which adds the award prior (#535).

    The shelf must order by the same number. 2 films with the same
    payload features differ only by their stored scores here."""

    from datetime import datetime

    from app import db
    from app.models import User
    from app.newly_added import newly_added_shelves
    from app.recommendations import SCORES_KEY

    user_id = subscribe_criterion(app)
    today = date.today().isoformat()
    with app.app_context():
        low = make_movie("Tile Order Low", 1956, tmdb_id=9121)
        high = make_movie("Tile Order High", 1956, tmdb_id=9122)
        for movie in (low, high):
            movie.tmdb_data_as_of = datetime(2026, 10, 1)
        db.session.commit()
        app.redis.set(
            SCORES_KEY.format(user_id=user_id),
            json.dumps({str(low.id): 0.25, str(high.id): 1.75}),
        )
        plant_feed(
            app,
            [
                new_item(9121, "Tile Order Low", first_seen=today),
                new_item(9122, "Tile Order High", first_seen=today),
            ],
        )
        try:
            (shelf,) = newly_added_shelves(db.session.get(User, user_id))
            assert [(row["title"], row["score"]) for row in shelf["items"]] == [
                ("Tile Order High", 1.75),
                ("Tile Order Low", 0.25),
            ]
        finally:
            app.redis.delete(SCORES_KEY.format(user_id=user_id))


def test_a_failed_film_page_leaves_the_arrival_unmatched_for_a_day(app, monkeypatch):
    """The newly-added feed skips the match of such a film too (#539)."""

    import app.leaving_criterion as leaving_criterion
    import app.newly_added as newly_added

    for media_id in ("aaaa1111", "bbbb2222"):
        app.redis.delete(leaving_criterion.DIRECTOR_KEY.format(media_id=media_id))

    def fake_requests_get(url, timeout=None):
        if "/films/" in url:
            raise ConnectionError("timed out")
        return FakeResponse(text=LEAVING_HTML)

    matched = []
    monkeypatch.setattr(leaving_criterion.requests, "get", fake_requests_get)
    monkeypatch.setattr(
        newly_added, "match_tmdb_id", lambda *a, **k: matched.append(a) or 3110
    )
    feed = {
        "url": "https://www.criterionchannel.com/discover/newly-added",
        "label": "Newly added",
    }
    key = newly_added.NEWLY_ADDED_KEY.format(provider_id=999001)
    try:
        with app.app_context():
            newly_added._refresh_feed(999001, feed)
            stored = json.loads(app.redis.get(key))
        assert matched == []
        assert [item["tmdb_id"] for item in stored["items"]] == [None, None]
    finally:
        app.redis.delete(key)


def test_the_shelf_reads_the_weights_1_time_per_call(app, monkeypatch):
    """The landing shelf pays about 1 query per film, not 6 (#538).

    Each film has co-preference neighbors. Thus, the score reads them.
    The weights of the user come once per call."""

    from app import db
    from app.models import MovieCopref, User, UserMovieReview
    from app.newly_added import newly_added_shelves
    from app.videos import star_rating_fields
    from tests.test_tmdb_apply import _count_queries

    user_id = subscribe_criterion(app)
    today = date.today().isoformat()
    with app.app_context():
        liked = make_movie("Weighted Western", 1950, tmdb_id=9300)
        db.session.add(
            UserMovieReview(user_id=user_id, movie_id=liked.id, **star_rating_fields(5))
        )
        for tmdb_id in range(9301, 9321):
            db.session.add(
                MovieCopref(tmdb_id_a=tmdb_id, tmdb_id_b=9300, similarity=0.5)
            )
        db.session.commit()

    def queries_for(count):
        plant_feed(
            app,
            [
                new_item(tmdb_id, f"Weighted Arrival {tmdb_id}", first_seen=today)
                for tmdb_id in range(9301, 9301 + count)
            ],
        )
        with app.app_context():
            user = db.session.get(User, user_id)
            return _count_queries(lambda: newly_added_shelves(user))

    extra = queries_for(15) - queries_for(5)
    assert extra <= 12, f"{extra} queries for 10 more films"


def test_the_shelf_scores_only_the_films_that_it_shows(app, monkeypatch):
    """The lane scores ran for logged and owned films too (#580).

    A logged film has no stored score. Each one cost a live score on the
    first landing render of the day."""

    import app.newly_added as newly_added
    from app import db
    from app.models import User, UserMovieReview
    from app.videos import star_rating_fields

    user_id = subscribe_criterion(app)
    today = date.today().isoformat()
    with app.app_context():
        logged = make_movie("Shelf Logged", 1956, tmdb_id=9401)
        db.session.add(
            UserMovieReview(
                user_id=user_id, movie_id=logged.id, **star_rating_fields(4)
            )
        )
        db.session.commit()
    plant_feed(
        app,
        [
            new_item(9401, "Shelf Logged", first_seen=today),
            new_item(9402, "Shelf Shown", first_seen=today),
        ],
    )
    calls = []
    monkeypatch.setattr(
        newly_added,
        "record_lane_scores",
        lambda redis, user_id, tmdb_ids, profile: calls.append(list(tmdb_ids)) or {},
    )
    with app.app_context():
        newly_added.newly_added_shelves(db.session.get(User, user_id))
    assert calls == [[9402]]


def test_the_inventory_ranks_a_record_by_its_movie_lane(app, monkeypatch):
    """The /newly-added page ranked a record by the TMDB lane (#580)."""

    import app.newly_added as newly_added
    from app import db
    from app.models import User

    user_id = subscribe_criterion(app)
    today = date.today().isoformat()
    plant_feed(
        app,
        [
            new_item(9411, "Inventory High", first_seen=today),
            new_item(9412, "Inventory Recorded", first_seen=today),
        ],
    )
    lane = {9411: 2.0, 9412: 1.0}
    monkeypatch.setattr(newly_added, "stored_profile", lambda redis, u: {"x": 1})
    monkeypatch.setattr(
        newly_added,
        "tmdb_taste_score",
        lambda user_id, tmdb_id, item, profile: (lane[tmdb_id], []),
    )
    monkeypatch.setattr(
        newly_added,
        "record_lane_scores",
        lambda redis, user_id, tmdb_ids, profile: {9412: 5.0},
    )
    with app.app_context():
        sections = newly_added.newly_added_inventory(db.session.get(User, user_id))
    films = [film["tmdb_id"] for film in sections[0]["films"]]
    assert films == [9412, 9411]
