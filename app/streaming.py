"""Streaming availability through the watch-provider endpoints of TMDB.

JustWatch licenses the underlying data. The terms of TMDB make
attribution mandatory. Each surface that shows the data must show a
"Streaming data by JustWatch" credit. If not, TMDB can revoke the API
access. Fitzflix caches the availability per title. The
refresh_availability cron refreshes it each night. The cache holds
each entry for 2 days. Thus, one missed night does not make the cache
cold. Before 2026-08, the day-cached entries expired in a cluster. The
first page view of the day then stalled behind 50 inline fetches under
the rate limiter. The displays show the services that each user
selected. This is a per-user Profile setting, never a site-wide
setting. The payload has no deep links. The only outbound link is the
TMDB watch page of the film.
"""

import json
import traceback

from concurrent.futures import ThreadPoolExecutor

import requests

from flask import current_app, g
from werkzeug.local import LocalProxy

from app import db, get_app
from app.models import Movie, tmdb_get

# The app instance of this process. Fitzflix resolves it lazily. Thus,
# the warm task can run on a worker without a second application.

app = LocalProxy(get_app)

WATCH_REGION = "US"
CACHE_SECONDS = 2 * 86400
REFRESH_WORKERS = 20
REGISTRY_KEY = "fitzflix:tmdb:watch-providers:registry"
REGISTRY_RETRY_SECONDS = 300
AVAILABILITY_KEY = "fitzflix:tmdb:watch-providers:movie:{tmdb_id}"


