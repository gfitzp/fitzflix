"""Streaming availability via TMDB's watch-provider endpoints.

The underlying data is licensed from JustWatch, and TMDB's terms make
attribution mandatory: every surface that shows it must carry a
"Streaming data by JustWatch" credit, or API access can be revoked.
Availability is cached per title, refreshed nightly by the
refresh_availability cron and held for two days so a missed night
doesn't go cold (before Aug 2026 the day-cached entries expired in a
cluster and the first page view of the day stalled behind 50 inline
fetches under the rate limiter), and displays are customized to each
user's chosen services —
a per-user Profile setting, never site-wide. The payload carries no
deep links; the only outbound link is the film's TMDB watch page.
"""

import json
import traceback

from concurrent.futures import ThreadPoolExecutor

import requests

from flask import current_app
from werkzeug.local import LocalProxy

from app import db, get_app
from app.models import Movie, tmdb_get

# This process's app instance, resolved lazily so the warm task can run
# on a worker without building a second application

app = LocalProxy(get_app)

WATCH_REGION = "US"
CACHE_SECONDS = 2 * 86400
REFRESH_WORKERS = 20
REGISTRY_KEY = "fitzflix:tmdb:watch-providers:registry"
AVAILABILITY_KEY = "fitzflix:tmdb:watch-providers:movie:{tmdb_id}"


def provider_registry():
    """US movie watch providers from TMDB's registry, sorted by display
    priority and cached for a day; [] without an API key or when TMDB
    is unreachable."""

    if not current_app.config["TMDB_API_KEY"]:
        return []
    cached = current_app.redis.get(REGISTRY_KEY)
    if cached:
        return json.loads(cached)
    try:
        r = tmdb_get(
            current_app.config["TMDB_API_URL"] + "/watch/providers/movie",
            params={
                "api_key": current_app.config["TMDB_API_KEY"],
                "watch_region": WATCH_REGION,
            },
            timeout=10,
        )
        r.raise_for_status()
        results = r.json().get("results") or []
    except Exception:
        current_app.logger.warning(traceback.format_exc())
        return []

    providers = [
        {
            "provider_id": p.get("provider_id"),
            "provider_name": p.get("provider_name"),
            "logo_path": p.get("logo_path"),
            "display_priority": (p.get("display_priorities") or {}).get(
                WATCH_REGION, p.get("display_priority", 999)
            ),
        }
        for p in results
        if p.get("provider_id") is not None
    ]
    providers.sort(key=lambda p: p["display_priority"])
    current_app.redis.set(REGISTRY_KEY, json.dumps(providers), ex=CACHE_SECONDS)
    return providers


def title_availability(tmdb_id, refresh=False):
    """The film's US watch-provider payload {link, flatrate, ads, rent,
    buy}, cached for CACHE_SECONDS; None while unknown (no key, TMDB
    down). refresh skips the cache read — the nightly task's way of
    re-fetching a title whose entry is still live.

    A film with no US providers caches an empty payload, so absence
    doesn't re-query TMDB on every page view.
    """

    if tmdb_id is None or not current_app.config["TMDB_API_KEY"]:
        return None
    cache_key = AVAILABILITY_KEY.format(tmdb_id=int(tmdb_id))
    cached = None if refresh else current_app.redis.get(cache_key)
    if cached:
        return json.loads(cached)
    try:
        r = tmdb_get(
            current_app.config["TMDB_API_URL"]
            + f"/movie/{int(tmdb_id)}/watch/providers",
            params={"api_key": current_app.config["TMDB_API_KEY"]},
            timeout=10,
        )
        r.raise_for_status()
        region = (r.json().get("results") or {}).get(WATCH_REGION) or {}
    except requests.exceptions.HTTPError as e:
        # A 404 is TMDB's answer, not an outage — the id has no watch
        # record (a stale or wrong tmdb id) — so cache it as empty
        # rather than re-querying on every page view

        if e.response is not None and e.response.status_code == 404:
            region = {}
        else:
            current_app.logger.warning(traceback.format_exc())
            return None
    except Exception:
        current_app.logger.warning(traceback.format_exc())
        return None

    payload = {"link": region.get("link")}
    for kind in ("flatrate", "ads", "rent", "buy"):
        payload[kind] = [
            {
                "provider_id": p.get("provider_id"),
                "provider_name": p.get("provider_name"),
                "logo_path": p.get("logo_path"),
            }
            for p in region.get(kind) or []
        ]
    current_app.redis.set(cache_key, json.dumps(payload), ex=CACHE_SECONDS)
    return payload


def batch_title_availability(
    tmdb_ids, max_workers=REFRESH_WORKERS, fetch_limit=None, refresh=False
):
    """(payloads, deferred): availability for many titles at once, as
    {tmdb_id: payload-or-None} plus the ids that weren't fetched.

    Cache hits are read in one MGET; misses fetch concurrently, each
    through title_availability so caching and 404 handling match the
    single-title path. All fetches share tmdb_get's app-wide rate
    limiter, so fetch_limit bounds how long a render can stall behind
    it — leftover ids come back for the caller to warm in the
    background instead. The page renders pass fetch_limit=0 (Aug 2026):
    they answer from the cache the nightly refresh keeps full and never
    fetch inline. refresh re-fetches every id, cached or not."""

    results = {}
    ids = sorted({int(t) for t in tmdb_ids if t is not None})
    if not ids:
        return results, []
    if refresh:
        misses = ids
    else:
        misses = []
        cached = current_app.redis.mget(
            [AVAILABILITY_KEY.format(tmdb_id=tmdb_id) for tmdb_id in ids]
        )
        for tmdb_id, payload in zip(ids, cached):
            if payload:
                results[tmdb_id] = json.loads(payload)
            else:
                misses.append(tmdb_id)
    if not misses or not current_app.config["TMDB_API_KEY"]:
        return results, []

    deferred = []
    if fetch_limit is not None:
        misses, deferred = misses[:fetch_limit], misses[fetch_limit:]
    if not misses:
        return results, deferred

    flask_app = current_app._get_current_object()

    def fetch(tmdb_id):
        """One title's availability under its own app context."""

        with flask_app.app_context():
            return tmdb_id, title_availability(tmdb_id, refresh=refresh)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for tmdb_id, payload in pool.map(fetch, misses):
            results[tmdb_id] = payload
    return results, deferred


