"""Discover films for the recommendation universe from the provider catalogs (#250).

The engine can recommend only the films that have records. The
subscribed streaming services are a discovery source that the engine
never saw. A nightly task enumerates the catalog of each subscribed
provider through the discover endpoint of TMDB. The result is in
popularity order, with a page cap. The Apple storefront alone lists
approximately 38000 films. Thus, the task cuts the big catalogs to
their popular slice by design. The task compares the ids with a
cumulative set of all ids seen for each provider. Thus, changes at the
popularity boundary cannot flag a film a second time. The first run
only plants the set. The task queues the ids that are really new for
processing.

Flat-rate streaming only, no rentals (Glenn, 2026-08-27): the task
never enumerates the rental and purchase storefronts on the
subscription list. Those rows exist to show the rent badges. The
discover queries ask only for flatrate monetization. The per-title
verification accepts only the streaming buckets. Each run processes a
batch of limited size from the pending queue. The provider and
monetization filters of discover contaminate each other. The
streaming rail found this first. Thus, the task verifies each
candidate against the per-title watch-provider cache before all other
steps. A film must really stream on a service that some user
subscribes to. The task enriches the films that pass and scores them
against each stored taste profile. If the best estimated rating of a
film is above the bar, the film becomes a movie record without a
file. The record goes through the same find_or_create_tmdb_movie
function that the review and Criterion-catalog paths use. The task
enqueues the standard TMDB refresh after it.

From there, the existing machinery does the rest, without changes.
The refresh sets tmdb_data_as_of. This makes the record scoreable.
The 01:45 recompute scores it for each user. The availability cache
makes it eligible. This task fetches the cache at verification, and
the nightly refresh updates it. The shelves of the Recommendations
page suggest the film where its features fit. Films that fail the
verification, or that score below the bar, are removed permanently.
Profiles change over time. But a second evaluation of each rejected
film, for ever, would grow without limit.
"""

import traceback

from flask import current_app
from werkzeug.local import LocalProxy

from app import db, get_app
from app.models import CatalogExclusion, Movie, User, UserStreamingProvider, tmdb_get
from app.recommendations import estimated_rating, score_movie, stored_profile
from app.streaming import batch_title_availability, streaming_matches
from app.streaming_rail import _payload_features, enriched_movie

# This is the app instance of this process. Fitzflix resolves it lazily.
# Thus, the nightly task can run on a worker without a second application

app = LocalProxy(get_app)

SEEN_KEY = "fitzflix:catalog:seen:{provider_id}"
PENDING_KEY = "fitzflix:catalog:pending"

# This is the depth of the enumeration of 1 provider. 200 pages is the
# full catalog for each current subscription, except the Apple
# storefront. The popular slice of that storefront is sufficient. The
# vote floor removes the low-quality films and keeps the page counts
# correct. A niche new film below the floor reaches the engine when a
# user logs it or puts it on a watchlist. The Criterion scrape (#246)
# stays the fresh path for that shelf

PAGE_CAP = 200
VOTE_FLOOR = 20

# These are the rental and purchase storefronts. They are TMDb providers
# that sell or rent films. They do not stream on a subscription. They
# are Apple TV, Google Play, Fandango at Home, Amazon Video, Microsoft
# Store, and YouTube. A subscription row for one of them exists to show
# the rent badges. Thus, this task never enumerates their catalogs
# (Glenn, 2026-08-27: flat-rate services only, no rentals). The
# per-title verification would reject their films in all cases. But to
# skip them saves the enumeration and the churn of the pending queue

STOREFRONT_PROVIDER_IDS = frozenset({2, 3, 7, 10, 68, 192})

# These are the limits per run. The first is the number of candidates
# processed. The availability and the enrichment are 1 request each.
# Thus, this limit caps the burst. The second is the number of records
# created. This also caps the growth of the nightly refresh and
# recompute loads. The third is the estimated-stars bar. A film must be
# above the bar for some user before it gets a record

PROCESS_CAP = 200
CREATE_CAP = 50
MIN_ESTIMATE = 3.0


def _catalog_ids(provider_id):
    """Return the streamable catalog of the provider as tmdb ids.

    The ids are in popularity order. This function reads the discover
    pages up to PAGE_CAP. After a failure, it returns the ids that it
    collected before the failure. The comparison with the set of all
    ids seen makes a short read safe. The run only discovers less
    tonight."""

    ids = []
    page = 1
    while page <= PAGE_CAP:
        try:
            r = tmdb_get(
                current_app.config["TMDB_API_URL"] + "/discover/movie",
                params={
                    "api_key": current_app.config["TMDB_API_KEY"],
                    "watch_region": "US",
                    "with_watch_providers": str(provider_id),
                    "with_watch_monetization_types": "flatrate",
                    "include_adult": "false",
                    "vote_count.gte": VOTE_FLOOR,
                    "page": page,
                },
                timeout=10,
            )
            r.raise_for_status()
            body = r.json() or {}
        except Exception:
            current_app.logger.warning(traceback.format_exc())
            break
        ids.extend(item["id"] for item in body.get("results") or [] if item.get("id"))
        if page >= (body.get("total_pages") or 0):
            break
        page += 1
    return ids


