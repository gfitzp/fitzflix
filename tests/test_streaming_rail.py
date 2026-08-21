"""The "Streaming on your services" rail: discover pools as candidate
generators, coarse taste ranking, per-title availability verification
(discover's provider filters cross-contaminate, so pools are never
display truth), credits enrichment, and the landing-page section with
its mandatory JustWatch attribution."""

import json

from requests.exceptions import HTTPError

from tests.factories import make_movie, make_movie_file

NETFLIX = {"provider_id": 8, "provider_name": "Netflix", "logo_path": "/netflix.jpg"}


def plant_profile(app, user_id, affinities):
    """Store a taste profile as the nightly recompute would."""

    app.redis.set(
        f"fitzflix:recs:profile:{user_id}",
        json.dumps({"affinities": affinities, "movies": 5}),
    )


def plant_availability(app, tmdb_id, flatrate):
    """Store a per-title availability payload."""

    app.redis.set(
        f"fitzflix:tmdb:watch-providers:movie:{tmdb_id}",
        json.dumps(
            {"link": None, "flatrate": flatrate, "ads": [], "rent": [], "buy": []}
        ),
    )


def subscribe(app, provider_id, name):
    """Add a streaming service to the admin user's profile."""

    from app import db
    from app.models import User, UserStreamingProvider

    with app.app_context():
        user = User.query.filter_by(admin=True).first()
        db.session.add(
            UserStreamingProvider(
                user_id=user.id,
                provider_id=provider_id,
                name=name,
                logo_path=f"/logo{provider_id}.jpg",
            )
        )
        db.session.commit()
        return user.id


COMEDY_PROFILE = {
    "genre:35": {"class": "genre", "label": "Comedy", "count": 3, "score": 0.5},
    "actor:9001": {"class": "actor", "label": "Rail Actor", "count": 2, "score": 0.4},
}

DISCOVER_ITEMS = [
    {
        "id": 5001,
        "genre_ids": [35],
        "release_date": "1994-05-01",
        "original_language": "en",
        "popularity": 10.0,
    },
    {
        "id": 5002,
        "genre_ids": [18],
        "release_date": "1953-05-01",
        "original_language": "en",
        "popularity": 5.0,
    },
    # In the library: excluded before scoring
    {"id": 5003, "genre_ids": [35], "release_date": "1994-05-01", "popularity": 9.0},
    # In the user's diary: excluded before scoring
    {"id": 5004, "genre_ids": [35], "release_date": "1994-05-01", "popularity": 8.0},
    # Discover contamination: claims to stream, availability says no
    {"id": 5005, "genre_ids": [35], "release_date": "1994-05-01", "popularity": 7.0},
]

ENRICHED = {
    5001: {
        "id": 5001,
        "title": "Rail Comedy",
        "release_date": "1994-05-01",
        "poster_path": "/rail1.jpg",
        "runtime": 95,
        "original_language": "en",
        "genres": [{"id": 35, "name": "Comedy"}],
        "keywords": {"keywords": []},
        "credits": {"cast": [{"id": 9001, "name": "Rail Actor"}], "crew": []},
    },
    5002: {
        "id": 5002,
        "title": "Rail Drama",
        "release_date": "1953-05-01",
        "poster_path": "/rail2.jpg",
        "runtime": 110,
        "original_language": "en",
        "genres": [{"id": 18, "name": "Drama"}],
        "keywords": {"keywords": []},
        "credits": {"cast": [], "crew": []},
    },
}


class FakeTMDb:
    """Canned TMDb response."""

    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        """Never an HTTP error."""

    def json(self):
        """The canned payload."""

        return self.payload


