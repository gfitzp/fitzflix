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
import os
import time
import traceback
from urllib.parse import urlsplit
from xml.etree import ElementTree

import requests

from flask import current_app

from app.cache import cached_fetch
from app.file_ranking import MOVIE_BEST_ORDER
from app.models import File, RefQuality
from app.plex_api import plex_get as _plex_get

# Stable controller identity. The Apple TV tracks controllers by this
# id. Thus, a change to it makes Fitzflix look like a new remote.
CLIENT_IDENTIFIER = "fitzflix"

PLEX_TV = "https://plex.tv"
PLAYER_PORT = 32500

# These are the networks where a player can be: the private ranges,
# link-local, loopback, and the 100.64/10 range of Tailscale (the
# device of a remote household, refer to the module docstring). The
# play command carries a Plex token. Thus, the address must be a
# literal IP on one of these networks. It is never a hostname. A
# hostname can resolve to any address, and to a different address
# after the probe.
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

# The tokens of one Home user, by the uuid of that user. The play
# command of a non-admin carries the token of THEIR Plex Home user
# (#653). The stream selection sync (#652) acts for every Home user.
# Each Home user has a cache of their own. Thus, a member who changes
# their Plex username on the Profile page gets the token of the newly
# linked user at the next play. Nothing needs to forget. plex.tv limits the
# home switch, and answered 429 to 12 switches in one minute. Thus, the
# account token of a switch lives 12 hours, not one run. The server
# refuses that account token with a 401. A shared user reaches the
# server with the access token that plex.tv lists for that server in
# the resources of the user. That token lives 12 hours too.

HOME_SWITCH_KEY = "fitzflix:plex:home-switch:{uuid}"
HOME_ACCESS_KEY = "fitzflix:plex:home-access:{uuid}"
HOME_TOKEN_SECONDS = 12 * 3600
HOME_TOKEN_MISS_SECONDS = 300


def player_address(text, default_port=PLAYER_PORT):
    """Return the normalized "ip:port" of a player address, or None.

    The result is None if the address is not a literal IP on a private
    network (PLAYER_NETWORKS) with a valid port. A bare IP gets the
    default port. That is the Plex Companion port unless the caller
    gives a different one (the Infuse block passes the Apple port). An
    IPv6 address goes in brackets. The Profile page validates with
    this function. play_movie checks the stored value again. Thus, a
    hostname cannot reach the play command by any route."""

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
    """Return the Plex token that the play command gives the player of the user.

    An admin is the owner of the server. An admin gets the owner
    token. Each other user gets the token with which THEIR Plex Home
    user reaches the local server. Fitzflix matches the Home user to
    User.plex_username by username or by title. The token comes from
    the same per-Home-user cache as the stream selection sync (#652).
    The server refuses the account token of the home switch itself
    with a 401 (#653). The result is None when no Home user matches,
    or when that user is PIN-protected. A user links a Home user with
    the Plex username field on their own Profile page. The owner token
    never travels to a device that a non-admin chose. The probe of the
    Profile page only proves that something answered at the address.
    It does not prove that a Plex player answered (security review,
    2026-09)."""

    if user.admin:
        return current_app.config["PLEX_TOKEN"]
    home_user = _home_user_named(user.plex_username)
    if home_user is None:
        return None
    return _cached_access_token(home_user)


def _plex_tv_headers():
    """Return the headers of a plex.tv call as the owner."""

    return {
        "Accept": "application/json",
        "X-Plex-Client-Identifier": CLIENT_IDENTIFIER,
        "X-Plex-Token": current_app.config["PLEX_TOKEN"],
    }


def _home_users():
    """Return the Home users of the owner, as plex.tv lists them."""

    r = requests.get(
        f"{PLEX_TV}/api/v2/home/users", headers=_plex_tv_headers(), timeout=15
    )
    r.raise_for_status()
    return r.json().get("users") or []


