"""Test the provider-catalog discovery (#250).

The tests cover the page-capped enumeration, the ever-seen diff, the
per-title streaming verification, the profile scoring, and the bounded
record creation."""

from tests.factories import make_movie
from tests.test_leaving_criterion import (
    FakeResponse,
    shelf_item,
    subscribe_criterion,
)
from tests.test_streaming import plant_availability

CRITERION = {
    "provider_id": 258,
    "provider_name": "Criterion Channel",
    "logo_path": None,
}


def catalog_page(ids, total_pages):
    return FakeResponse(
        payload={"results": [{"id": i} for i in ids], "total_pages": total_pages}
    )


def scorer_that_likes_westerns(monkeypatch):
    """Patch the scoring pair for the tests.

    A Western payload estimates 5 stars. Each different payload
    estimates 2 stars. Thus, the test covers the bar logic without the
    real curve of the engine."""

    import app.provider_catalog as pc

    monkeypatch.setattr(
        pc,
        "score_movie",
        lambda features, profile: (
            1.0 if any(label == "Western" for _, _, label in features) else 0.0,
            [],
        ),
    )
    monkeypatch.setattr(
        pc, "estimated_rating", lambda profile, score: 5.0 if score > 0 else 2.0
    )


def test_enumeration_plants_then_queues_only_new(app, monkeypatch):
    import app.provider_catalog as pc

    from app import db
    from app.models import UserStreamingProvider

    user_id = subscribe_criterion(app)
    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")

    # A rental storefront is on the subscription list because it lights
    # the rent badges. Fitzflix must never enumerate it. It enumerates
    # flat-rate services only

    with app.app_context():
        db.session.add(
            UserStreamingProvider(user_id=user_id, provider_id=2, name="Apple TV Store")
        )
        db.session.commit()

    pages = [[101, 102], [103]]
    providers_queried = set()

    def fake_tmdb_get(url, params=None, **kwargs):
        providers_queried.add(params.get("with_watch_providers"))
        assert params.get("with_watch_monetization_types") == "flatrate"
        page = params.get("page")
        if page <= len(pages):
            return catalog_page(pages[page - 1], total_pages=len(pages))
        return catalog_page([], total_pages=len(pages))

    monkeypatch.setattr(pc, "tmdb_get", fake_tmdb_get)
    monkeypatch.setattr(pc, "_process_pending", lambda subscribed: 0)

    # The first run plants the ever-seen set. It queues nothing. A film
    # that was already in the catalog is not a discovery

    assert pc.refresh_provider_catalogs() is True
    assert providers_queried == {"258"}
    seen_key = pc.SEEN_KEY.format(provider_id=258)
    assert {int(x) for x in app.redis.smembers(seen_key)} == {101, 102, 103}
    assert app.redis.scard(pc.PENDING_KEY) == 0

    # A later run queues only the id that Fitzflix has not seen before.
    # The ever-seen set continues to grow

    pages[1] = [103, 104]
    pc.refresh_provider_catalogs()
    assert {int(x) for x in app.redis.smembers(pc.PENDING_KEY)} == {104}
    assert {int(x) for x in app.redis.smembers(seen_key)} == {101, 102, 103, 104}

    # A film that leaves the popularity window and returns cannot queue
    # again

    pages[1] = [103]
    pc.refresh_provider_catalogs()
    pages[1] = [103, 104]
    app.redis.delete(pc.PENDING_KEY)
    pc.refresh_provider_catalogs()
    assert app.redis.scard(pc.PENDING_KEY) == 0


def test_processing_verifies_scores_and_creates(app, monkeypatch):
    import app.provider_catalog as pc

    from app import db
    from app.models import Movie

    subscribe_criterion(app)
    scorer_that_likes_westerns(monkeypatch)

    payloads = {
        201: shelf_item(201, "Catalog Western"),
        202: shelf_item(202, "Catalog Dull", genre=(18, "Drama")),
        203: shelf_item(203, "Catalog Rental"),
    }
    monkeypatch.setattr(pc, "enriched_movie", lambda tmdb_id: payloads.get(tmdb_id))

    # Film 201 streams on the subscribed service. Film 202 streams too,
    # but it scores low. Film 203 is rent-only. That is discover
    # cross-contamination. The per-title verification drops it. Film 204
    # already has a record

    plant_availability(
        app,
        201,
        {"link": None, "flatrate": [CRITERION], "ads": [], "rent": [], "buy": []},
    )
    plant_availability(
        app,
        202,
        {"link": None, "flatrate": [CRITERION], "ads": [], "rent": [], "buy": []},
    )
    plant_availability(
        app,
        203,
        {"link": None, "flatrate": [], "ads": [], "rent": [CRITERION], "buy": []},
    )
    with app.app_context():
        make_movie("Already Here", 1956, tmdb_id=204)
        db.session.commit()

    app.redis.sadd(pc.PENDING_KEY, 201, 202, 203, 204)

    refreshes = []

    class FakeQueue:
        def enqueue(self, *args, **kwargs):
            refreshes.append((args, kwargs))

    with app.app_context():
        monkeypatch.setattr(app, "maintenance_queue", FakeQueue())
        created = pc._process_pending({258})

    assert created == 1
    with app.app_context():
        movie = Movie.query.filter_by(tmdb_id=201).first()
        assert movie is not None
        assert movie.title == "Catalog Western"
        assert movie.year == 1956
        assert not movie.files.count()
        assert Movie.query.filter_by(tmdb_id=202).first() is None
        assert Movie.query.filter_by(tmdb_id=203).first() is None

    # Fitzflix enqueues 1 standard TMDB refresh for the created record.
    # It consumes the batch. It drops the rejects permanently

    assert len(refreshes) == 1
    assert refreshes[0][0][0] == "app.videos.refresh_tmdb_info"
    assert app.redis.scard(pc.PENDING_KEY) == 0


def test_creation_cap_pushes_verified_films_back(app, monkeypatch):
    import app.provider_catalog as pc

    from app.models import Movie

    subscribe_criterion(app)
    scorer_that_likes_westerns(monkeypatch)
    monkeypatch.setattr(pc, "CREATE_CAP", 1)

    payloads = {
        301: shelf_item(301, "Catalog First"),
        302: shelf_item(302, "Catalog Second"),
    }
    monkeypatch.setattr(pc, "enriched_movie", lambda tmdb_id: payloads.get(tmdb_id))
    for tmdb_id in (301, 302):
        plant_availability(
            app,
            tmdb_id,
            {"link": None, "flatrate": [CRITERION], "ads": [], "rent": [], "buy": []},
        )
    app.redis.sadd(pc.PENDING_KEY, 301, 302)

    class FakeQueue:
        def enqueue(self, *args, **kwargs):
            pass

    with app.app_context():
        monkeypatch.setattr(app, "maintenance_queue", FakeQueue())
        created = pc._process_pending({258})

    # One record made the cap. The second verified film waits in the
    # pending queue for the next run. Fitzflix does not drop it

    assert created == 1
    assert app.redis.scard(pc.PENDING_KEY) == 1
    with app.app_context():
        assert Movie.query.filter(Movie.tmdb_id.in_([301, 302])).count() == 1
