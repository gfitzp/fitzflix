"""Write the TV episode titles from Fitzflix into Plex.

Some episode filenames have a title segment. The pattern is "Series -
SxxEyy - Title [Quality]". The filename parse stores this segment in
file.edition. The Doctor Who (1963) specials are the population. They
have custom episode numbers that no agent will title. The rule from
Glenn: if a TV file has an edition, that edition IS the title of the
episode. This applies to all shows and all seasons. A nightly sweep
writes the title to Plex through the local API. It locks the title
field. This sweep replaces the external cron that wrote directly to
the SQLite database of Plex. That cron used a hardcoded path offset of
70 characters. A library move would have broken it.

This module matches the episodes to the files by the BASENAME of the
Part file. It never matches by title. Plex titles the classic series
only "Doctor Who". The "(1963)" is only in the folder name.
"""

import os
import traceback

import requests

from flask import current_app
from werkzeug.local import LocalProxy

from app import db, get_app
from app.models import File

app = LocalProxy(get_app)

PAGE_SIZE = 1000


def _plex_get(path, params=None):
    """Send one authenticated JSON GET to the local Plex server."""

    r = requests.get(
        current_app.config["PLEX_URL"] + path,
        params={
            **(params or {}),
            "X-Plex-Token": current_app.config["PLEX_TOKEN"],
        },
        headers={"Accept": "application/json"},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()


def _plex_put(path, params):
    """Send one PUT to the local Plex server (metadata edits)."""

    r = requests.put(
        current_app.config["PLEX_URL"] + path,
        params={**params, "X-Plex-Token": current_app.config["PLEX_TOKEN"]},
        timeout=60,
    )
    r.raise_for_status()


def _tv_section_key():
    """Return the Plex library section that holds the TV shows, or None."""

    payload = _plex_get("/library/sections")
    for section in payload.get("MediaContainer", {}).get("Directory", []) or []:
        if section.get("type") == "show":
            return section.get("key")
    return None


def sync_plex_episode_titles():
    """Set the title of each Plex episode whose Fitzflix file has an edition.

    This task locks the title field. Thus, the agents cannot overwrite
    it. The task is safe to run at any time. It does not change the
    titles that are already current."""

    with app.app_context():
        if not (
            current_app.config.get("PLEX_URL") and current_app.config.get("PLEX_TOKEN")
        ):
            return True

        desired = {
            os.path.basename(file_path): edition
            for file_path, edition in db.session.query(File.file_path, File.edition)
            .filter(File.season.isnot(None), File.edition.isnot(None))
            .filter(File.edition != "")
        }

        # By design, this task does not fill titles from TMDB (reverted).
        # The episode titles in Plex come only from the agent. Fitzflix
        # stores no episode metadata of its own to push.

        if not desired:
            return True

        try:
            section = _tv_section_key()
            if section is None:
                current_app.logger.warning("Plex episode titles: no TV section found")
                return True

            updated = current = matched = 0
            start = 0
            while True:
                payload = _plex_get(
                    f"/library/sections/{section}/all",
                    params={
                        "type": 4,
                        "X-Plex-Container-Start": start,
                        "X-Plex-Container-Size": PAGE_SIZE,
                    },
                )
                container = payload.get("MediaContainer", {})
                page = container.get("Metadata", []) or []
                for episode in page:
                    title = None
                    for media in episode.get("Media", []) or []:
                        for part in media.get("Part", []) or []:
                            name = os.path.basename(part.get("file") or "")
                            if name in desired:
                                title = desired[name]
                                break
                        if title:
                            break
                    if title is None:
                        continue
                    matched += 1
                    if (episode.get("title") or "") == title:
                        current += 1
                        continue
                    _plex_put(
                        f"/library/sections/{section}/all",
                        {
                            "type": 4,
                            "id": episode["ratingKey"],
                            "title.value": title,
                            "title.locked": 1,
                        },
                    )
                    current_app.logger.info(
                        f"Plex episode titles: {episode.get('ratingKey')} "
                        f"{episode.get('title')!r} -> {title!r}"
                    )
                    updated += 1
                start += len(page)
                if not page or start >= container.get("totalSize", 0):
                    break
        except Exception:
            current_app.logger.warning(traceback.format_exc())
            return True

        if updated:
            current_app.logger.info(
                f"Plex episode titles: {updated} updated, {current} already "
                f"current, of {len(desired)} titled files ({matched} matched "
                f"in Plex)"
            )
        return True