def install_rail_fakes(app, monkeypatch, discover_calls=None):
    """Fake tmdb_get for the rail and availability modules: discover
    returns the canned candidates, /movie/{id} returns enrichment
    payloads, availability misses return nothing streamable."""

    import app.streaming as streaming
    import app.streaming_rail as streaming_rail

    def fake_tmdb_get(url, params=None, **kwargs):
        if "/discover/movie" in url:
            if discover_calls is not None:
                discover_calls.append(params or {})
            return FakeTMDb({"results": DISCOVER_ITEMS})
        for tmdb_id, payload in ENRICHED.items():
            if url.endswith(f"/movie/{tmdb_id}"):
                return FakeTMDb(payload)
        if "/watch/providers" in url:
            return FakeTMDb({"results": {}})
        return FakeTMDb({"results": []})

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(streaming_rail, "tmdb_get", fake_tmdb_get)
    monkeypatch.setattr(streaming, "tmdb_get", fake_tmdb_get)


def test_provider_pool_queries_and_caches(app, monkeypatch):
    from app import streaming_rail

    calls = []
    install_rail_fakes(app, monkeypatch, discover_calls=calls)

    with app.app_context():
        pool = streaming_rail.provider_pool(8, "Netflix")
        streaming_rail.provider_pool(8, "Netflix")

    # Three sorts x three pages, fetched once then served from cache

    assert len(calls) == streaming_rail.POOL_PAGES * len(streaming_rail.POOL_SORTS)
    sorts = {params["sort_by"] for params in calls}
    assert sorts == {
        "popularity.desc",
        "vote_average.desc",
        "primary_release_date.desc",
    }

    # Hygiene on every query: US flatrate on the provider, no adult
    # titles, a vote floor (the acclaimed variant needs a deeper one)

    for params in calls:
        assert params["watch_region"] == "US"
        assert params["with_watch_providers"] == "8"
        assert params["with_watch_monetization_types"] == "flatrate"
        assert params["include_adult"] == "false"
        assert params["vote_count.gte"] >= 50
    acclaimed = [p for p in calls if p["sort_by"] == "vote_average.desc"]
    assert all(p["vote_count.gte"] == 200 for p in acclaimed)

    # Provenance tags name the query that produced each candidate

    assert "popular on Netflix" in pool["5001"]["sources"]


def test_deleted_tmdb_id_caches_the_miss(app, monkeypatch):
    """TMDb deletes films whose credit rows linger on person pages, so a
    filmography keeps offering an id the movie endpoint 404s. The miss
    is cached as a null payload: later calls answer None from Redis
    without re-asking TMDb."""

    from app import streaming_rail

    calls = []

    class Gone:
        """A TMDb response for a deleted movie id."""

        status_code = 404

        def raise_for_status(self):
            raise HTTPError("404 Client Error", response=self)

    def fake_tmdb_get(url, params=None, **kwargs):
        calls.append(url)
        return Gone()

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(streaming_rail, "tmdb_get", fake_tmdb_get)

    with app.app_context():
        assert streaming_rail.enriched_movie(126678) is None
        assert streaming_rail.enriched_movie(126678) is None

    assert len(calls) == 1
    cached = app.redis.get(streaming_rail.ENRICHED_KEY.format(tmdb_id=126678))
    assert json.loads(cached) is None


def test_compute_user_rail_excludes_verifies_and_explains(app, monkeypatch):
    from app import db
    from app.models import User, UserMovieReview
    from app.streaming_rail import compute_user_rail
    from app.videos import star_rating_fields

    user_id = subscribe(app, 8, "Netflix")
    plant_profile(app, user_id, COMEDY_PROFILE)
    install_rail_fakes(app, monkeypatch)

    with app.app_context():
        owned = make_movie("Rail Owned", 1994, tmdb_id=5003)
        make_movie_file(owned, "Bluray-1080p")
        logged = make_movie("Rail Logged", 1994, tmdb_id=5004)
        db.session.add(
            UserMovieReview(
                user_id=user_id, movie_id=logged.id, **star_rating_fields(4.0)
            )
        )
        db.session.commit()

    # 5001 and 5002 genuinely stream on Netflix; 5005 was discover
    # contamination and must not survive verification

    plant_availability(app, 5001, [NETFLIX])
    plant_availability(app, 5002, [NETFLIX])
    plant_availability(app, 5005, [])

    with app.app_context():
        user = db.session.get(User, user_id)
        items = compute_user_rail(user)

    ids = [item["tmdb_id"] for item in items]
    assert 5001 in ids and 5002 in ids
    assert 5003 not in ids, "library film recommended"
    assert 5004 not in ids, "diary film recommended"
    assert 5005 not in ids, "unverified discover contamination shown"

    # The comedy outranks the drama on the full-feature rescore, its
    # provenance tag leads the explanation, and the enriched credits
    # contribute ("Rail Actor" carries an affinity)

    top = items[0]
    assert top["tmdb_id"] == 5001
    assert top["title"] == "Rail Comedy"

    # Providers are stored as full match dicts so the landing page can
    # render the standard logo badges

    assert [p["provider_name"] for p in top["providers"]] == ["Netflix"]
    assert top["providers"][0]["kind"] == "flatrate"
    assert top["providers"][0]["logo_path"] == "/netflix.jpg"
    assert top["because"][0] == "popular on Netflix"
    assert "Comedy" in top["because"]
    assert "Rail Actor" in top["because"]
    assert top["runtime"] == 95


