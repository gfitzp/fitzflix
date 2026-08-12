"""Streaming availability via TMDb's watch-provider endpoints.

The underlying data is licensed from JustWatch, and TMDb's terms make
attribution mandatory: every surface that shows it must carry a
"Streaming data by JustWatch" credit, or API access can be revoked.
Availability is cached per title for a day (the poster-gallery
pattern), and displays are customized to each user's chosen services —
a per-user Profile setting, never site-wide. The payload carries no
deep links; the only outbound link is the film's TMDb watch page.
"""

import json
import traceback

from flask import current_app

from app.videos import tmdb_get

WATCH_REGION = "US"
CACHE_SECONDS = 86400
REGISTRY_KEY = "fitzflix:tmdb:watch-providers:registry"
AVAILABILITY_KEY = "fitzflix:tmdb:watch-providers:movie:{tmdb_id}"


def provider_registry():
    """US movie watch providers from TMDb's registry, sorted by display
    priority and cached for a day; [] without an API key or when TMDb
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


def title_availability(tmdb_id):
    """The film's US watch-provider payload {link, flatrate, ads, rent,
    buy}, cached for a day; None while unknown (no key, TMDb down).

    A film with no US providers caches an empty payload, so absence
    doesn't re-query TMDb on every page view.
    """

    if tmdb_id is None or not current_app.config["TMDB_API_KEY"]:
        return None
    cache_key = AVAILABILITY_KEY.format(tmdb_id=int(tmdb_id))
    cached = current_app.redis.get(cache_key)
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


def user_provider_ids(user):
    """The TMDb provider ids of the user's chosen services."""

    return {row.provider_id for row in user.streaming_providers}


def streaming_matches(availability, provider_ids):
    """Providers carrying the film that the user subscribes to —
    streaming kinds only (flatrate, then free-with-ads); rent and buy
    aren't subscriptions."""

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
    return matches


def user_streaming(tmdb_id, user, negative=False):
    """The template payload for one film: the user's matches and the
    TMDb watch-page link, or None when the user picked no services (the
    surfaces stay quiet for them). negative=True keeps the payload when
    nothing matched, so unowned-film pages can say "not on your
    services" instead of nothing."""

    provider_ids = user_provider_ids(user)
    if not provider_ids:
        return None
    availability = title_availability(tmdb_id)
    matches = streaming_matches(availability, provider_ids)
    if not matches and not negative:
        return None
    return {
        "matches": matches,
        "link": (availability or {}).get("link"),
        "known": availability is not None,
    }
