"""Plex library refresh + trash emptying, safely.

Replaces the external cron that curl'd refresh and emptyTrash for
hardcoded section ids, guarded by checking one mount per section. The
guard is the whole point: if a section's directory is missing (an SMB
mount dropped), a scan marks everything missing and emptying the
trash then deletes the library's metadata — watch states included —
rebuilding it from scratch when the mount returns.

This version asks Plex for each movie/show section's OWN location
paths and requires every location's MOUNT to be alive before touching
that section (the old script checked a single mount per section — the
Movies section actually has two roots on two volumes). The guard is
mount-level, not leaf-level: an empty transcodes volume legitimately
lacks its Movies subfolder, and an absent leaf on a healthy mount is
harmless to scan. Mount probes go through volume_alive, which treats
a hung SMB stat as dead instead of hanging the worker.
"""

import os
import traceback

import requests

from flask import current_app
from werkzeug.local import LocalProxy

from app import get_app
from app.maintenance import volume_alive
from app.plex_titles import _plex_get

app = LocalProxy(get_app)


def _probe_target(path):
    """What to health-check for a section location: the /Volumes mount
    that backs it, or the path itself when it isn't volume-backed."""

    if path.startswith("/Volumes/"):
        return "/".join(path.split("/")[:3])
    return path


def _plex_command(method, path):
    """A command-style Plex call (refresh, emptyTrash): these answer
    with an EMPTY body, so nothing is parsed — success is the status."""

    r = requests.request(
        method,
        current_app.config["PLEX_URL"] + path,
        params={"X-Plex-Token": current_app.config["PLEX_TOKEN"]},
        timeout=60,
    )
    r.raise_for_status()


def refresh_plex_libraries():
    """Task: scan each movie/show section for changes and empty its
    trash — per section, and only when every location it declares is
    mounted and present."""

    with app.app_context():
        if not (
            current_app.config.get("PLEX_URL") and current_app.config.get("PLEX_TOKEN")
        ):
            return True

        try:
            payload = _plex_get("/library/sections")
        except Exception:
            current_app.logger.warning(traceback.format_exc())
            return True

        for section in payload.get("MediaContainer", {}).get("Directory", []) or []:
            if section.get("type") not in ("movie", "show"):
                continue
            key = section.get("key")
            title = section.get("title")
            locations = [
                location.get("path")
                for location in section.get("Location", []) or []
                if location.get("path")
            ]

            # A dead or hung mount makes scanning dangerous — skip the
            # whole section until it's back

            missing = [
                path
                for path in locations
                if not volume_alive(_probe_target(path))
                or not os.path.isdir(_probe_target(path))
            ]
            if missing or not locations:
                current_app.logger.warning(
                    f"Plex library '{title}': skipping refresh — mount(s) "
                    f"unavailable for: {missing or 'no declared locations'}"
                )
                continue

            try:
                _plex_command("GET", f"/library/sections/{key}/refresh")
                _plex_command("PUT", f"/library/sections/{key}/emptyTrash")
                current_app.logger.info(
                    f"Plex library '{title}': scanned and emptied trash"
                )
            except Exception:
                current_app.logger.warning(traceback.format_exc())
        return True