def test_recompute_task_stores_rail_payloads(app, monkeypatch):
    from app.streaming_rail import RAIL_KEY, recompute_streaming_rail

    user_id = subscribe(app, 8, "Netflix")
    plant_profile(app, user_id, COMEDY_PROFILE)
    install_rail_fakes(app, monkeypatch)
    plant_availability(app, 5001, [NETFLIX])
    plant_availability(app, 5002, [NETFLIX])
    plant_availability(app, 5005, [])

    assert recompute_streaming_rail() is True

    stored = json.loads(app.redis.get(RAIL_KEY.format(user_id=user_id)))
    assert stored["computed_at"]
    assert stored["items"][0]["title"] == "Rail Comedy"


def test_recompute_creates_records_for_recordless_rail_films(app, monkeypatch):
    """Rail films found on TMDb alone get review-only records and the
    standard refresh enqueued, so the shared score source can estimate
    their tiles like any catalogued film's — instead of /movie_states
    answering their tmdb ids with the empty state."""

    from app.models import Movie
    from app.streaming_rail import recompute_streaming_rail

    user_id = subscribe(app, 8, "Netflix")
    plant_profile(app, user_id, COMEDY_PROFILE)
    install_rail_fakes(app, monkeypatch)
    plant_availability(app, 5001, [NETFLIX])
    plant_availability(app, 5002, [NETFLIX])
    plant_availability(app, 5005, [])

    assert recompute_streaming_rail() is True

    with app.app_context():
        comedy = Movie.query.filter_by(tmdb_id=5001).first()
        drama = Movie.query.filter_by(tmdb_id=5002).first()
        assert comedy is not None and comedy.title == "Rail Comedy"
        assert comedy.year == 1994
        assert drama is not None and drama.year == 1953
        assert comedy.files.count() == 0

        # Both records await the refresh that stamps tmdb_data_as_of

        refresh_targets = {
            job.args[1]
            for job in app.maintenance_queue.jobs
            if job.func_name == "app.videos.refresh_tmdb_info"
        }
        assert {comedy.id, drama.id} <= refresh_targets

    # A second run reuses the records instead of duplicating them

    assert recompute_streaming_rail() is True
    with app.app_context():
        assert Movie.query.filter_by(tmdb_id=5001).count() == 1