def refresh_provider_catalogs():
    """Enumerate the catalog of each subscribed provider each night.

    This task queues the ids that it never saw before. Then it
    processes a batch of limited size from the pending queue into movie
    records."""

    with app.app_context():
        if not current_app.config["TMDB_API_KEY"]:
            return True
        redis = current_app.redis
        provider_ids = sorted(
            {
                provider_id
                for (provider_id,) in db.session.query(
                    UserStreamingProvider.provider_id
                ).distinct()
            }
            - STOREFRONT_PROVIDER_IDS
        )

        discovered = 0
        for provider_id in provider_ids:
            ids = _catalog_ids(provider_id)
            if not ids:
                current_app.logger.warning(
                    f"Provider catalog: nothing enumerated for provider {provider_id}"
                )
                continue
            seen_key = SEEN_KEY.format(provider_id=provider_id)
            planted = bool(redis.exists(seen_key))
            seen = {int(member) for member in redis.smembers(seen_key)}
            fresh = sorted({tmdb_id for tmdb_id in ids if tmdb_id not in seen})
            if fresh:
                redis.sadd(seen_key, *fresh)
                # The first enumeration only plants the set. A film that
                # was already in the catalog is not a discovery
                if planted:
                    redis.sadd(PENDING_KEY, *fresh)
                    discovered += len(fresh)
            current_app.logger.info(
                f"Provider catalog: provider {provider_id} enumerated "
                f"{len(ids)} films"
                + (f", {len(fresh)} new" if planted else " (planted)")
            )

        created = _process_pending(set(provider_ids))
        current_app.logger.info(
            f"Provider catalog: {discovered} film(s) newly discovered, "
            f"{created} record(s) created, "
            f"{redis.scard(PENDING_KEY)} pending"
        )
        return True


def _process_pending(subscribed):
    """Verify and score 1 batch of pending ids and make movie records.

    The number of records is capped. This function returns the number
    of records created."""

    # The TMDB record code stays in app.videos. The import is lazy. Thus,
    # the module import direction stays one-way

    from app.videos import find_or_create_tmdb_movie

    redis = current_app.redis
    popped = redis.spop(PENDING_KEY, PROCESS_CAP)
    ids = sorted(int(member) for member in popped or [])
    if not ids or not subscribed:
        return 0

    existing = {
        tmdb_id
        for (tmdb_id,) in db.session.query(Movie.tmdb_id).filter(Movie.tmdb_id.in_(ids))
    }
    excluded = {tmdb_id for (tmdb_id,) in db.session.query(CatalogExclusion.tmdb_id)}
    candidates = [
        tmdb_id
        for tmdb_id in ids
        if tmdb_id not in existing and tmdb_id not in excluded
    ]

    # Discover said that these films stream on a subscribed provider. The
    # per-title payload is the truth. The fetch fills the same cache that
    # the eligibility check of the shelves reads. Thus, a created record
    # is answerable immediately

    availability, _ = batch_title_availability(candidates)
    streamable = [
        tmdb_id
        for tmdb_id in candidates
        if streaming_matches(availability.get(tmdb_id), subscribed, tmdb_id=tmdb_id)
    ]

    profiles = []
    for (user_id,) in db.session.query(User.id):
        profile = stored_profile(redis, user_id)
        if profile:
            profiles.append(profile)
    if not profiles:
        return 0

    to_refresh = []
    created = 0
    for tmdb_id in streamable:
        if created >= CREATE_CAP:
            # The cap protects the refresh queue and the nightly loads.
            # The verified films above the cap wait for the next run
            redis.sadd(PENDING_KEY, tmdb_id)
            continue
        payload = enriched_movie(tmdb_id)
        if (
            not payload
            or not payload.get("title")
            or not str(payload.get("year") or "").isdigit()
        ):
            continue
        features = _payload_features(payload)
        best = None
        for profile in profiles:
            score, _ = score_movie(features, profile)
            estimate = estimated_rating(profile, score)
            if estimate is not None and (best is None or estimate > best):
                best = estimate
        if best is None or best < MIN_ESTIMATE:
            continue
        movie, was_created = find_or_create_tmdb_movie(
            tmdb_id, payload["title"], int(payload["year"])
        )
        created += was_created
        if movie.tmdb_data_as_of is None:
            to_refresh.append(movie)
    db.session.commit()

    for movie in to_refresh:
        current_app.maintenance_queue.enqueue(
            "app.videos.refresh_tmdb_info",
            args=("Movies", movie.id, movie.tmdb_id),
            job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
            description=(f"Refreshing TMDB data for '{movie.title} ({movie.year})'"),
        )
    return created
