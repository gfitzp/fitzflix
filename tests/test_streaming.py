"""Streaming availability via TMDB's JustWatch-licensed watch-provider
data: the cached registry and per-title lookups, the Profile picker,
and the per-user surfaces (movie pages, TMDB search) with the
mandatory JustWatch attribution."""

import json
import re

from datetime import datetime

from tests.factories import make_movie, make_movie_file


class FakeTMDB:
    """Canned TMDB response."""

    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        """Never an HTTP error."""

    def json(self):
        """The canned payload."""

        return self.payload


def csrf_token_from(page_html):
    """The CSRF token baked into a rendered form."""

    match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page_html)
    assert match, "no csrf token found in page"
    return match.group(1)


def plant_registry(app, providers):
    """Store a fake provider registry in Redis, as the fetch would."""

    from app.streaming import REGISTRY_KEY

    app.redis.set(REGISTRY_KEY, json.dumps(providers))


def plant_availability(app, tmdb_id, payload):
    """Store a fake per-title availability payload in Redis."""

    from app.streaming import AVAILABILITY_KEY

    app.redis.set(AVAILABILITY_KEY.format(tmdb_id=tmdb_id), json.dumps(payload))


def subscribe(app, provider_id, name, email=None):
    """Add a streaming service to a user's profile directly."""

    from app import db
    from app.models import User, UserStreamingProvider

    with app.app_context():
        query = User.query
        user = (
            query.filter_by(email=email).one()
            if email
            else query.filter_by(admin=True).first()
        )
        db.session.add(
            UserStreamingProvider(
                user_id=user.id,
                provider_id=provider_id,
                name=name,
                logo_path=f"/logo{provider_id}.jpg",
            )
        )
        db.session.commit()


NETFLIX = {"provider_id": 8, "provider_name": "Netflix", "logo_path": "/netflix.jpg"}
MAX = {"provider_id": 1899, "provider_name": "Max", "logo_path": "/max.jpg"}
AMAZON = {
    "provider_id": 10,
    "provider_name": "Amazon Video",
    "logo_path": "/amazon.jpg",
}
APPLE = {"provider_id": 2, "provider_name": "Apple TV", "logo_path": "/apple.jpg"}
TUBI = {"provider_id": 73, "provider_name": "Tubi TV", "logo_path": "/tubi.jpg"}


def test_provider_registry_fetches_once_and_sorts(app, monkeypatch):
    import app.streaming as streaming

    calls = []

    def fake_tmdb_get(url, **kwargs):
        calls.append(url)
        return FakeTMDB(
            {
                "results": [
                    {**MAX, "display_priorities": {"US": 2}},
                    {**NETFLIX, "display_priorities": {"US": 1}},
                ]
            }
        )

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(streaming, "tmdb_get", fake_tmdb_get)

    with app.app_context():
        first = streaming.provider_registry()
        second = streaming.provider_registry()

    assert len(calls) == 1
    assert [p["provider_name"] for p in first] == ["Netflix", "Max"]
    assert second == first


def test_title_availability_caches_even_when_empty(app, monkeypatch):
    import app.streaming as streaming

    calls = []

    def fake_tmdb_get(url, **kwargs):
        calls.append(url)
        return FakeTMDB({"results": {}})

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(streaming, "tmdb_get", fake_tmdb_get)

    with app.app_context():
        first = streaming.title_availability(603)
        second = streaming.title_availability(603)

    # A film with no US providers is cached as an empty payload rather
    # than re-queried on every page view

    assert len(calls) == 1
    assert first["flatrate"] == [] and second["flatrate"] == []


def test_streaming_matches_only_subscription_kinds(app):
    from app.streaming import streaming_matches

    availability = {
        "link": "https://www.themoviedb.org/movie/603/watch",
        "flatrate": [NETFLIX],
        "ads": [TUBI, NETFLIX],
        "rent": [MAX],
        "buy": [MAX],
    }

    matches = streaming_matches(availability, {8, 73, 1899})
    by_name = {m["provider_name"]: m["kind"] for m in matches}

    # Netflix appears once (flatrate wins over its ads duplicate), Tubi
    # comes through as free-with-ads, and Max — rent/buy only — is not a
    # subscription match at all

    assert by_name == {"Netflix": "flatrate", "Tubi TV": "ads"}


