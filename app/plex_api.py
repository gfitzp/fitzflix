"""The HTTP calls to the local Plex Media Server.

Each call adds the token of PLEX_TOKEN as a query parameter and raises
on an HTTP error. plex_get reads JSON. plex_write sends a write (POST,
PUT, or DELETE) and reads the JSON answer, if there is one. plex_command
sends a call whose answer has no body, for example a refresh.

The plex.tv watchlist (app.plex_watchlist) and the player hand-off
(app.plex_player) talk to other hosts. They keep their own clients."""

import requests

from flask import current_app

# The timeout of a call to the local server, in seconds.

PLEX_TIMEOUT = 60


def _url(path):
    """Return the URL of a path on the local Plex server."""

    return current_app.config["PLEX_URL"] + path


def _params(params):
    """Return the query parameters with the Plex token added."""

    return {**(params or {}), "X-Plex-Token": current_app.config["PLEX_TOKEN"]}


def plex_get(path, params=None, timeout=PLEX_TIMEOUT):
    """Send 1 authenticated JSON GET to the local Plex server. Return the body."""

    r = requests.get(
        _url(path),
        params=_params(params),
        headers={"Accept": "application/json"},
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json()


def plex_write(method, path, params=None):
    """Send 1 write call (POST, PUT, or DELETE) to the local Plex server.

    Return the JSON answer, or {} for an empty answer."""

    r = requests.request(
        method,
        _url(path),
        params=_params(params),
        headers={"Accept": "application/json"},
        timeout=PLEX_TIMEOUT,
    )
    r.raise_for_status()
    return r.json() if r.content else {}


def plex_command(method, path, params=None):
    """Send 1 command-style call to the local Plex server (refresh, emptyTrash).

    These calls answer with an EMPTY body. Thus, this function parses
    nothing. The status is the success signal."""

    r = requests.request(
        method, _url(path), params=_params(params), timeout=PLEX_TIMEOUT
    )
    r.raise_for_status()