def _switch_token(home_user):
    """Return a token for one Home user through the home switch, or None.

    This never switches to the Home user with the admin flag. That
    user is the owner of the server. A PIN-protected user needs the
    PIN, which Fitzflix does not have."""

    if not home_user.get("uuid"):
        return None
    if home_user.get("protected") or home_user.get("admin"):
        return None
    r = requests.post(
        f"{PLEX_TV}/api/v2/home/users/{home_user['uuid']}/switch",
        headers=_plex_tv_headers(),
        timeout=15,
    )
    r.raise_for_status()
    return r.json().get("authToken") or None


def _home_user_named(plex_username):
    """Return the Plex Home user with this username or title, or None.

    A member who claims the name of the owner on their Profile page
    must not get the token of the owner in this way. _switch_token
    refuses the admin, so the match itself can include the owner."""

    wanted = (plex_username or "").strip().lower()
    if not wanted:
        return None
    for home_user in _home_users():
        if not home_user.get("uuid"):
            continue
        names = {
            (home_user.get("username") or "").lower(),
            (home_user.get("title") or "").lower(),
        }
        if wanted in names:
            return home_user
    return None


def _server_access_token(account_token):
    """Return the token with which the account of this token reaches the local server.

    plex.tv lists the servers that an account can reach, each with an
    access token for that account. The server refuses the account token
    itself with a 401. The result is None when the account cannot reach
    the local server."""

    machine_id = _server_machine_id()
    r = requests.get(
        f"{PLEX_TV}/api/v2/resources",
        params={"includeHttps": 1},
        headers={
            "Accept": "application/json",
            "X-Plex-Client-Identifier": CLIENT_IDENTIFIER,
            "X-Plex-Token": account_token,
        },
        timeout=15,
    )
    r.raise_for_status()
    for resource in r.json() or []:
        if (
            resource.get("provides") == "server"
            and resource.get("clientIdentifier") == machine_id
        ):
            return resource.get("accessToken") or None
    return None


def _cached(key, home_user, fetch):
    """Return the cached value of a token of a Home user. Fetch it on a miss.

    A refusal of plex.tv, for example a 429, caches nothing. The next
    call tries again. A None result (the owner, a PIN-protected user,
    a user without access to the server) is cached as a miss for a
    short time."""

    def guarded():
        try:
            return fetch(), True
        except requests.HTTPError:
            current_app.logger.warning(traceback.format_exc())
            return None, False

    return cached_fetch(
        key.format(uuid=home_user["uuid"]),
        HOME_TOKEN_SECONDS,
        guarded,
        miss_ttl=HOME_TOKEN_MISS_SECONDS,
    )


def _cached_account_token(home_user):
    """Return the account token of a Home user, from the cache or the home switch.

    A refusal of plex.tv raises. The caller decides what a refusal
    means for its own cache."""

    return cached_fetch(
        HOME_SWITCH_KEY.format(uuid=home_user["uuid"]),
        HOME_TOKEN_SECONDS,
        lambda: (_switch_token(home_user), True),
        miss_ttl=HOME_TOKEN_MISS_SECONDS,
    )


def _cached_access_token(home_user):
    """Return the server access token of a Home user, from the cache or plex.tv.

    A refused switch raises inside the fetch. Then the guard of _cached
    caches nothing at this level either."""

    def fetch():
        account_token = _cached_account_token(home_user)
        return _server_access_token(account_token) if account_token else None

    return _cached(HOME_ACCESS_KEY, home_user, fetch)


def home_user_tokens():
    """Return [(title, token)] for every Home user that Fitzflix can switch to.

    The token is the one that reaches the local server as that user.
    The owner and the PIN-protected users are not in the list. The
    stream selection sync (#652) acts for each of these accounts in
    turn. A user whose switch plex.tv refused is not in the list for
    this run.
    """

    tokens = []
    for home_user in _home_users():
        if not home_user.get("uuid"):
            continue
        token = _cached_access_token(home_user)
        if token:
            tokens.append((home_user.get("title") or home_user.get("username"), token))
    return tokens


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


