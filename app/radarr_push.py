"""Send ad-hoc Radarr requests, one film at a time and never automatically.

Glenn revised the original always-sync design. A push of every
watchlist film to Radarr would queue hundreds of downloads and fill
the volume. Thus, a request is a deliberate action for one film. The
action is an entry on the Find menu of unowned movie pages and
watchlist tiles. The matching withdraw exists only as a route. Glenn
removed the Un-request entry in 2026-08. The user removes films in
Radarr itself. Thus, the cached badge set can show such a removal
late, by up to its TTL of 1 hour.
The root folder of Radarr is the library volume itself. Thus, a
granted request downloads, renames, and comes back in through the
existing Radarr webhook. No other connection is necessary.

The shared root folder has one consequence. A TMDB refresh can rename
a movie folder. Radarr then holds a path that no longer exists, sees
no file, and downloads the film again. Thus, the refresh reports each
rename to Radarr with follow_rename. On a change of the TMDB id, the
refresh withdraws the entry of the old id with withdraw_movie.

House settings (specified by Glenn): monitor the movie only, minimum
availability Released, and the "Fitzflix" quality profile. Fitzflix
finds the profile by name, never by a hardcoded id.
"""

import json
import os

import requests

from flask import current_app

# This is the set of TMDB ids that Radarr manages, cached for 1 hour,
# for the request badges. Fitzflix refreshes the set immediately after
# each push or withdrawal.

RADARR_IDS_KEY = "fitzflix:radarr:tmdb_ids"
RADARR_IDS_TTL = 3600

QUALITY_PROFILE_NAME = "Fitzflix"


class RadarrError(Exception):
    """A Radarr request failed in a way that the user must know about.

    The message stays as it is."""


def radarr_configured():
    """Return True if the ad-hoc push can work at all."""

    return bool(
        current_app.config.get("RADARR_URL")
        and current_app.config.get("RADARR_API_KEY")
    )


def _radarr(method, path, payload=None):
    """Make one authenticated JSON call to the Radarr API."""

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
    """Return the TMDB ids that Radarr manages now, cached for 1 hour.

    If Fitzflix cannot reach Radarr, this function logs the error and
    returns an empty set. The badges are then incomplete. The buttons
    continue to work."""

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

    Radarr requires the full lookup object as the body of the add call.
    It refuses a minimal payload. Thus, the flow is: look up the film,
    add the house settings, then POST.
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
    """Remove one film from Radarr and keep its files on the disk."""

    listing = _radarr("GET", f"/api/v3/movie?tmdbId={int(tmdb_id)}")
    if not listing:
        raise RadarrError("Radarr doesn't have this film")
    _radarr(
        "DELETE",
        f"/api/v3/movie/{listing[0]['id']}"
        "?deleteFiles=false&addImportExclusion=false",
    )
    radarr_tmdb_ids(refresh=True)


def follow_rename(tmdb_id, new_folder):
    """Point the Radarr movie at its renamed folder, then request a rescan.

    Radarr matches the movie by TMDB id, its native key. The new path
    keeps the parent of the old Radarr path. Thus, a Radarr host with a
    different mount point still sees the correct folder. The PUT does
    not move files. Fitzflix moved them already. The RefreshMovie
    command makes Radarr adopt the renamed file at once. This function
    returns True if Radarr manages the film."""

    listing = _radarr("GET", f"/api/v3/movie?tmdbId={int(tmdb_id)}")
    if not listing:
        current_app.logger.info(
            f"Radarr does not manage tmdb {tmdb_id}, no path to follow"
        )
        return False
    return _point_entry_at(listing[0], new_folder)


def follow_import_move(source_folder, new_folder):
    """Point the Radarr movie that owns source_folder at new_folder.

    An import takes the download out of the folder of Radarr and puts
    the localized file at the Fitzflix path. Usually the 2 folders are
    the same. They differ when the naming rules differ, for example a
    slash or an accented letter in the title. Then Radarr sees an empty
    folder and searches again. This function finds the Radarr movie by
    the name of its folder. A TMDB id is not always known at import
    time. This function returns True if Radarr manages the folder."""

    wanted = os.path.basename(source_folder.rstrip("/"))
    for entry in _radarr("GET", "/api/v3/movie"):
        if os.path.basename((entry.get("path") or "").rstrip("/")) == wanted:
            return _point_entry_at(entry, new_folder)
    current_app.logger.info(f"Radarr does not manage folder {wanted!r}, skipping")
    return False


def _point_entry_at(entry, new_folder):
    """Rewrite the path of one Radarr movie, then request a rescan."""

    old_path = (entry.get("path") or "").rstrip("/")
    new_path = os.path.join(os.path.dirname(old_path), new_folder)
    if old_path and old_path != new_path:
        entry["path"] = new_path
        _radarr("PUT", f"/api/v3/movie/{entry['id']}?moveFiles=false", entry)
        current_app.logger.info(f"Radarr path {old_path!r} -> {new_path!r}")
    _radarr(
        "POST", "/api/v3/command", {"name": "RefreshMovie", "movieIds": [entry["id"]]}
    )
    return True