def test_landing_page_renders_the_rail(app, admin_client):
    from app import db
    from app.models import UserMovieReview, User
    from app.streaming_rail import RAIL_KEY
    from app.videos import star_rating_fields

    with app.app_context():
        user_id = User.query.filter_by(admin=True).first().id

        # One rail film has been acquired since the nightly run and one
        # was logged since; both must drop out of the display

        acquired = make_movie("Rail Acquired Since", 1994, tmdb_id=6002)
        make_movie_file(acquired, "Bluray-1080p")
        logged = make_movie("Rail Logged Since", 1994, tmdb_id=6003)
        db.session.add(
            UserMovieReview(
                user_id=user_id, movie_id=logged.id, **star_rating_fields(4.0)
            )
        )
        db.session.commit()

    app.redis.set(
        RAIL_KEY.format(user_id=user_id),
        json.dumps(
            {
                "computed_at": "2026-08-12 02:15",
                "items": [
                    {
                        "tmdb_id": 6001,
                        "title": "Rail Showpiece",
                        "year": "1994",
                        "poster_path": "/rail.jpg",
                        "runtime": 95,
                        "providers": [{**NETFLIX, "kind": "flatrate"}],
                        "because": ["popular on Netflix", "Comedy"],
                        "score": 1.0,
                    },
                    {
                        "tmdb_id": 6002,
                        "title": "Rail Acquired Since",
                        "year": "1994",
                        "poster_path": None,
                        "runtime": 90,
                        "providers": [{**NETFLIX, "kind": "flatrate"}],
                        "because": ["popular on Netflix"],
                        "score": 0.9,
                    },
                    {
                        "tmdb_id": 6003,
                        "title": "Rail Logged Since",
                        "year": "1994",
                        "poster_path": None,
                        "runtime": 90,
                        "providers": [{**NETFLIX, "kind": "flatrate"}],
                        "because": ["popular on Netflix"],
                        "score": 0.8,
                    },
                ],
            }
        ),
    )

    body = admin_client.get("/").get_data(as_text=True)
    assert "Streaming on your services" in body
    assert "Rail Showpiece (1994)" in body
    assert "/review/tmdb/6001" in body

    # The provider badge moved into the popover (Aug 2026); the tile
    # keeps the actions, and the taste reason rides the anchor as a
    # card label

    assert 'title="Streaming on Netflix"' not in body
    assert 'data-state-tmdb="6001"' in body
    assert 'data-card-reasons=\'["popular on Netflix", "Comedy"]\'' in body
    assert "Streaming data by JustWatch" in body
    assert "last run 2026-08-12 02:15" in body
    assert "Rail Acquired Since" not in body
    assert "Rail Logged Since" not in body


def test_landing_page_requests_rail_compute_once(app, admin_client):
    user_id = subscribe(app, 8, "Netflix")
    plant_profile(app, user_id, COMEDY_PROFILE)

    for _ in range(2):
        admin_client.get("/")

    rail_jobs = [
        job
        for job in app.maintenance_queue.jobs
        if job.func_name == "app.streaming_rail.recompute_streaming_rail"
    ]
    assert len(rail_jobs) == 1


def test_no_rail_section_without_provider_picks(app, admin_client):
    body = admin_client.get("/").get_data(as_text=True)
    assert "Streaming on your services" not in body
    assert app.maintenance_queue.jobs == []


def test_runtime_filter_trims_the_streaming_rail(app, admin_client):
    """The minute limit filters stored rail items by their enriched
    runtime; zero means unknown and hides only from filtered views."""

    from app.models import User
    from app.streaming_rail import RAIL_KEY

    with app.app_context():
        user_id = User.query.filter_by(admin=True).first().id

    def rail_item(tmdb_id, title, runtime):
        """A minimal stored rail entry."""

        return {
            "tmdb_id": tmdb_id,
            "title": title,
            "year": "1994",
            "poster_path": None,
            "runtime": runtime,
            "providers": [{**NETFLIX, "kind": "flatrate"}],
            "because": ["popular on Netflix"],
            "score": 1.0,
        }

    app.redis.set(
        RAIL_KEY.format(user_id=user_id),
        json.dumps(
            {
                "computed_at": "2026-08-12 02:15",
                "items": [
                    rail_item(7001, "Rail Filter Short", 95),
                    rail_item(7002, "Rail Filter Long", 200),
                    rail_item(7003, "Rail Filter Unknown", 0),
                ],
            }
        ),
    )

    body = admin_client.get("/?minutes=100").get_data(as_text=True)
    assert "Rail Filter Short (1994)" in body
    assert "Rail Filter Long" not in body
    assert "Rail Filter Unknown" not in body

    body = admin_client.get("/").get_data(as_text=True)
    assert "Rail Filter Short (1994)" in body
    assert "Rail Filter Long (1994)" in body
    assert "Rail Filter Unknown (1994)" in body