CRITERION = {
    "provider_id": 258,
    "provider_name": "Criterion Channel",
    "logo_path": "/criterion.jpg",
}


def plant_newly_added(app, tmdb_id, first_seen):
    """Store a scraped newly-added feed entry, as the diff would."""

    from app.newly_added import NEWLY_ADDED_KEY

    app.redis.set(
        NEWLY_ADDED_KEY.format(provider_id=258),
        json.dumps(
            {
                "items": [
                    {
                        "tmdb_id": tmdb_id,
                        "first_seen": first_seen.isoformat(),
                        "scraped_title": "x",
                        "scraped_year": 1981,
                    }
                ]
            }
        ),
    )


def test_scraped_arrival_synthesizes_criterion_match(app, monkeypatch):
    # Sept 1 2026: films live on the Channel's own newly-added page
    # showed no Criterion service because TMDB's JustWatch feed hadn't
    # caught up — the scrape is first-party word, so it wins

    from datetime import date

    from app.streaming import streaming_matches

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    plant_registry(app, [CRITERION])
    plant_newly_added(app, 22171, date.today())
    availability = {"link": None, "flatrate": [NETFLIX], "ads": [], "rent": []}

    with app.app_context():
        matches = streaming_matches(availability, {8, 258}, tmdb_id=22171)

    criterion = [m for m in matches if m["provider_id"] == 258]
    assert len(criterion) == 1
    assert criterion[0]["kind"] == "flatrate"
    assert criterion[0]["logo_path"] == "/criterion.jpg"
    assert criterion[0]["new_since"] == date.today().strftime("%B %-d")


def test_leaving_set_synthesizes_criterion_match_even_unfetched(app):
    # The leaving store is equally first-party, and synthesis holds up
    # even while the TMDB payload is unknown (None)

    import calendar
    from datetime import date

    from app.leaving_criterion import LEAVING_KEY
    from app.streaming import streaming_matches

    today = date.today()
    departs = date(
        today.year, today.month, calendar.monthrange(today.year, today.month)[1]
    )
    app.redis.set(
        LEAVING_KEY,
        json.dumps({"departs": departs.isoformat(), "items": [{"tmdb_id": 42}]}),
    )

    with app.app_context():
        matches = streaming_matches(None, {258}, tmdb_id=42)

    assert [m["provider_id"] for m in matches] == [258]
    assert matches[0]["kind"] == "flatrate"
    assert matches[0]["leaving"] == departs.strftime("%B %-d")
    # The registry stand-in still renders — the badge just has no logo
    assert matches[0]["provider_name"] == "Criterion Channel"
    assert matches[0]["logo_path"] is None


def test_no_synthesis_when_tmdb_already_lists_criterion(app):
    from datetime import date

    from app.streaming import streaming_matches

    plant_newly_added(app, 22171, date.today())
    availability = {"link": None, "flatrate": [CRITERION], "ads": [], "rent": []}

    with app.app_context():
        matches = streaming_matches(availability, {258}, tmdb_id=22171)

    assert [m["provider_id"] for m in matches] == [258]
    assert matches[0]["new_since"] == date.today().strftime("%B %-d")


def test_no_synthesis_without_criterion_subscription(app):
    from datetime import date

    from app.streaming import streaming_matches

    plant_newly_added(app, 22171, date.today())

    with app.app_context():
        assert streaming_matches(None, {8}, tmdb_id=22171) == []
        # And the owned-film callers' tmdb_id=None skips synthesis too
        assert streaming_matches(None, {258}, tmdb_id=None) == []


