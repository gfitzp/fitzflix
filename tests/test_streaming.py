"""Streaming availability via TMDb's JustWatch-licensed watch-provider
data: the cached registry and per-title lookups, the Profile picker,
and the per-user surfaces (movie pages, TMDb search) with the
mandatory JustWatch attribution."""

import json
import re

from datetime import datetime

from tests.factories import make_movie, make_movie_file


class FakeTMDb:
    """Canned TMDb response."""

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
        return FakeTMDb(
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
        return FakeTMDb({"results": {}})

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
    import app.main.routes as main_routes

    def fake_tmdb_get(url, **kwargs):
        if url.endswith("/search/movie"):
            return FakeTMDb(
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
        return FakeTMDb({"results": []})

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(main_routes, "tmdb_get", fake_tmdb_get)

    subscribe(app, 8, "Netflix")
    plant_availability(
        app,
        700,
        {"link": None, "flatrate": [NETFLIX], "ads": [], "rent": [], "buy": []},
    )

    page = admin_client.get("/search/tmdb?q=streamable").get_data(as_text=True)

    # The same logo badge as the movie-page strip, tooltip and all

    assert 'title="Streaming on Netflix"' in page
    assert "/w45/netflix.jpg" in page
    assert "Streaming data by JustWatch" in page


def test_search_results_without_matches_carry_no_attribution(
    app, admin_client, monkeypatch
):
    import app.main.routes as main_routes

    def fake_tmdb_get(url, **kwargs):
        if url.endswith("/search/movie"):
            return FakeTMDb(
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
        return FakeTMDb({"results": []})

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(main_routes, "tmdb_get", fake_tmdb_get)

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
    import app.main.routes as main_routes

    def fake_tmdb_get(url, **kwargs):
        return FakeTMDb(
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
    monkeypatch.setattr(main_routes, "tmdb_get", fake_tmdb_get)

    subscribe(app, 8, "Netflix")
    subscribe(app, 2, "Apple TV")
    plant_availability(
        app,
        800,
        {
            "link": None,
            "flatrate": [],
            "ads": [],
            "rent": [AMAZON, APPLE],
            "buy": [AMAZON],
        },
    )

    # A film with no local file shows where it can be rented as a logo
    # badge, filtered to the user's own services (Amazon isn't one of
    # them) — and with a rental in hand there's no negative badge

    page = admin_client.get("/review/tmdb/800").get_data(as_text=True)
    assert "Apple TV (rent)" in page
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
    """A 404 is TMDb's answer (stale or wrong tmdb id), not an outage:
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
        return FakeTMDb({"results": {"US": {"flatrate": [NETFLIX]}}})

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
    """Career rows without a local file get the same logo badges as the
    other surfaces; owned rows keep their quality badge unadorned."""

    import app.main.routes as main_routes

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
            return FakeTMDb(
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
        return FakeTMDb({"name": "Streaming Actor"})

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(main_routes, "tmdb_get", fake_tmdb_get)

    subscribe(app, 8, "Netflix")

    # Both films stream on Netflix, but only the unowned row may badge

    for tmdb_id in (910, 911):
        plant_availability(
            app,
            tmdb_id,
            {"link": None, "flatrate": [NETFLIX], "ads": [], "rent": [], "buy": []},
        )

    page = admin_client.get("/library/movie?credit=777003").get_data(as_text=True)
    assert page.count('title="Streaming on Netflix"') == 1
    assert page.index("Filmography Unowned") < page.index(
        'title="Streaming on Netflix"'
    )
    assert "/w45/netflix.jpg" in page
    assert "Streaming data by JustWatch" in page


def test_filmography_defers_overflow_to_a_warm_task(app, admin_client, monkeypatch):
    """When the bounded batch defers ids, one warm task lands on the
    maintenance queue and the marker stops reload storms."""

    import app.main.routes as main_routes

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
            return FakeTMDb(
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
        return FakeTMDb({"name": "Deferred Actor"})

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(main_routes, "tmdb_get", fake_tmdb_get)
    monkeypatch.setattr(
        main_routes, "batch_title_availability", lambda ids, **kw: ({}, [921])
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
