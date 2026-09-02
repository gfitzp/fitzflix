"""Remote playback on the living-room Apple TV through Plex Companion.

GDM discovery does not work in this network. The Plex server is on the
DMZ VLAN and never hears the broadcasts of the players. Thus, /clients
is always empty and there is no discovery step. Each USER has a player
of their own instead (User.plex_player_address / _id). The user sets
it on the Profile page. The Profile page probes the address and reads
the machine id from the player itself. The play buttons of user A
target the Apple TV of user A. The play buttons of user B target the
Apple TV of user B. The device of a remote household works the same
way when the network can reach it at a private-network IP address.
The 100.64/10 addresses of Tailscale qualify. Names do not (refer to
player_address). The server side is already reachable from all places,
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

import ipaddress
import time
from urllib.parse import urlsplit
from xml.etree import ElementTree

import requests

from flask import current_app

from app.plex_titles import _plex_get

# Stable controller identity. The Apple TV tracks controllers by this
# id. Thus, a change to it makes Fitzflix look like a new remote.
CLIENT_IDENTIFIER = "fitzflix"

PLEX_TV = "https://plex.tv"
PLAYER_PORT = 32500

# Where a player may live: the private ranges, link-local, loopback,
# and Tailscale's 100.64/10 (a remote household's device, per the
# module docstring). The play command carries a Plex token, so the
# address is a literal on one of these — never a hostname, which can
# resolve anywhere, and somewhere else again after the probe
PLAYER_NETWORKS = [
    ipaddress.ip_network(cidr)
    for cidr in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "100.64.0.0/10",
        "169.254.0.0/16",
        "127.0.0.0/8",
        "fc00::/7",
        "fe80::/10",
        "::1/128",
    )
]

# A non-admin's play command carries a token for THEIR Plex Home user,
# minted through plex.tv and kept for a while; a miss (no such Home
# user) is remembered briefly so an unlinked member's clicks don't each
# wait on plex.tv
HOME_TOKEN_KEY = "fitzflix:plex:home-token:{user_id}"
HOME_TOKEN_SECONDS = 12 * 3600
HOME_TOKEN_MISS_SECONDS = 300


def player_address(text, default_port=PLAYER_PORT):
    """The normalized "ip:port" of a player address, or None when it
    isn't a literal IP on a private network (PLAYER_NETWORKS) with a
    sane port. A bare IP takes the default port (Plex Companion's
    unless told otherwise — the Infuse block passes Apple's); IPv6
    goes in brackets. The Profile page validates with this and
    play_movie re-checks the stored value, so a hostname can't reach
    the play command by any route."""

    text = (text or "").strip()
    if not text:
        return None
    host, port = text, default_port
    if text.startswith("["):
        end = text.find("]")
        if end == -1:
            return None
        host, rest = text[1:end], text[end + 1 :]
        if rest:
            if not rest.startswith(":"):
                return None
            port = rest[1:]
    elif text.count(":") == 1:
        host, port = text.split(":")
    try:
        ip = ipaddress.ip_address(host)
        port = int(port)
    except ValueError:
        return None
    if not 0 < port < 65536 or not any(ip in network for network in PLAYER_NETWORKS):
        return None
    return f"[{ip}]:{port}" if ip.version == 6 else f"{ip}:{port}"


def player_token(user):
    """The Plex token the play command hands the user's player. An
    admin — the server's owner — gets the owner token. Anyone else
    gets a token for THEIR Plex Home user (matched to User.plex_username
    by username or title), minted through plex.tv's home switch and
    cached; None when no Home user matches (the user links one by
    entering their Plex username on their own Profile page) or that
    user is PIN-protected. The owner token never travels to a device a
    non-admin chose: the Profile page's probe only proves something
    answered at the address, not that it's a Plex player (security
    review, Sept 2026)."""

    if user.admin:
        return current_app.config["PLEX_TOKEN"]
    if not user.plex_username:
        return None
    key = HOME_TOKEN_KEY.format(user_id=user.id)
    cached = current_app.redis.get(key)
    if cached is not None:
        return cached.decode() or None
    token = _home_user_token(user.plex_username)
    if token:
        current_app.redis.set(key, token, ex=HOME_TOKEN_SECONDS)
    else:
        current_app.redis.set(key, "", ex=HOME_TOKEN_MISS_SECONDS)
    return token


def forget_home_token(user_id):
    """Drop the user's cached Home token — the Profile page calls
    this when the Plex username changes, so the next play is minted
    for the newly linked user rather than the old one."""

    current_app.redis.delete(HOME_TOKEN_KEY.format(user_id=user_id))


def _home_user_token(plex_username):
    """A token for the named Plex Home user, or None. The Home user
    flagged admin — the server's owner — is never switched to: a
    member who claims the owner's name on their Profile page must not
    be able to mint the owner's token this way."""

    wanted = plex_username.strip().lower()
    if not wanted:
        return None
    headers = {
        "Accept": "application/json",
        "X-Plex-Client-Identifier": CLIENT_IDENTIFIER,
        "X-Plex-Token": current_app.config["PLEX_TOKEN"],
    }
    r = requests.get(f"{PLEX_TV}/api/v2/home/users", headers=headers, timeout=15)
    r.raise_for_status()
    for home_user in r.json().get("users") or []:
        names = {
            (home_user.get("username") or "").lower(),
            (home_user.get("title") or "").lower(),
        }
        if wanted in names and home_user.get("uuid"):
            if home_user.get("protected") or home_user.get("admin"):
                return None
            r = requests.post(
                f"{PLEX_TV}/api/v2/home/users/{home_user['uuid']}/switch",
                headers=headers,
                timeout=15,
            )
            r.raise_for_status()
            return r.json().get("authToken") or None
    return None


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


def _create_play_queue(server_id, rating_key, token):
    """Return a play queue that holds the movie, or None if Plex built it empty.

    The queue is built as the user of the token. Thus, the player can
    get it with the same token."""

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
            "X-Plex-Token": token,
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
    address = player_address(user.plex_player_address)
    if address is None:
        return False, (
            "Your playback device isn't at a private-network address — "
            "set it again on your Profile page."
        )

    try:
        token = player_token(user)
        if token is None:
            return False, (
                "Remote playback needs your Plex Home account linked — set "
                "your Plex username on your Profile page."
            )
        rating_key = _movie_rating_key(movie)
        if rating_key is None:
            return False, "Plex doesn't have this movie (or hasn't scanned it yet)."
        server_id = _server_machine_id()
        if not server_id:
            return False, "The Plex server didn't report its machine id."
        queue_id = _create_play_queue(server_id, rating_key, token)
        if queue_id is None:
            return False, "Plex built an empty play queue for this movie."
    except requests.RequestException:
        return False, "Couldn't talk to the Plex server."

    server = urlsplit(current_app.config["PLEX_PLAYER_SERVER_URI"])
    try:
        r = requests.get(
            f"http://{address}/player/playback/playMedia",
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
                "token": token,
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