def provider_registry():
    """Return the US movie watch providers from the registry of TMDB.

    The list is sorted by display priority and cached for CACHE_SECONDS.
    The result is [] without an API key or when TMDB is not reachable."""

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
        # A failed fetch is remembered briefly so the callers that ask
        # per film (rec-shelf pool, DVR build, availability alerts)
        # pay one timeout per outage, not one per film
        current_app.logger.warning(traceback.format_exc())
        current_app.redis.set(REGISTRY_KEY, "[]", ex=REGISTRY_RETRY_SECONDS)
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
    """Return the US watch-provider payload of the film, or None while unknown.

    The payload is {link, flatrate, ads, rent, buy}. Fitzflix caches it
    for CACHE_SECONDS. The result is None when there is no key or TMDB
    is down. refresh=True skips the cache read. The nightly task uses
    it to fetch a title again while its entry is still live.

    A film with no US providers caches an empty payload. Thus, the
    absence does not query TMDB again on each page view.
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
        # A 404 is the answer of TMDB, not an outage. The id has no watch
        # record (a stale or wrong tmdb id). Thus, Fitzflix caches it as
        # empty and does not query again on each page view.

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
    """Return (payloads, deferred) for many titles at one time.

    payloads is {tmdb_id: payload-or-None}. deferred is the list of the
    ids that were not fetched. Fitzflix reads the cache hits in 1 MGET.
    The misses are fetched concurrently. Each miss goes through
    title_availability. Thus, the caching and the 404 handling are the
    same as in the single-title path. All fetches share the app-wide
    rate limiter of tmdb_get. Thus, fetch_limit bounds the time that a
    render can stall behind the limiter. The ids that are left come
    back to the caller. The caller can warm them in the background.
    The page renders pass fetch_limit=0 (2026-08). They answer from the
    cache that the nightly refresh keeps full. They never fetch inline.
    refresh=True fetches each id again, cached or not."""

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
        """Return the availability of one title under its own app context."""

        with flask_app.app_context():
            return tmdb_id, title_availability(tmdb_id, refresh=refresh)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for tmdb_id, payload in pool.map(fetch, misses):
            results[tmdb_id] = payload
    return results, deferred


def warm_title_availability(tmdb_ids):
    """Fill the availability cache for the given titles (background task).

    The ids are the ones that a page render found uncached. Thus, the
    next visit has each badge and count without a wait."""

    with app.app_context():
        batch_title_availability(tmdb_ids)
        return True


def refresh_availability():
    """Fetch the availability again for each film with a TMDB id (nightly task).

    Added 2026-08. The pages that read the availability (the watchlist,
    the Criterion catalog, the filmographies) then always answer from a
    full cache. They never block on TMDB. The whole-library cost is
    some thousand requests. They take minutes under the rate limiter.
    The 2-day TTL of each entry restarts. Thus, one missed night still
    serves the data of the day before."""

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
    """Return the TMDB provider ids of the services that the user selected."""

    return {row.provider_id for row in user.streaming_providers}


def _criterion_provider():
    """Return the Criterion Channel entry of the provider registry.

    A synthesized match renders with this name and logo. If Fitzflix
    cannot read the registry, return a stand-in without a logo. The
    badge template skips the image for a null logo_path. A registry hit
    is kept on flask.g for the app context. The rec-shelf pool asks for
    each candidate film, and each registry read parses the full provider
    list. The stand-in is never cached. Thus, a registry that comes back
    in the same context is picked up. The retry window of
    provider_registry bounds the cost of a new request while TMDB is
    down."""

    from app.leaving_criterion import CRITERION_PROVIDER_ID

    entry = getattr(g, "_criterion_provider_entry", None)
    if entry is None:
        for provider in provider_registry():
            if provider["provider_id"] == CRITERION_PROVIDER_ID:
                entry = {
                    "provider_id": provider["provider_id"],
                    "provider_name": provider["provider_name"],
                    "logo_path": provider["logo_path"],
                }
                g._criterion_provider_entry = entry
                break
    if entry is None:
        return {
            "provider_id": CRITERION_PROVIDER_ID,
            "provider_name": "Criterion Channel",
            "logo_path": None,
        }
    return dict(entry)


def streaming_matches(availability, provider_ids, tmdb_id=None):
    """Return the providers that carry the film and that the user subscribes to.

    Only the streaming kinds count (flatrate, then free-with-ads). Rent
    and buy are not subscriptions. If the caller gives the tmdb_id of
    the film, a Criterion Channel match also learns if the film is on
    the leaving set of the month. Its "leaving" key holds the departure
    date ("August 31"). Thus, the badge can light up wherever it
    renders (requested by Glenn, 2026-08, to watch it before it goes).

    A film in the scraped newly-added store or leaving store gets a
    synthesized Criterion match (decided by Glenn, 2026-09-01). Those
    stores are the catalog pages of the Channel itself. They are the
    first-party word that the film streams there. The JustWatch-fed
    payload of TMDB is days behind them around the month turnover.
    That is exactly when the discovery shelves push the film hardest.
    The synthesis (like the annotations) needs the tmdb_id. Thus, the
    owned-film callers that pass None on purpose skip both."""

    if not provider_ids:
        return []
    matches = []
    seen = set()
    for kind in ("flatrate", "ads"):
        for provider in (availability or {}).get(kind) or []:
            if provider["provider_id"] in provider_ids:
                if provider["provider_id"] not in seen:
                    seen.add(provider["provider_id"])
                    matches.append({**provider, "kind": kind})
    if tmdb_id is not None:
        # Imported here. leaving_criterion and newly_added reach this
        # module through streaming_rail. Thus, top-level imports would
        # be circular.
        from app.leaving_criterion import CRITERION_PROVIDER_ID, leaving_departure
        from app.newly_added import newly_added_since

        if CRITERION_PROVIDER_ID in provider_ids and CRITERION_PROVIDER_ID not in seen:
            if newly_added_since(tmdb_id, CRITERION_PROVIDER_ID) or leaving_departure(
                tmdb_id
            ):
                matches.append({**_criterion_provider(), "kind": "flatrate"})

        for match in matches:
            if match["provider_id"] == CRITERION_PROVIDER_ID:
                departs = leaving_departure(tmdb_id)
                if departs:
                    match["leaving"] = departs
            # The newly-added feeds (#246) mark a recent arrival on
            # the badge of any provider. Departure urgency outranks it
            # on the rare film that is somehow both.
            if "leaving" not in match:
                added = newly_added_since(tmdb_id, match["provider_id"])
                if added:
                    match["new_since"] = added
    return matches


def rental_matches(availability, provider_ids):
    """Return the rent providers of the film that the user subscribes to.

    Digital purchase is ignored on purpose. Purchases in this house are
    on physical media. Rentals never join the streaming match set. The
    cached payload still holds the buy list, in case that preference
    changes."""

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
    """Return the template payload for one film, or None.

    The payload holds the matches of the user and the TMDB watch-page
    link. The result is None when the user selected no services (the
    surfaces stay quiet for them). negative=True keeps the payload when
    nothing matched. Thus, the pages of unowned films can say "not on
    your services" instead of nothing. Only those pages also list where
    the user can rent the film, because the watch decision is live
    there. Fitzflix filters the rentals to the selected services of the
    user too (a rental at a different place is 1 click away through the
    watch-page link). A rental never counts as a subscription match.
    local=True marks an owned film. The strip then starts with "In your
    library". Thus, a streaming badge never outranks the copy on the
    shelf. The payload survives an empty match list to say so.
    upgradable colors that library badge (revision by Glenn, 2026-08).
    True paints it amber, because the copy is worth an upgrade. False
    paints it green. None leaves it neutral for the surfaces that never
    looked."""

    provider_ids = user_provider_ids(user)
    if not provider_ids:
        return None
    availability = title_availability(tmdb_id)
    # The strip of an owned film never warns of a departure or announces
    # an arrival (decided by Glenn, 2026-08-27). The copy on the shelf
    # stays. Thus, Fitzflix skips the annotation lookups completely.
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