def test_profile_picker_saves_and_removes_services(app, admin_client, monkeypatch):
    from app.models import User, UserStreamingProvider

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")

    plant_registry(
        app,
        [
            {**NETFLIX, "display_priority": 1},
            {**MAX, "display_priority": 2},
        ],
    )

    page = admin_client.get("/profile").get_data(as_text=True)
    assert "Streaming Services" in page
    assert "Netflix" in page and "Max" in page
    assert "Streaming data by JustWatch" in page

    # The picker lists alphabetically, not by JustWatch display priority
    # (Netflix outranks Max in priority but follows it in the alphabet)

    assert page.index("Max") < page.index("Netflix")

    token = csrf_token_from(page)
    response = admin_client.post(
        "/profile",
        data={
            "csrf_token": token,
            "providers": ["8", "1899"],
            "providers_submit": "Save Streaming Services",
        },
    )
    assert response.status_code == 302

    with app.app_context():
        user_id = User.query.filter_by(admin=True).first().id
        rows = {
            row.provider_id: row
            for row in UserStreamingProvider.query.filter_by(user_id=user_id)
        }
        assert set(rows) == {8, 1899}
        assert rows[8].name == "Netflix"
        assert rows[8].logo_path == "/netflix.jpg"

    # Unchecking a service removes it

    response = admin_client.post(
        "/profile",
        data={
            "csrf_token": token,
            "providers": ["8"],
            "providers_submit": "Save Streaming Services",
        },
    )
    assert response.status_code == 302

    with app.app_context():
        rows = UserStreamingProvider.query.filter_by(user_id=user_id).all()
        assert [row.provider_id for row in rows] == [8]


def test_movie_page_shows_streaming_on_your_services(app, admin_client, monkeypatch):
    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")

    with app.app_context():
        movie = make_movie(
            "Streaming Owned",
            1999,
            tmdb_id=603,
            tmdb_data_as_of=datetime.utcnow(),
        )
        make_movie_file(movie, "Bluray-1080p")
        from app import db

        db.session.commit()
        movie_id = movie.id

    subscribe(app, 8, "Netflix")
    plant_availability(
        app,
        603,
        {
            "link": "https://www.themoviedb.org/movie/603/watch",
            "flatrate": [NETFLIX],
            "ads": [],
            "rent": [AMAZON],
            "buy": [AMAZON],
        },
    )

    page = admin_client.get(f"/movie/{movie_id}").get_data(as_text=True)

    # The library badge leads, then the streaming match as a logo badge

    assert "In library" in page
    assert 'title="Streaming on Netflix"' in page
    assert page.index("In library") < page.index('title="Streaming on Netflix"')
    assert "Streaming data by JustWatch" in page
    assert "All watch options" in page

    # Owned films never advertise rentals — the film is on the shelf

    assert "(rent)" not in page


def test_owned_movie_with_no_match_notes_the_local_copy(app, admin_client, monkeypatch):
    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")

    with app.app_context():
        movie = make_movie(
            "Streaming Quiet",
            1999,
            tmdb_id=604,
            tmdb_data_as_of=datetime.utcnow(),
        )
        make_movie_file(movie, "Bluray-1080p")
        from app import db

        db.session.commit()
        movie_id = movie.id

    subscribe(app, 8, "Netflix")
    plant_availability(
        app,
        604,
        {"link": None, "flatrate": [MAX], "ads": [], "rent": [], "buy": []},
    )

    # The film streams somewhere, but not on the user's services — an
    # owned film shows the library badge instead of a negative, and a
    # bare library badge shows no JustWatch data so it carries no credit

    page = admin_client.get(f"/movie/{movie_id}").get_data(as_text=True)
    assert "In library" in page
    assert "Not on your services" not in page
    assert "Streaming data by JustWatch" not in page


def test_movie_page_without_subscriptions_shows_nothing(app, admin_client, monkeypatch):
    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")

    with app.app_context():
        movie = make_movie(
            "Streaming Unpicked",
            1999,
            tmdb_id=605,
            tmdb_data_as_of=datetime.utcnow(),
        )
        make_movie_file(movie, "Bluray-1080p")
        from app import db

        db.session.commit()
        movie_id = movie.id

    plant_availability(
        app,
        605,
        {"link": None, "flatrate": [NETFLIX], "ads": [], "rent": [], "buy": []},
    )

    page = admin_client.get(f"/movie/{movie_id}").get_data(as_text=True)
    assert "Streaming data by JustWatch" not in page
    assert "In library" not in page


