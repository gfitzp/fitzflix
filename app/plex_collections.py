"""Keep the Criterion Collection collection of Plex in step with the library.

Plex collections belong to one library. They do not sync between
libraries. Thus, this module puts the same collection in every Plex
movie library from one rule (#281). The rule is the rule of the
criterion_collection reporting view. A main-feature file belongs in the
collection when it is the best copy of its title group. Its quality must
be physical media. Its movie must have a custom poster and a Criterion
spine number that no other movie shares. Box sets that share 1 spine are not included. There
is no extras rule, because some box-set films have no extras of their
own.

The collection is a mirror. The task adds the films that the rule
selects, and it removes the films that it no longer selects. The match
uses the file path, because the guid lookup of this Plex server returns
nothing. If many selected files have no Plex item, the match is broken.
Then the task adds only. It does not empty the collection.
"""

import os
import traceback
import unicodedata

import requests

from flask import current_app
from werkzeug.local import LocalProxy

from app import db, get_app
from app.models import File, Movie, RefQuality, movie_file_rank
from app.plex_titles import _plex_get

app = LocalProxy(get_app)

PAGE_SIZE = 500

# The task removes nothing from a library where more than this part of
# the selected files has no Plex item.

MAX_UNMATCHED_FRACTION = 0.1

# The number of items in each add request. This keeps the URL short.

ADD_BATCH = 100


def _normal(path):
    """Return a path in one Unicode form. Then 2 spellings of a name match."""

    return unicodedata.normalize("NFC", path)


def criterion_paths():
    """Return the absolute paths of the files that belong in the collection."""

    shared = (
        db.session.query(Movie.criterion_spine_number)
        .filter(Movie.criterion_spine_number.isnot(None))
        .group_by(Movie.criterion_spine_number)
        .having(db.func.count(Movie.id) > 1)
    )
    ranked = (
        db.session.query(File.id.label("id"), movie_file_rank())
        .join(Movie, Movie.id == File.movie_id)
        .join(RefQuality, RefQuality.id == File.quality_id)
        .subquery()
    )
    rows = (
        db.session.query(File.file_path)
        .join(ranked, ranked.c.id == File.id)
        .join(Movie, Movie.id == File.movie_id)
        .join(RefQuality, RefQuality.id == File.quality_id)
        .filter(
            ranked.c.rank == 1,
            File.feature_type_id.is_(None),
            Movie.criterion_spine_number.isnot(None),
            Movie.criterion_spine_number.notin_(shared),
            Movie.custom_poster.isnot(None),
            RefQuality.physical_media == True,
        )
        .all()
    )
    library = current_app.config["LIBRARY_DIR"]
    return {_normal(os.path.join(library, path)) for (path,) in rows}


def _plex_request(method, path, params=None):
    """Send one write call (POST, PUT, or DELETE) to the local Plex server."""

    r = requests.request(
        method,
        current_app.config["PLEX_URL"] + path,
        params={
            **(params or {}),
            "X-Plex-Token": current_app.config["PLEX_TOKEN"],
        },
        headers={"Accept": "application/json"},
        timeout=60,
    )
    r.raise_for_status()
    return r.json() if r.content else {}


def _paged(path, params=None):
    """Yield every Metadata entry of a Plex list, one page at a time."""

    start = 0
    while True:
        container = _plex_get(
            path,
            params={
                **(params or {}),
                "X-Plex-Container-Start": start,
                "X-Plex-Container-Size": PAGE_SIZE,
            },
        ).get("MediaContainer", {})
        batch = container.get("Metadata", []) or []
        yield from batch
        start += len(batch)
        if not batch or start >= int(container.get("totalSize") or 0):
            return


def _items_by_path(key):
    """Return {part file path: ratingKey} for the movies of a section."""

    items = {}
    for item in _paged(f"/library/sections/{key}/all", {"type": 1}):
        for media in item.get("Media", []) or []:
            for part in media.get("Part", []) or []:
                if part.get("file"):
                    items[_normal(part["file"])] = item["ratingKey"]
    return items


def _uri(machine, keys):
    """Return the library URI that names a list of Plex items."""

    return (
        f"server://{machine}/com.plexapp.plugins.library/library/metadata/"
        + ",".join(keys)
    )


