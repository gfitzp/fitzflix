"""Keep the Plex item of a title that Fitzflix renames.

Plex sees a rename as one file gone and one file new. It matches the new
file again. When the match gives the guid that the old item had, the old
item takes the new file. Its add date, its collections and its art stay.
When the match gives another guid, Plex makes a new item with a new add
date. The film then shows as recently added.

A comparison of the Plex database backups on 2026-11-06 showed both
cases. The African Queen kept its item through two year changes. Plex
had the folder "War and Peace (1968)" matched to a Hindi film of 1968.
The corrected year moved its match to the right film, and Plex made a
new item with a new add date.

Thus, a rename does two things here. Before the file moves, it pins the
match of the Plex item to the TMDB id of the record. The Fix Match
search of Plex accepts "tmdb-<id>" as the title. The next scan then
gives the renamed file to the same item. After that scan, the refresh
looks at the new path. When Plex made a new item there in spite of the
pin, the refresh writes the old add date back and locks it.

Both steps are best-effort, like every other Plex step. A failure costs
one film in Recently Added, never the rename.
"""

import json
import os
import time
import traceback

from flask import current_app

from app.plex_api import plex_command, plex_get
from app.plex_library import PAGE_SIZE, _part_files, _sections_holding, _tail

# The renames that wait for their scan. A Redis hash: the new path of the
# title gives a JSON record with the rating key and the add date of the
# Plex item before the rename.

PENDING_KEY = "fitzflix:plex:renamed"

# A rename that no scan lists within this time leaves the hash. The
# quarter-hourly refresh scans the library many times in a day.

PENDING_HOURS = 24

# Plex fetches the metadata of a new match in the background. The pin
# waits this long for the id to show on the item. Then the rename goes
# on, and the add-date guard covers the rest.

MATCH_WAIT_SECONDS = 12
MATCH_POLL_SECONDS = 2

AGENTS = {"movie": "tv.plex.agents.movie", "show": "tv.plex.agents.series"}

# The item type of each kind in a section page, and in the add-date edit.

ITEM_TYPES = {"movie": 1, "show": 2}

# The part files of a show section hang on its episodes.

PAGE_TYPES = {"movie": 1, "show": 4}

_sleep = time.sleep


def plex_configured():
    """Return True when the settings name a Plex server and a token."""

    return bool(
        current_app.config.get("PLEX_URL") and current_app.config.get("PLEX_TOKEN")
    )


def _sections(kind):
    """Return the Plex sections of this kind, "movie" or "show"."""

    payload = plex_get("/library/sections")
    return [
        section
        for section in payload.get("MediaContainer", {}).get("Directory", []) or []
        if section.get("type") == kind
    ]


def _pages(section_key, item_type):
    """Yield every item of a section, page by page."""

    start = 0
    while True:
        payload = plex_get(
            f"/library/sections/{section_key}/all",
            params={
                "type": item_type,
                "X-Plex-Container-Start": start,
                "X-Plex-Container-Size": PAGE_SIZE,
            },
        )
        container = payload.get("MediaContainer", {})
        page = container.get("Metadata", []) or []
        yield from page
        start += len(page)
        if not page or start >= container.get("totalSize", 0):
            return


def _index(section, kind):
    """Return the paths of a section with the rating key of their item.

    A movie section maps each part file to its movie. A show section
    maps the folder of each episode, and the folder above it, to the
    show. The first item that names a path keeps it."""

    index = {}
    for item in _pages(section["key"], PAGE_TYPES[kind]):
        if kind == "movie":
            keys = [os.path.abspath(path) for path in _part_files(item)]
            holder = item.get("ratingKey")
        else:
            keys = []
            for path in _part_files(item):
                season = os.path.dirname(os.path.abspath(path))
                keys.extend([season, os.path.dirname(season)])
            holder = item.get("grandparentRatingKey")
        for key in keys:
            if holder:
                index.setdefault(key, holder)
    return index


def _item(rating_key, section_key):
    """Return the item of a rating key: its add date, guid, edition, and external ids.

    Plex gives an edition item the guid of the film. Only its
    editionTitle tells it apart from the theatrical item (#679)."""

    payload = plex_get(f"/library/metadata/{rating_key}")
    meta = (payload.get("MediaContainer", {}).get("Metadata") or [{}])[0]
    return {
        "rating_key": str(rating_key),
        "section": str(section_key),
        "added_at": meta.get("addedAt"),
        "guid": meta.get("guid") or "",
        "edition": meta.get("editionTitle") or "",
        "ids": {guid.get("id") for guid in meta.get("Guid", []) or [] if guid},
    }


def find_plex_item(path, kind, cache=None):
    """Return the Plex item at a library path, or None.

    For a movie, path is the file. For a show, path is the show folder.
    The lookup takes the full path first. When Plex reaches the library
    by another mount, the last components identify it: the folder and
    the file of a movie, or the folder of a show. cache keeps the index
    of each section across the calls of one run."""

    path = os.path.abspath(path)
    parts = 2 if kind == "movie" else 1
    cache = {} if cache is None else cache
    for section in _sections_holding(_sections(kind), {path}):
        if section["key"] not in cache:
            cache[section["key"]] = _index(section, kind)
        index = cache[section["key"]]
        rating_key = index.get(path)
        if rating_key is None:
            tail = _tail(path, parts)
            rating_key = next(
                (key for known, key in index.items() if _tail(known, parts) == tail),
                None,
            )
        if rating_key:
            return _item(rating_key, section["key"])
    return None