def test_search_results_badge_unowned_matches(app, admin_client, monkeypatch):
    import app.main.search as search

    def fake_tmdb_get(url, **kwargs):
        if url.endswith("/search/movie"):
            return FakeTMDB(
                {
                    "results": [
                        {
                            "id": 700,
                            "title": "Streamable Search Hit",
                            "release_date": "1999-09-09",
                            "overview": "Findable.",
                        }
                    ]
                }
            )
        return FakeTMDB({"results": []})

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(search, "tmdb_get", fake_tmdb_get)

    subscribe(app, 8, "Netflix")
    subscribe(app, 2, "Apple TV")
    plant_availability(
        app,
        700,
        {"link": None, "flatrate": [NETFLIX], "ads": [], "rent": [APPLE], "buy": []},
    )

    page = admin_client.get("/search/tmdb?q=streamable").get_data(as_text=True)

    # The same logo badges as the movie-page strip, tooltips and all —
    # streaming and rental side by side

    assert 'title="Streaming on Netflix"' in page
    assert "/w45/netflix.jpg" in page
    assert 'title="Rent from Apple TV"' in page
    assert "Apple TV (rent)" in page
    assert "Streaming data by JustWatch" in page


def test_search_results_without_matches_carry_no_attribution(
    app, admin_client, monkeypatch
):
    import app.main.search as search

    def fake_tmdb_get(url, **kwargs):
        if url.endswith("/search/movie"):
            return FakeTMDB(
                {
                    "results": [
                        {
                            "id": 701,
                            "title": "Unstreamable Search Hit",
                            "release_date": "1999-09-09",
                            "overview": "Findable.",
                        }
                    ]
                }
            )
        return FakeTMDB({"results": []})

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(search, "tmdb_get", fake_tmdb_get)

    subscribe(app, 8, "Netflix")
    plant_availability(
        app, 701, {"link": None, "flatrate": [], "ads": [], "rent": [], "buy": []}
    )

    page = admin_client.get("/search/tmdb?q=unstreamable").get_data(as_text=True)
    assert "Streaming on" not in page
    assert "Streaming data by JustWatch" not in page


def test_review_tmdb_page_shows_rental_badge_for_your_stores(
    app, admin_client, monkeypatch
):
    import app.main.discover as discover

    def fake_tmdb_get(url, **kwargs):
        return FakeTMDB(
            {
                "title": "Unowned Reviewable",
                "release_date": "1999-09-09",
                "overview": "Not in the library.",
                "runtime": 90,
                "genres": [],
                "credits": {"cast": []},
                "release_dates": {"results": []},
            }
        )

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(discover, "tmdb_get", fake_tmdb_get)

    subscribe(app, 8, "Netflix")
    subscribe(app, 2, "Apple TV")
    subscribe(app, 1899, "Max")
    plant_availability(
        app,
        800,
        {
            "link": None,
            "flatrate": [],
            "ads": [],
            "rent": [AMAZON, APPLE],
            "buy": [AMAZON, MAX],
        },
    )

    # A film with no local file shows where it can be rented as a logo
    # badge, filtered to the user's own services (Amazon isn't one of
    # them) — and with a rental in hand there's no negative badge

    page = admin_client.get("/review/tmdb/800").get_data(as_text=True)
    assert "Apple TV (rent)" in page
    # Digital purchase is ignored — buying happens on physical media —
    # so buy-only Max shows nothing even though it's a picked service
    assert "(buy)" not in page
    assert "Max (buy)" not in page and 'title="Buy from' not in page
    assert "Amazon Video" not in page
    assert "Not on your services" not in page
    assert "Streaming data by JustWatch" in page


def test_unowned_movie_record_shows_rental_badge(app, admin_client, monkeypatch):
    """A review-only movie record (no local files) gets the rental
    badge on its movie page."""

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")

    with app.app_context():
        movie = make_movie(
            "Streaming Fileless",
            1999,
            tmdb_id=801,
            tmdb_data_as_of=datetime.utcnow(),
        )
        from app import db

        db.session.commit()
        movie_id = movie.id

    subscribe(app, 8, "Netflix")
    subscribe(app, 10, "Amazon Video")
    plant_availability(
        app,
        801,
        {"link": None, "flatrate": [], "ads": [], "rent": [AMAZON], "buy": []},
    )

    page = admin_client.get(f"/movie/{movie_id}").get_data(as_text=True)
    assert "Amazon Video (rent)" in page
    assert "Not on your services" not in page
    assert "Streaming data by JustWatch" in page
    assert "In library" not in page