def _rating_key_at(sections, path):
    """Return the ratingKey of the Plex movie with a part file at path, or None.

    The file filter of a section page is a text match, not an exact
    match. Plex reads a comma in the value as a list of words, and an
    item matches any word. Thus, this function compares the part files
    of each answer with path. It asks for the last 2 components of the path: the folder and
    the file. When Plex reaches the library by another mount, those
    components identify the file, as in find_plex_item of
    app.plex_rename. The full path wins over such a match."""

    # app.plex_library imports app.plex_selection, and that module
    # imports this module. Thus, the import waits for the first call.

    from app.plex_library import _part_files, _sections_holding, _tail

    # The path needs no normalization. LIBRARY_DIR is absolute, and
    # file_path holds no "..". The folder and the file name of a movie
    # are ASCII, because the import passes them through unidecode
    # (app.importing). Thus, the part file of Plex has the same text.

    tail = _tail(path, 2)
    for section in _sections_holding(sections, {path}):
        payload = _plex_get(
            f"/library/sections/{section['key']}/all",
            params={"type": 1, "file": tail},
        )
        other_mount = None
        for item in payload.get("MediaContainer", {}).get("Metadata", []) or []:
            for part in _part_files(item):
                if part == path:
                    return item.get("ratingKey")
                if other_mount is None and _tail(part, 2) == tail:
                    other_mount = item.get("ratingKey")
        if other_mount:
            return other_mount
    return None


def _movie_rating_key(movie):
    """Return the ratingKey of the movie in the local Plex library, or None.

    Invariant (#761): a film that Plex has plays, and None means that
    Plex lists no main-feature file of the film. Thus, the message "Plex
    does not have this movie" is true. The lookup goes by the path of
    each main-feature file. The best copy by MOVIE_BEST_ORDER comes
    first, over all editions. When the plain cut and a named edition
    tie on each key of that order but the file id, the plain cut comes
    first. A file that Plex
    does not list, for example a copy that is only in S3, gives way to
    the next copy. An extra is never the film. The guid filter of Plex
    found no film on this server, and a title search missed the films
    with another Plex title.
    """

    # MOVIE_BEST_ORDER has no edition. The DVR and the frame game use it
    # as it is. Thus, only this query puts the plain cut before the file
    # id, its last key (#761).

    *rank, file_id = MOVIE_BEST_ORDER
    files = (
        File.query.join(RefQuality, RefQuality.id == File.quality_id)
        .filter(File.movie_id == movie.id, File.feature_type_id.is_(None))
        .order_by(*rank, File.edition.isnot(None).asc(), file_id)
        .all()
    )
    sections = [
        section
        for section in _plex_get("/library/sections")
        .get("MediaContainer", {})
        .get("Directory", [])
        or []
        if section.get("type") == "movie"
    ]
    library = current_app.config["LIBRARY_DIR"]
    for file in files:
        rating_key = _rating_key_at(sections, os.path.join(library, file.file_path))
        if rating_key:
            return rating_key
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
        return False, "Remote playback is not configured."
    if not user.plex_player_configured:
        return False, "Set your playback device on your Profile page first."
    address = player_address(user.plex_player_address)
    if address is None:
        return False, (
            "Your playback device is not at a private-network address. "
            "Set it again on your Profile page."
        )

    try:
        token = player_token(user)
        if token is None:
            return False, (
                "Remote playback needs a link to your Plex Home account. "
                "Set your Plex username on your Profile page."
            )
        rating_key = _movie_rating_key(movie)
        if rating_key is None:
            return False, "Plex does not have this movie, or Plex did not scan it yet."
        server_id = _server_machine_id()
        if not server_id:
            return False, "The Plex server did not report its machine id."
        queue_id = _create_play_queue(server_id, rating_key, token)
        if queue_id is None:
            return False, "Plex built an empty play queue for this movie."
    except requests.RequestException:
        return False, "Fitzflix could not talk to the Plex server."

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
        return (
            False,
            "Fitzflix could not reach your player. Make sure that the Plex app is open on it.",
        )
    except requests.RequestException:
        return False, "Your player refused the play command."

    return True, "Playing on your device."
