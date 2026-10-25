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
    from app.streaming_rail import _payload_features

    def score(redis, user_id, tmdb_id, profile):
        """Score a Western payload 1.0 and each different payload 0.0."""

        features = _payload_features(pc.enriched_movie(tmdb_id) or {})
        return 1.0 if any(label == "Western" for _, _, label in features) else 0.0

    monkeypatch.setattr(pc, "resolved_tmdb_score", score)
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


def test_the_bar_reads_the_shared_score(app, monkeypatch):
    """The shared score adds co-preference. The bar must read that score.

    The profile leans to Western, so a Drama scores 0 on taste only. The
    shared source has a higher score for the film. The film passes."""

    import app.provider_catalog as pc

    from app.models import Movie
    from app.recommendations import TMDB_PATCH_SCORES_KEY

    user_id = subscribe_criterion(app)
    monkeypatch.setattr(
        pc, "estimated_rating", lambda profile, score: 5.0 if score > 0 else 2.0
    )
    payloads = {205: shelf_item(205, "Catalog Copref Drama", genre=(18, "Drama"))}
    monkeypatch.setattr(pc, "enriched_movie", lambda tmdb_id: payloads.get(tmdb_id))
    plant_availability(
        app,
        205,
        {"link": None, "flatrate": [CRITERION], "ads": [], "rent": [], "buy": []},
    )
    app.redis.hset(TMDB_PATCH_SCORES_KEY.format(user_id=user_id), "205", 0.6)
    app.redis.sadd(pc.PENDING_KEY, 205)

    class FakeQueue:
        def enqueue(self, *args, **kwargs):
            pass

    with app.app_context():
        monkeypatch.setattr(app, "maintenance_queue", FakeQueue())
        assert pc._process_pending({258}) == 1
        assert Movie.query.filter_by(tmdb_id=205).first() is not None


def test_an_unknown_answer_keeps_the_id_pending(app, monkeypatch):
    """A TMDB failure must not drop a discovery for good (#304)."""

    import app.provider_catalog as pc

    subscribe_criterion(app)
    scorer_that_likes_westerns(monkeypatch)
    streams = {"link": None, "flatrate": [CRITERION], "ads": [], "rent": [], "buy": []}
    rental = {"link": None, "flatrate": [], "ads": [], "rent": [CRITERION], "buy": []}

    # 301 has no availability answer. 302 streams, but its payload fetch
    # fails. 303 is a verified rental, a real negative.

    monkeypatch.setattr(
        pc,
        "batch_title_availability",
        lambda ids: ({301: None, 302: streams, 303: rental}, []),
    )
    monkeypatch.setattr(pc, "enriched_movie", lambda tmdb_id: None)
    monkeypatch.setattr(pc, "movie_gone", lambda tmdb_id: False)
    app.redis.sadd(pc.PENDING_KEY, 301, 302, 303)

    with app.app_context():
        assert pc._process_pending({258}) == 0
    assert sorted(int(m) for m in app.redis.smembers(pc.PENDING_KEY)) == [301, 302]

    # With no taste profile yet, the streamable ids wait too.

    app.redis.delete(pc.PENDING_KEY)
    app.redis.sadd(pc.PENDING_KEY, 302)
    monkeypatch.setattr(pc, "stored_profile", lambda redis, user_id: None)
    with app.app_context():
        assert pc._process_pending({258}) == 0
    assert sorted(int(m) for m in app.redis.smembers(pc.PENDING_KEY)) == [302]