def test_unowned_film_with_nothing_shows_the_negative_badge(
    app, admin_client, monkeypatch
):
    """No streaming match and no rentals on the user's services — the
    rental elsewhere is filtered out — so the secondary badge says so,
    with the credit."""

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")

    with app.app_context():
        movie = make_movie(
            "Streaming Nowhere",
            1999,
            tmdb_id=802,
            tmdb_data_as_of=datetime.utcnow(),
        )
        from app import db

        db.session.commit()
        movie_id = movie.id

    subscribe(app, 8, "Netflix")
    plant_availability(
        app,
        802,
        {"link": None, "flatrate": [], "ads": [], "rent": [AMAZON], "buy": []},
    )

    page = admin_client.get(f"/movie/{movie_id}").get_data(as_text=True)
    assert "Not on your services" in page
    assert "(rent)" not in page
    assert "Streaming data by JustWatch" in page


def test_title_availability_caches_a_404_as_empty(app, monkeypatch):
    """A 404 is TMDB's answer (stale or wrong tmdb id), not an outage:
    it caches as an empty payload instead of re-querying per view."""

    import requests

    import app.streaming as streaming

    calls = []

    def fake_tmdb_get(url, **kwargs):
        calls.append(url)
        response = requests.Response()
        response.status_code = 404
        raise requests.exceptions.HTTPError(response=response)

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(streaming, "tmdb_get", fake_tmdb_get)

    with app.app_context():
        first = streaming.title_availability(999999)
        second = streaming.title_availability(999999)

    assert len(calls) == 1
    assert first["flatrate"] == [] and second["flatrate"] == []


def test_batch_availability_mixes_cache_hits_and_fetches(app, monkeypatch):
    """The batch helper reads cached titles directly and fetches only
    the misses, which then land in the cache like any single lookup."""

    import app.streaming as streaming

    calls = []

    def fake_tmdb_get(url, **kwargs):
        calls.append(url)
        return FakeTMDB({"results": {"US": {"flatrate": [NETFLIX]}}})

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(streaming, "tmdb_get", fake_tmdb_get)

    plant_availability(
        app,
        900,
        {"link": None, "flatrate": [MAX], "ads": [], "rent": [], "buy": []},
    )

    with app.app_context():
        results, deferred = streaming.batch_title_availability([900, 901, None, 900])

    assert deferred == []
    assert len(calls) == 1 and "/movie/901/" in calls[0]
    assert results[900]["flatrate"] == [MAX]
    assert results[901]["flatrate"] == [NETFLIX]
    assert app.redis.get(streaming.AVAILABILITY_KEY.format(tmdb_id=901))

    # A fetch limit defers the overflow instead of stalling the render

    with app.app_context():
        results, deferred = streaming.batch_title_availability(
            [900, 902], fetch_limit=0
        )

    assert results[900]["flatrate"] == [MAX]
    assert deferred == [902]
    assert len(calls) == 1


