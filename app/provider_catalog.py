"""Provider-catalog discovery for the recommendation universe (#250).

The engine can only recommend films it has records for; subscribed
streaming services are a discovery source it never saw. A nightly task
enumerates each subscribed provider's catalog through TMDB's discover
endpoint (popularity-ordered, page-capped — Apple's storefront alone
lists ~38k films, so big catalogs are deliberately truncated to their
popular slice), diffs the ids against a cumulative ever-seen set per
provider (so the popularity boundary's churn can't re-flag films; the
first run only plants), and queues genuinely new ids for processing.

Flat-rate streaming only, no rentals (Glenn, Aug 27 2026): rental
and purchase storefronts on the subscription list (they exist to
light the rent badges) are never enumerated, the discover queries ask
for flatrate monetization alone, and the per-title verification
accepts only the streaming buckets. Each run processes a bounded
batch from the pending queue. Discover's provider and monetization
filters demonstrably cross-contaminate (the streaming rail learned
this first), so every candidate is verified against the per-title
watch-provider cache before anything else — a film must actually
stream on a service some user subscribes to. Survivors are enriched and scored against every
stored taste profile, and a film whose best estimated rating clears
the bar becomes a file-less movie record through the same
find_or_create_tmdb_movie door the review and Criterion-catalog paths
use, with the standard TMDB refresh enqueued behind it.

From there the existing machinery does the rest, unmodified: the
refresh stamps tmdb_data_as_of, which makes the record scoreable; the
1:45 recompute scores it for every user; the availability cache
(fetched here at verification, refreshed nightly) makes it eligible;
and the Recommendations page's shelves suggest it wherever its
features fit. Films that fail verification or score below the bar are
dropped for good — profiles drift, but re-evaluating every reject
forever would grow without bound.
"""

import traceback

from flask import current_app
from werkzeug.local import LocalProxy

from app import db, get_app
from app.models import CatalogExclusion, Movie, User, UserStreamingProvider, tmdb_get
from app.recommendations import estimated_rating, score_movie, stored_profile
from app.streaming import batch_title_availability, streaming_matches
from app.streaming_rail import _payload_features, enriched_movie

# This process's app instance, resolved lazily so the nightly task can
# run on a worker without building a second application

app = LocalProxy(get_app)

SEEN_KEY = "fitzflix:catalog:seen:{provider_id}"
PENDING_KEY = "fitzflix:catalog:pending"

# How deep one provider's enumeration digs: 200 pages is the whole
# catalog for every current subscription except Apple's storefront,
# whose popular slice is plenty. The vote floor trims shovelware and
# keeps the page counts honest; niche new arrivals below it reach the
# engine anyway once anyone logs or watchlists them, and Criterion's
# own scrape (#246) stays the fresh path for that shelf

PAGE_CAP = 200
VOTE_FLOOR = 20

# Rental/purchase storefronts: TMDb providers that sell or rent
# rather than stream on a subscription — Apple TV, Google Play,
# Fandango at Home, Amazon Video, Microsoft Store, YouTube. A
# subscription row for one exists to light the rent badges, so their
# catalogs are never enumerated here (Glenn, Aug 27 2026: flat-rate
# services only, no rentals); the per-title verification would reject
# their films anyway, but skipping them saves the enumeration and the
# pending-queue churn

STOREFRONT_PROVIDER_IDS = frozenset({2, 3, 7, 10, 68, 192})

# Per-run bounds: candidates processed (availability + enrichment are
# one request each, so this caps the burst), records created (which
# also caps how fast the nightly refresh and recompute loads grow),
# and the estimated-stars bar a film must clear for some user before
# it earns a record

PROCESS_CAP = 200
CREATE_CAP = 50
MIN_ESTIMATE = 3.0


def _catalog_ids(provider_id):
    """The provider's streamable catalog as popularity-ordered tmdb
    ids, paginating discover up to PAGE_CAP; whatever was gathered
    before a failure (the diff against ever-seen makes a short read
    safe — it just discovers less tonight)."""

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
    """Nightly task: enumerate every subscribed provider's catalog,
    queue the ids never seen before, and process a bounded batch of
    the pending queue into movie records."""

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
                # The first enumeration only plants: what was already
                # in the catalog isn't a discovery
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
    """Verify, score, and (capped) turn one batch of pending ids into
    movie records; the number of records created."""

    # TMDB record plumbing stays in app.videos; lazy so the module
    # import direction stays one-way

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

    # Discover said these films stream on a subscribed provider; the
    # per-title payload is the truth. The fetch primes the same cache
    # the shelves' eligibility check reads, so a created record is
    # answerable immediately

    availability, _ = batch_title_availability(candidates)
    streamable = [
        tmdb_id
        for tmdb_id in candidates
        if streaming_matches(availability.get(tmdb_id), subscribed)
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
            # The cap protects the refresh queue and the nightly
            # loads; verified films past it wait for the next run
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
