"""TV episode titles into Plex from Fitzflix (#68).

Some episode filenames carry a title segment — "Series - SxxEyy -
Title [Quality]" — which the filename parse stores in file.edition;
the Doctor Who (1963) specials are the population (custom episode
numbers no agent will ever title). Glenn's rule: wherever a TV file
carries an edition, that IS the episode's title, any show, any
season. A nightly sweep writes it to Plex through the local API with
the title field locked, replacing the external cron that wrote Plex's
SQLite directly (with a hardcoded 70-character path offset that would
have broken on any library move).

Episodes are matched to files by Part-file BASENAME — never by title
(Plex titles the classic series just "Doctor Who"; the "(1963)" lives
only in the folder name).
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
    """One authenticated JSON GET against the local Plex server."""

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
    """One PUT against the local Plex server (metadata edits)."""

    r = requests.put(
        current_app.config["PLEX_URL"] + path,
        params={**params, "X-Plex-Token": current_app.config["PLEX_TOKEN"]},
        timeout=60,
    )
    r.raise_for_status()


def _tv_section_key():
    """The Plex library section holding TV shows, or None."""

    payload = _plex_get("/library/sections")
    for section in payload.get("MediaContainer", {}).get("Directory", []) or []:
        if section.get("type") == "show":
            return section.get("key")
    return None


def sync_plex_episode_titles():
    """Task: title every Plex episode whose Fitzflix file carries an
    edition, and lock the field so agents can't overwrite it. Safe to
    run any time; current titles are left untouched."""

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