def test_filmography_badges_unowned_films_on_your_services(
    app, admin_client, monkeypatch
):
    """Availability moved into the popover (Aug 2026): filmography
    tiles carry no provider badges, and an unowned career film's card
    answers from the same cached availability the page warms."""

    import app.main.discover as discover
    import app.main.library as library

    from app import db
    from app.models import TMDBCredit, MovieCast

    with app.app_context():
        person = TMDBCredit(id=777003, name="Streaming Actor")
        db.session.add(person)
        owned = make_movie(
            "Filmography Owned",
            1980,
            tmdb_id=910,
            tmdb_data_as_of=datetime.utcnow(),
        )
        make_movie_file(owned, "Bluray-1080p")
        db.session.flush()
        db.session.add(
            MovieCast(
                movie_id=owned.id,
                credit_id=person.id,
                character="Lead",
                billing_order=0,
            )
        )
        db.session.commit()

    def fake_tmdb_get(url, **kwargs):
        """Person details and a two-film career."""

        if url.endswith("/movie_credits"):
            return FakeTMDB(
                {
                    "cast": [
                        {
                            "id": 910,
                            "title": "Filmography Owned",
                            "release_date": "1980-05-01",
                            "character": "Lead",
                        },
                        {
                            "id": 911,
                            "title": "Filmography Unowned",
                            "release_date": "1999-09-09",
                            "character": "Cameo",
                        },
                    ]
                }
            )
        return FakeTMDB({"name": "Streaming Actor"})

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(library, "tmdb_get", fake_tmdb_get)

    subscribe(app, 8, "Netflix")
    subscribe(app, 2, "Apple TV")

    # Both films stream on Netflix and rent on Apple TV, but only the
    # unowned row may badge

    for tmdb_id in (910, 911):
        plant_availability(
            app,
            tmdb_id,
            {
                "link": None,
                "flatrate": [NETFLIX],
                "ads": [],
                "rent": [APPLE],
                "buy": [],
            },
        )

    page = admin_client.get("/library/movie?credit=777003").get_data(as_text=True)
    assert 'title="Streaming on Netflix"' not in page
    assert "Apple TV (rent)" not in page
    assert 'data-state-tmdb="911"' in page

    # The unowned film's card serves the badges from the cached
    # availability — logo, rent suffix, and the mandatory credit

    def fake_details_get(url, **kwargs):
        return FakeTMDB(
            {
                "title": "Filmography Unowned",
                "release_date": "1999-09-09",
                "credits": {"cast": [], "crew": []},
            }
        )

    monkeypatch.setattr(discover, "tmdb_get", fake_details_get)
    card = admin_client.get("/movie_card?tmdb_id=911").get_data(as_text=True)
    assert card.count('title="Streaming on Netflix"') == 1
    assert card.count("Apple TV (rent)") == 1
    assert "/w45/netflix.jpg" in card
    assert "Streaming data by JustWatch" in card


def test_filmography_defers_overflow_to_a_warm_task(app, admin_client, monkeypatch):
    """When the bounded batch defers ids, one warm task lands on the
    maintenance queue and the marker stops reload storms."""

    import app.main.library as library

    from app import db
    from app.models import MovieCast, TMDBCredit

    with app.app_context():
        person = TMDBCredit(id=777004, name="Deferred Actor")
        db.session.add(person)
        owned = make_movie(
            "Deferred Owned",
            1980,
            tmdb_id=920,
            tmdb_data_as_of=datetime.utcnow(),
        )
        make_movie_file(owned, "Bluray-1080p")
        db.session.flush()
        db.session.add(
            MovieCast(
                movie_id=owned.id,
                credit_id=person.id,
                character="Lead",
                billing_order=0,
            )
        )
        db.session.commit()

    def fake_tmdb_get(url, **kwargs):
        """Person details and a one-film career."""

        if url.endswith("/movie_credits"):
            return FakeTMDB(
                {
                    "cast": [
                        {
                            "id": 921,
                            "title": "Deferred Unowned",
                            "release_date": "1999-09-09",
                        }
                    ]
                }
            )
        return FakeTMDB({"name": "Deferred Actor"})

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(library, "tmdb_get", fake_tmdb_get)
    monkeypatch.setattr(
        library, "batch_title_availability", lambda ids, **kw: ({}, [921])
    )

    subscribe(app, 8, "Netflix")

    for _ in range(2):
        admin_client.get("/library/movie?credit=777004")

    warm_jobs = [
        job
        for job in app.maintenance_queue.jobs
        if job.func_name == "app.streaming.warm_title_availability"
    ]
    assert len(warm_jobs) == 1
    assert warm_jobs[0].args == ([921],)


