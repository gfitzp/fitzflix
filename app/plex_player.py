"""Remote playback on the living-room Apple TV through Plex Companion.

GDM discovery does not work in this network. The Plex server is on the
DMZ VLAN and never hears the broadcasts of the players. Thus, /clients
is always empty and there is no discovery step. Each USER has a player
of their own instead (User.plex_player_address / _id). The user sets
it on the Profile page. The Profile page probes the address and reads
the machine id from the player itself. The play buttons of user A
target the Apple TV of user A. The play buttons of user B target the
Apple TV of user B. The device of a remote household works the same
way when the network can reach it (a VPN address, for example from
Tailscale). The server side is already reachable from all places,
because PLEX_PLAYER_SERVER_URI is a public HTTPS address.

The command flow is the one validated by hand on 2026-08-21. Resolve
the ratingKey of the movie in the local library. Build a play queue on
the server. Then give the Apple TV a pointer to the queue and a server
address that the Apple TV can reach. That address must be the
plex.direct HTTPS host (PLEX_PLAYER_SERVER_URI). The tvOS app streams
over the same route. The raw LAN address stops at the inter-VLAN
firewall. Also, tvOS requires TLS.

This module guards against 2 traps. Both occurred during validation.
A play queue created with a bad server id still returns a
playQueueID, but with 0 items. The player then fails with "container
couldn't be retrieved or is empty". Thus, this module checks the item
count of the queue before it sends a command to the player. Also, the
Plex app must be in the FOREGROUND on the Apple TV (tvOS cannot wake
it). Thus, this module reports a connection failure at the player as
"is the Plex app open?" and not as an error.
"""

import time
from urllib.parse import urlsplit
from xml.etree import ElementTree

import requests

from flask import current_app

from app.plex_titles import _plex_get

# Stable controller identity. The Apple TV tracks controllers by this
# id. Thus, a change to it makes Fitzflix look like a new remote.
CLIENT_IDENTIFIER = "fitzflix"


def remote_playback_configured():
    """Return True if the SERVER side of remote playback is configured.

    The player side is on the row of each user (plex_player_configured)."""

    return all(
        current_app.config.get(key)
        for key in ("PLEX_URL", "PLEX_TOKEN", "PLEX_PLAYER_SERVER_URI")
    )


def probe_player(address):
    """Ask the Companion player at ip:port to identify itself.

    Return {"machine_id", "name"}, or None if no player answered. The
    Profile page uses this function to verify an entered address and
    to find the machine id. No user must curl their own Apple TV. A
    player answers only while the Plex app is OPEN on it and Advertise
    as Player is enabled."""

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
    """Return the machineIdentifier of the local Plex server."""

    payload = _plex_get("/")
    return payload.get("MediaContainer", {}).get("machineIdentifier")


def _movie_rating_key(movie):
    """Return the ratingKey of the movie in the local Plex library, or None.

    The guid filter is exact and cheap where the server supports it.
    The fallback is a title search. This function verifies each
    candidate. It uses the TMDB Guid when the movie has a TMDB id. It
    uses the year in the other case. Thus, a remake or a short with the
    same name cannot play by mistake.
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
    """Return a play queue that holds the movie, or None if Plex built it empty."""

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
    """Start the movie on the player of the user.

    Return (ok, message). Each failure mode gets a message that the
    movie page can show."""

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
