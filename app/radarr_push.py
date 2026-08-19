"""Ad-hoc Radarr requests (#66): a per-film hand-off, never automatic.

Glenn's revision of the original always-sync design: pushing every
watchlist film to Radarr would queue hundreds of downloads and fill
the volume, so requesting is a deliberate per-film action — a button
on unowned movie pages and watchlist rows — with a matching withdraw.
Radarr's root folder is the library volume itself, so a granted
request downloads, renames, and flows back in through the existing
Radarr webhook without any further wiring.

House settings (Glenn's spec): monitor the movie only, minimum
availability Released, and the "Fitzflix" quality profile — resolved
by name, never a hardcoded id.
"""

import json

import requests

from flask import current_app

# The hour-cached set of TMDb ids Radarr manages, for request badges;
# refreshed immediately after every push or withdrawal

RADARR_IDS_KEY = "fitzflix:radarr:tmdb_ids"
RADARR_IDS_TTL = 3600

QUALITY_PROFILE_NAME = "Fitzflix"


class RadarrError(Exception):
    """A Radarr request that failed in a way the user should hear
    about, message intact."""


def radarr_configured():
    """Whether the ad-hoc push can work at all."""

    return bool(
        current_app.config.get("RADARR_URL")
        and current_app.config.get("RADARR_API_KEY")
    )


def _radarr(method, path, payload=None):
    """One authenticated JSON call against the Radarr API."""

    r = requests.request(
        method,
        current_app.config["RADARR_URL"] + path,
        json=payload,
        headers={"X-Api-Key": current_app.config["RADARR_API_KEY"]},
        timeout=30,
    )
    r.raise_for_status()
    return r.json() if r.content else None


def radarr_tmdb_ids(refresh=False):
    """The TMDb ids Radarr currently manages, hour-cached. Returns an
    empty set (logged) when Radarr can't be reached — badges degrade,
    buttons still work."""

    if not refresh:
        cached = current_app.redis.get(RADARR_IDS_KEY)
        if cached is not None:
            return set(json.loads(cached))
    try:
        ids = {movie["tmdbId"] for movie in _radarr("GET", "/api/v3/movie")}
    except Exception as e:
        current_app.logger.warning(f"Radarr: couldn't list movies: {e}")
        return set()
    current_app.redis.set(RADARR_IDS_KEY, json.dumps(sorted(ids)), ex=RADARR_IDS_TTL)
    return ids


def request_movie(tmdb_id):
    """Add one film to Radarr, monitored and searched immediately.

    Radarr insists on the full lookup object as the add body — a
    minimal payload is refused — so the flow is lookup, decorate with
    the house settings, post.
    """

    profiles = _radarr("GET", "/api/v3/qualityprofile")
    profile = next(
        (
            p
            for p in profiles
            if (p.get("name") or "").lower() == QUALITY_PROFILE_NAME.lower()
        ),
        None,
    )
    if profile is None:
        raise RadarrError(f"Radarr has no '{QUALITY_PROFILE_NAME}' quality profile")
    roots = _radarr("GET", "/api/v3/rootfolder")
    if not roots:
        raise RadarrError("Radarr has no root folder configured")

    movie = _radarr("GET", f"/api/v3/movie/lookup/tmdb?tmdbId={int(tmdb_id)}")
    movie.update(
        {
            "qualityProfileId": profile["id"],
            "rootFolderPath": roots[0]["path"],
            "monitored": True,
            "minimumAvailability": "released",
            "addOptions": {"monitor": "movieOnly", "searchForMovie": True},
        }
    )
    try:
        _radarr("POST", "/api/v3/movie", movie)
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 400:
            body = e.response.text or ""
            if "already been added" in body:
                raise RadarrError("Radarr already has this film") from e
        raise
    radarr_tmdb_ids(refresh=True)


def withdraw_movie(tmdb_id):
    """Remove one film from Radarr, keeping any files on disk."""

    listing = _radarr("GET", f"/api/v3/movie?tmdbId={int(tmdb_id)}")
    if not listing:
        raise RadarrError("Radarr doesn't have this film")
    _radarr(
        "DELETE",
        f"/api/v3/movie/{listing[0]['id']}"
        "?deleteFiles=false&addImportExclusion=false",
    )
    radarr_tmdb_ids(refresh=True)