def _sync_section(section, wanted_paths, name, machine):
    """Make the collection of one movie section match the selected paths."""

    key = section["key"]
    title = section.get("title")
    by_path = _items_by_path(key)
    wanted = {by_path[path] for path in wanted_paths if path in by_path}
    unmatched = len([path for path in wanted_paths if path not in by_path])

    collection = next(
        (
            entry
            for entry in _plex_get(f"/library/sections/{key}/collections")
            .get("MediaContainer", {})
            .get("Metadata", [])
            or []
            if entry.get("title") == name
        ),
        None,
    )

    if collection is None:
        if not wanted:
            return
        first = sorted(wanted)[:ADD_BATCH]
        created = _plex_request(
            "POST",
            "/library/collections",
            {
                "type": 1,
                "title": name,
                "smart": 0,
                "sectionId": key,
                "uri": _uri(machine, first),
            },
        )
        collection_key = created["MediaContainer"]["Metadata"][0]["ratingKey"]
        members = set(first)
        added = len(first)
        current_app.logger.info(f"Plex collection '{name}' created in '{title}'")
    else:
        collection_key = collection["ratingKey"]
        members = {
            entry["ratingKey"]
            for entry in _paged(f"/library/collections/{collection_key}/children")
        }
        added = 0

    to_add = sorted(wanted - members)
    added += len(to_add)
    for start in range(0, len(to_add), ADD_BATCH):
        try:
            _plex_request(
                "PUT",
                f"/library/collections/{collection_key}/items",
                {"uri": _uri(machine, to_add[start : start + ADD_BATCH])},
            )
        except requests.HTTPError as error:
            if error.response is None or error.response.status_code != 400:
                raise

            # Plex refuses every add to a collection that lost its tag. It
            # keeps the collection item, but the item can hold no members.

            current_app.logger.warning(
                f"Plex collection '{name}' in '{title}' (ratingKey "
                f"{collection_key}) refuses new items. Its tag is probably "
                f"missing. Delete the collection in Plex, and the next sync "
                f"makes a new one"
            )
            return

    # A broken match makes the selected films look absent. Then a mirror
    # would empty the collection. Remove nothing in that case.

    to_remove = sorted(members - wanted)
    if wanted_paths and unmatched > len(wanted_paths) * MAX_UNMATCHED_FRACTION:
        current_app.logger.warning(
            f"Plex collection '{name}' in '{title}': {unmatched} of "
            f"{len(wanted_paths)} selected files have no Plex item. The sync "
            f"added films but removed none"
        )
        to_remove = []
    for member in to_remove:
        _plex_request("DELETE", f"/library/collections/{collection_key}/items/{member}")

    current_app.logger.info(
        f"Plex collection '{name}' in '{title}': {len(wanted)} films, "
        f"{added} added, "
        f"{len(to_remove)} removed"
    )


def sync_criterion_collection():
    """Mirror the Criterion selection into each Plex movie library (a queue task)."""

    with app.app_context():
        if not (
            current_app.config.get("PLEX_URL") and current_app.config.get("PLEX_TOKEN")
        ):
            return True

        try:
            from app.plex_player import _server_machine_id

            name = current_app.config["PLEX_CRITERION_COLLECTION"]
            wanted_paths = criterion_paths()
            machine = _server_machine_id()
            sections = _plex_get("/library/sections").get("MediaContainer", {})
        except Exception:
            current_app.logger.warning(traceback.format_exc())
            return True

        for section in sections.get("Directory", []) or []:
            if section.get("type") != "movie":
                continue
            try:
                _sync_section(section, wanted_paths, name, machine)
            except Exception:
                current_app.logger.warning(traceback.format_exc())
        return True


def enqueue_criterion_sync():
    """Queue the sync now. A custom poster change can add or remove a film."""

    if not (
        current_app.config.get("PLEX_URL") and current_app.config.get("PLEX_TOKEN")
    ):
        return
    current_app.maintenance_queue.enqueue(
        "app.plex_collections.sync_criterion_collection",
        job_timeout=600,
        description="Syncing the Criterion collection into Plex",
    )