def pin_plex_match(item, kind, source):
    """Fix the match of a Plex item to an external id, for example "tmdb-29266".

    Return True when the item carries the id. An item that carries it
    already needs no request. The Fix Match search of Plex takes the id
    as its title. The first result is the match. Plex fetches the
    metadata of the match in the background. The function waits for
    the id to show on the item, up to MATCH_WAIT_SECONDS."""

    wanted = source.replace("-", "://", 1)
    if wanted in item["ids"]:
        return True
    label = f"Plex item {item['rating_key']}"
    if item["edition"]:
        # Fitzflix does not pin an edition item, because the effect of a
        # new match on its edition is not proven (#679).
        current_app.logger.info(
            f"{label} is the edition '{item['edition']}'. Its match stays"
        )
        return False
    payload = plex_get(
        f"/library/metadata/{item['rating_key']}/matches",
        params={
            "manual": 1,
            "title": source,
            "agent": AGENTS[kind],
            "language": "en-US",
        },
        timeout=60,
    )
    results = payload.get("MediaContainer", {}).get("SearchResult") or []
    match = results[0] if results else {}
    if not match.get("guid"):
        current_app.logger.warning(
            f"{label}: Plex finds no match for {source}. Its match stays"
        )
        return False
    params = {"guid": match["guid"], "name": match.get("name") or ""}
    if match.get("year"):
        params["year"] = match["year"]
    plex_command("PUT", f"/library/metadata/{item['rating_key']}/match", params)

    deadline = time.monotonic() + MATCH_WAIT_SECONDS
    while True:
        _sleep(MATCH_POLL_SECONDS)
        fresh = _item(item["rating_key"], item["section"])
        if wanted in fresh["ids"]:
            current_app.logger.info(
                f"{label} now matches {match['guid']} ({source}), "
                f"before: {item['guid'] or 'no guid'}"
            )
            return True
        if time.monotonic() >= deadline:
            current_app.logger.warning(
                f"{label}: the match of {source} did not show within "
                f"{MATCH_WAIT_SECONDS} s. The add-date guard covers the rename"
            )
            return False


def remember_rename(old_path, new_path, kind, tmdb_id=None, tvdb_id=None):
    """Pin the match of the Plex item at old_path, and record it for new_path.

    The caller renames a movie file or a show folder right after this.
    The refresh settles the record after the next scan. This function
    never raises. A Plex that does not answer costs the pin, not the
    rename."""

    if not plex_configured():
        return
    try:
        item = find_plex_item(old_path, kind)
        if item is None:
            current_app.logger.info(
                f"Plex lists no {kind} at '{old_path}'. Nothing to keep"
            )
            return
        source = None
        if tmdb_id:
            source = f"tmdb-{tmdb_id}"
        elif tvdb_id:
            source = f"tvdb-{tvdb_id}"
        if source:
            pin_plex_match(item, kind, source)
        current_app.redis.hset(
            PENDING_KEY,
            os.path.abspath(new_path),
            json.dumps(
                {
                    "rating_key": item["rating_key"],
                    "added_at": item["added_at"],
                    "kind": kind,
                    "since": int(time.time()),
                }
            ),
        )
    except Exception:
        current_app.logger.warning(traceback.format_exc())


def _settle(new_path, record, cache):
    """Settle one rename after a scan. Return True when the record is done."""

    kind = record["kind"]
    name = os.path.basename(new_path)
    if time.time() - record.get("since", 0) > PENDING_HOURS * 3600:
        current_app.logger.warning(
            f"Plex listed no {kind} at '{new_path}' within {PENDING_HOURS} h. "
            f"Its add date stays as Plex set it"
        )
        return True
    item = find_plex_item(new_path, kind, cache)
    if item is None:
        return False
    if item["rating_key"] == record["rating_key"]:
        current_app.logger.info(
            f"'{name}' kept Plex item {item['rating_key']} through its rename"
        )
        return True
    old = record.get("added_at")
    if old and item["added_at"] and item["added_at"] > old:
        plex_command(
            "PUT",
            f"/library/sections/{item['section']}/all",
            {
                "type": ITEM_TYPES[kind],
                "id": item["rating_key"],
                "addedAt.value": old,
                "addedAt.locked": 1,
            },
        )
        current_app.logger.info(
            f"'{name}' is the new Plex item {item['rating_key']} (before: "
            f"{record['rating_key']}). Its add date goes back to "
            f"{time.strftime('%Y-%m-%d', time.localtime(old))} and is locked"
        )
    return True


def restore_add_dates():
    """Give each renamed title that Plex made new its old add date back.

    The refresh calls this before it asks for a scan. Thus, the records
    of the previous run meet the result of the previous scan. A title
    that no scan lists yet waits for the next run."""

    if not plex_configured():
        return
    redis = current_app.redis
    pending = redis.hgetall(PENDING_KEY)
    cache = {}
    for field, raw in pending.items():
        new_path = field.decode()
        try:
            done = _settle(new_path, json.loads(raw), cache)
        except Exception:
            current_app.logger.warning(traceback.format_exc())
            continue
        if done:
            redis.hdel(PENDING_KEY, new_path)
