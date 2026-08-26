"""Remote playback on the living-room Apple TV via Plex Companion.

GDM discovery is dead in this network — the Plex server lives on the
DMZ VLAN and never hears the players' broadcasts, so /clients is
permanently empty and there is no discovery step. Each USER carries
their own player instead (User.plex_player_address / _id, set on the
Profile page, which probes the address and reads the machine id off
the player itself): user A's play buttons target their Apple TV,
user B's target theirs. A remote household's device works the same
way once it's network-reachable (a VPN address like Tailscale's);
the server side is already reachable from anywhere because
PLEX_PLAYER_SERVER_URI is a public HTTPS address.

The command flow is the one validated by hand on 2026-08-21: resolve
the movie's ratingKey in the local library, build a play queue on the
server, then hand the Apple TV a pointer to the queue plus a server
address IT can reach. That address must be the plex.direct HTTPS host
(PLEX_PLAYER_SERVER_URI) — the same route the tvOS app streams over;
the raw LAN address dies at the inter-VLAN firewall and tvOS insists
on TLS anyway.

Two traps this module guards against, both hit during validation:
a play queue created with a bad server id still returns a
playQueueID, just with zero items — the player then fails with
"container couldn't be retrieved or is empty" — so the queue's item
count is checked before the player is told anything. And the Plex app
must be FOREGROUNDED on the Apple TV (tvOS can't wake it), so a
connection failure at the player is reported as "is the Plex app
open?" rather than as an error.
"""

import time
from urllib.parse import urlsplit
from xml.etree import ElementTree

import requests

from flask import current_app

from app.plex_titles import _plex_get

# Stable controller identity: the Apple TV tracks controllers by this
# id, so changing it makes Fitzflix look like a brand-new remote
CLIENT_IDENTIFIER = "fitzflix"


def remote_playback_configured():
    """Whether the SERVER side of remote playback is configured; the
    player side lives on each user's row (plex_player_configured)."""

    return all(
        current_app.config.get(key)
        for key in ("PLEX_URL", "PLEX_TOKEN", "PLEX_PLAYER_SERVER_URI")
    )


def probe_player(address):
    """Ask the Companion player at ip:port to identify itself:
    {"machine_id", "name"}, or None if nothing answered. This is how
    the Profile page verifies an entered address and discovers the
    machine id — nobody should have to curl their own Apple TV. A
    player only answers while the Plex app is OPEN on it with
    Advertise as Player enabled."""

    try:
        r = requests.get(
            f"http://{address}/resources",
            headers={"X-Plex-Client-Identifier": CLIENT_IDENTIFIER},
            timeout=5,
        )
        r.raise_for_status()
        root = ElementTree.fromstring(r.content)
    except (requests.RequestException, ElementTree.ParseError):
        return None
    player = root.find("Player")
    if player is None or not player.get("machineIdentifier"):
        return None
    return {
        "machine_id": player.get("machineIdentifier"),
        "name": player.get("title") or player.get("product") or "Plex player",
    }


def _server_machine_id():
    """The local Plex server's machineIdentifier."""

    payload = _plex_get("/")
    return payload.get("MediaContainer", {}).get("machineIdentifier")


def _movie_rating_key(movie):
    """The movie's ratingKey in the local Plex library, or None.

    The guid filter is exact and cheap where the server supports it;
    the fallback is a title search with each candidate verified — by
    its TMDB Guid when the movie has a TMDB id, by year otherwise —
    so a remake or a same-named short can't be played by mistake.
    """

    if movie.tmdb_id:
        payload = _plex_get(
            "/library/all", params={"guid": f"tmdb://{movie.tmdb_id}", "type": 1}
        )
        for item in payload.get("MediaContainer", {}).get("Metadata", []) or []:
            if item.get("ratingKey"):
                return item["ratingKey"]

    query = movie.tmdb_title or movie.title
    payload = _plex_get("/search", params={"query": query})
    for item in payload.get("MediaContainer", {}).get("Metadata", []) or []:
        if item.get("type") != "movie" or not item.get("ratingKey"):
            continue
        if movie.tmdb_id:
            meta = _plex_get(f"/library/metadata/{item['ratingKey']}")
            entries = (meta.get("MediaContainer", {}).get("Metadata") or [{}])[0]
            if any(
                guid.get("id") == f"tmdb://{movie.tmdb_id}"
                for guid in entries.get("Guid", []) or []
            ):
                return item["ratingKey"]
        elif item.get("year") == movie.year:
            return item["ratingKey"]
    return None


def _create_play_queue(server_id, rating_key):
    """A play queue holding the movie, or None if Plex built it empty."""

    r = requests.post(
        current_app.config["PLEX_URL"] + "/playQueues",
        params={
            "type": "video",
            "uri": (
                f"server://{server_id}/com.plexapp.plugins.library"
                f"/library/metadata/{rating_key}"
            ),
            "shuffle": "0",
            "repeat": "0",
            "continuous": "0",
            "X-Plex-Token": current_app.config["PLEX_TOKEN"],
        },
        headers={
            "Accept": "application/json",
            "X-Plex-Client-Identifier": CLIENT_IDENTIFIER,
        },
        timeout=30,
    )
    r.raise_for_status()
    container = r.json().get("MediaContainer", {})
    if not container.get("playQueueID") or not container.get("playQueueTotalCount"):
        return None
    return container["playQueueID"]


def play_movie(movie, user):
    """Start the movie on the user's own player. Returns (ok, message);
    every failure mode gets a message fit to show on the movie page."""

    if not remote_playback_configured():
        return False, "Remote playback isn't configured."
    if not user.plex_player_configured:
        return False, "Set your playback device on your Profile page first."

    try:
        rating_key = _movie_rating_key(movie)
        if rating_key is None:
            return False, "Plex doesn't have this movie (or hasn't scanned it yet)."
        server_id = _server_machine_id()
        if not server_id:
            return False, "The Plex server didn't report its machine id."
        queue_id = _create_play_queue(server_id, rating_key)
        if queue_id is None:
            return False, "Plex built an empty play queue for this movie."
    except requests.RequestException:
        return False, "Couldn't talk to the Plex server."

    server = urlsplit(current_app.config["PLEX_PLAYER_SERVER_URI"])
    try:
        r = requests.get(
            f"http://{user.plex_player_address}/player/playback/playMedia",
            params={
                "commandID": int(time.time()),
                "providerIdentifier": "com.plexapp.plugins.library",
                "machineIdentifier": server_id,
                "protocol": server.scheme,
                "address": server.hostname,
                "port": server.port or (443 if server.scheme == "https" else 32400),
                "containerKey": f"/playQueues/{queue_id}?window=100&own=1",
                "key": f"/library/metadata/{rating_key}",
                "offset": 0,
                "token": current_app.config["PLEX_TOKEN"],
            },
            headers={
                "X-Plex-Client-Identifier": CLIENT_IDENTIFIER,
                "X-Plex-Target-Client-Identifier": user.plex_player_id,
            },
            timeout=15,
        )
        r.raise_for_status()
    except (requests.ConnectionError, requests.Timeout):
        return False, "Couldn't reach your player — is the Plex app open on it?"
    except requests.RequestException:
        return False, "Your player refused the play command."

    return True, "Playing on your device."