def warm_title_availability(tmdb_ids):
    """Background task: fill the availability cache for the given
    titles — the ids a page render found uncached — so the next visit
    has every badge and count without waiting."""

    with app.app_context():
        batch_title_availability(tmdb_ids)
        return True


def refresh_availability():
    """Nightly task (Aug 2026): re-fetch availability for every film
    with a TMDB id, so the pages that read it — the watchlist, the
    Criterion catalog, filmographies — always answer from a full cache
    and never block on TMDB. Whole-library cost is a few thousand
    requests, minutes under the rate limiter, and each entry's two-day
    TTL restarts, so one missed night still serves yesterday's data."""

    with app.app_context():
        if not current_app.config["TMDB_API_KEY"]:
            return True
        tmdb_ids = [
            tmdb_id
            for (tmdb_id,) in db.session.query(Movie.tmdb_id)
            .filter(Movie.tmdb_id.isnot(None))
            .distinct()
        ]
        results, _ = batch_title_availability(tmdb_ids, refresh=True)
        fetched = sum(1 for payload in results.values() if payload is not None)
        current_app.logger.info(
            f"Streaming availability refresh: {fetched} of {len(tmdb_ids)} films fetched"
        )
        return True


def user_provider_ids(user):
    """The TMDB provider ids of the user's chosen services."""

    return {row.provider_id for row in user.streaming_providers}


def streaming_matches(availability, provider_ids, tmdb_id=None):
    """Providers carrying the film that the user subscribes to —
    streaming kinds only (flatrate, then free-with-ads); rent and buy
    aren't subscriptions. Given the film's tmdb_id, a Criterion
    Channel match also learns whether the film is on the month's
    leaving set: its "leaving" key carries the departure date ("August
    31") so the badge can light up wherever it renders (Glenn's ask,
    Aug 2026 — watch it before it goes)."""

    if not availability or not provider_ids:
        return []
    matches = []
    seen = set()
    for kind in ("flatrate", "ads"):
        for provider in availability.get(kind) or []:
            if provider["provider_id"] in provider_ids:
                if provider["provider_id"] not in seen:
                    seen.add(provider["provider_id"])
                    matches.append({**provider, "kind": kind})
    if tmdb_id is not None:
        # Imported here: leaving_criterion and newly_added reach this
        # module through streaming_rail, so top-level imports would be
        # circular
        from app.leaving_criterion import CRITERION_PROVIDER_ID, leaving_departure
        from app.newly_added import newly_added_since

        for match in matches:
            if match["provider_id"] == CRITERION_PROVIDER_ID:
                departs = leaving_departure(tmdb_id)
                if departs:
                    match["leaving"] = departs
            # The newly-added feeds (#246) mark a recent arrival on
            # any provider's badge; departure urgency outranks it on
            # the odd film that is somehow both
            if "leaving" not in match:
                added = newly_added_since(tmdb_id, match["provider_id"])
                if added:
                    match["new_since"] = added
    return matches


def rental_matches(availability, provider_ids):
    """Rent providers carrying the film that the user subscribes to.

    Digital purchase is deliberately ignored — buying happens on
    physical media in this house — and rentals never join the
    streaming match set. The cached payload still carries the buy
    list, should that preference ever change."""

    if not availability or not provider_ids:
        return []
    matches = []
    seen = set()
    for provider in availability.get("rent") or []:
        if provider["provider_id"] in provider_ids:
            if provider["provider_id"] not in seen:
                seen.add(provider["provider_id"])
                matches.append({**provider, "kind": "rent"})
    return matches


def user_streaming(tmdb_id, user, negative=False, local=False, upgradable=None):
    """The template payload for one film: the user's matches and the
    TMDB watch-page link, or None when the user picked no services (the
    surfaces stay quiet for them). negative=True keeps the payload when
    nothing matched, so unowned-film pages can say "not on your
    services" instead of nothing — and only those pages also list where
    the film can be rented, since that's where the watch decision is
    live. Rentals are filtered to the user's chosen services too
    (renting elsewhere is a click away via the watch-page link), and a
    rental never counts as a subscription match. local=True marks an
    owned film: the strip leads with "In your library" so a streaming
    badge never upstages the copy on the shelf, and the payload survives
    an empty match list to say so. upgradable colors that library
    badge (Glenn's Aug 2026 revision): True paints it amber — the
    copy is worth upgrading — False green, None leaves it neutral for
    surfaces that never looked."""

    provider_ids = user_provider_ids(user)
    if not provider_ids:
        return None
    availability = title_availability(tmdb_id)
    # An owned film's strip never warns of a departure or trumpets an
    # arrival (Glenn, Aug 27 2026) — the copy on the shelf isn't going
    # anywhere — so the annotation lookups are skipped outright
    matches = streaming_matches(
        availability, provider_ids, tmdb_id=None if local else tmdb_id
    )
    if not matches and not negative and not local:
        return None

    rentals = rental_matches(availability, provider_ids) if negative else []

    return {
        "matches": matches,
        "link": (availability or {}).get("link"),
        "known": availability is not None,
        "rentals": rentals,
        "local": local,
        "upgradable": upgradable,
    }