def test_batch_availability_reads_the_cache_in_one_call(app, monkeypatch):
    """Cache hits come back through a single MGET — a 400-film list
    paid 400 round trips before Aug 2026 — and refresh re-fetches
    every id, live entries included."""

    import app.streaming as streaming

    calls = []

    def fake_tmdb_get(url, **kwargs):
        calls.append(url)
        return FakeTMDB({"results": {"US": {"flatrate": [NETFLIX]}}})

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(streaming, "tmdb_get", fake_tmdb_get)
    for tmdb_id in (930, 931, 932):
        plant_availability(
            app,
            tmdb_id,
            {"link": None, "flatrate": [MAX], "ads": [], "rent": [], "buy": []},
        )

    gets = []
    real_get = app.redis.get
    monkeypatch.setattr(
        app.redis, "get", lambda key, *a, **k: gets.append(key) or real_get(key)
    )

    with app.app_context():
        results, deferred = streaming.batch_title_availability([930, 931, 932])

    assert deferred == [] and calls == []
    assert {tmdb_id: payload["flatrate"] for tmdb_id, payload in results.items()} == {
        930: [MAX],
        931: [MAX],
        932: [MAX],
    }
    assert gets == []

    with app.app_context():
        results, deferred = streaming.batch_title_availability([930, 931], refresh=True)

    assert sorted(calls) == [
        app.config["TMDB_API_URL"] + "/movie/930/watch/providers",
        app.config["TMDB_API_URL"] + "/movie/931/watch/providers",
    ]
    assert results[930]["flatrate"] == [NETFLIX]
    assert json.loads(app.redis.get(streaming.AVAILABILITY_KEY.format(tmdb_id=931)))[
        "flatrate"
    ] == [NETFLIX]


def test_refresh_availability_covers_every_film_with_a_tmdb_id(app, monkeypatch):
    """The nightly task re-fetches every film's availability, cached or
    not, and restarts each entry's two-day life — the pages read this
    cache and never fetch inline, so it has to be full every morning."""

    import app.streaming as streaming

    from app import db

    with app.app_context():
        make_movie("Refresh Cached", 1970, tmdb_id=940)
        make_movie("Refresh Missing", 1971, tmdb_id=941)
        make_movie("Refresh Local Only", 1972)
        db.session.commit()
    plant_availability(
        app, 940, {"link": None, "flatrate": [MAX], "ads": [], "rent": [], "buy": []}
    )
    app.redis.expire(streaming.AVAILABILITY_KEY.format(tmdb_id=940), 600)

    calls = []

    def fake_tmdb_get(url, **kwargs):
        calls.append(url)
        return FakeTMDB({"results": {"US": {"flatrate": [NETFLIX]}}})

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(streaming, "tmdb_get", fake_tmdb_get)

    assert streaming.refresh_availability() is True
    assert sorted(calls) == [
        app.config["TMDB_API_URL"] + "/movie/940/watch/providers",
        app.config["TMDB_API_URL"] + "/movie/941/watch/providers",
    ]
    for tmdb_id in (940, 941):
        key = streaming.AVAILABILITY_KEY.format(tmdb_id=tmdb_id)
        assert json.loads(app.redis.get(key))["flatrate"] == [NETFLIX]
        assert app.redis.ttl(key) > 86400


def test_list_pages_never_fetch_availability_inline(app, admin_client, monkeypatch):
    """The watchlist and the Criterion catalog answer from the cache
    alone: an uncached film costs no TMDB call during the render (fifty
    of them stalled the page four seconds before Aug 2026) — it's
    handed to one background warm job instead."""

    import app.main.discover as discover
    import app.main.library as library
    import app.streaming as streaming

    from app import db
    from app.models import UserWatchlist

    with app.app_context():
        cached = make_movie("Inline Cached", 1980, tmdb_id=950)
        uncached = make_movie("Inline Uncached", 1981, tmdb_id=951)
        db.session.flush()
        for movie in (cached, uncached):
            db.session.add(UserWatchlist(user_id=1, movie_id=movie.id))
        db.session.commit()
    plant_availability(
        app,
        950,
        {"link": None, "flatrate": [NETFLIX], "ads": [], "rent": [], "buy": []},
    )
    subscribe(app, 8, "Netflix")

    calls = []

    def fake_tmdb_get(url, **kwargs):
        calls.append(url)
        return FakeTMDB({"results": {"US": {"flatrate": [NETFLIX]}}})

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    for module in (streaming, discover, library):
        monkeypatch.setattr(module, "tmdb_get", fake_tmdb_get)

    page = admin_client.get("/watchlist?availability=services").get_data(as_text=True)
    assert "Inline Cached" in page
    assert "Inline Uncached" not in page
    assert "1 film is not on this page yet" in page
    assert calls == []

    warm_jobs = [
        job
        for job in app.maintenance_queue.jobs
        if job.func_name == "app.streaming.warm_title_availability"
    ]
    assert len(warm_jobs) == 1
    assert warm_jobs[0].args == ([951],)
