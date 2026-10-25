"""Keep the Criterion collections of Plex in step with the library.

Plex collections belong to one library. They do not sync between
libraries. Thus, this module puts the same collections in every Plex
movie library from rules in the database.

The main collection (#281) uses the rule of the criterion_collection
reporting view. A main-feature file belongs in it when it is the best
copy of its title group. Its quality must be physical media. Its movie
must have a custom poster and a Criterion spine number that no other
movie shares. Box sets that share 1 spine are not included. There is no
extras rule, because some box-set films have no extras of their own.

Each Criterion box set gets its own collection (#282), named for the
set. A film belongs in it when its movie has the set title and at least
1 main-feature file on physical media. That file does not have to be
the best copy. A set needs 2 films or more. The box-set collections sort
by release date.

The main collection sorts by spine number (#285). The sync keeps that
custom order, and a new film lands at its spine.

Each film is 1 item in the main collection (#284). Plex makes 1 item
for each edition of a film. The main collection uses the version with
no edition. A film with editions only uses the first edition by name.
The box-set collections show each edition as its own item, because the
discs of a set hold each version (#286).

Each collection is a mirror. The task adds the films that the rule
selects, and it removes the films that it no longer selects. The match
uses the file path, because the guid lookup of this Plex server returns
nothing. A film matches through any of its files. If many films have no
Plex item, the match is broken. Then the task adds only. It does not
empty the collection.
"""

import os
import traceback
import unicodedata
from collections import defaultdict

import requests

from flask import current_app
from werkzeug.local import LocalProxy

from app.tasks import enqueue_task
from app import db, get_app
from app.models import File, Movie, RefQuality, movie_file_rank
from app.plex_titles import _plex_get

app = LocalProxy(get_app)

PAGE_SIZE = 500

# The task removes nothing from a collection when more than this part of
# its selected films has no Plex item.

MAX_UNMATCHED_FRACTION = 0.1

# The number of items in each add request. This keeps the URL short.

ADD_BATCH = 100

# A box set with fewer films than this in the library gets no collection.

BOX_SET_MIN_FILMS = 2

# The collectionSort values of Plex for release date and custom order.

SORT_RELEASE = "0"
SORT_CUSTOM = "2"


def _normal(path):
    """Return a path in one Unicode form. Then 2 spellings of a name match."""

    return unicodedata.normalize("NFC", path)


def _one_version(rows):
    """Return {movie id: paths} with 1 version of each film (#284).

    The rows are (movie id, edition, path). The version with no edition
    has priority. A film with editions only keeps the first edition by
    name. Thus, an alternate cut does not become a second item."""

    editions = defaultdict(lambda: defaultdict(set))
    for movie_id, edition, path in rows:
        editions[movie_id][edition or ""].add(path)
    library = current_app.config["LIBRARY_DIR"]
    return {
        movie_id: {
            _normal(os.path.join(library, path)) for path in versions[min(versions)]
        }
        for movie_id, versions in editions.items()
    }


def criterion_paths():
    """Return {absolute path: spine number} for the files of the collection."""

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
        db.session.query(
            File.movie_id,
            File.edition,
            File.file_path,
            Movie.criterion_spine_number,
        )
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
    spines = {movie_id: spine for movie_id, _, _, spine in rows}
    films = _one_version(
        [(movie_id, edition, path) for movie_id, edition, path, _ in rows]
    )
    return {
        path: spines[movie_id] for movie_id, paths in films.items() for path in paths
    }


def box_set_groups():
    """Return {set title: [paths of 1 film, ...]} for each box set.

    A film is in its set when it has at least 1 main-feature file on
    physical media. Each film gives all of its main-feature paths, of
    every edition (#286). Then each edition is an item of the set, and a
    film matches through the file that Plex has, also a digital copy.
    """

    on_disc = (
        db.session.query(File.movie_id)
        .join(RefQuality, RefQuality.id == File.quality_id)
        .filter(File.feature_type_id.is_(None), RefQuality.physical_media == True)
    )
    rows = (
        db.session.query(Movie.criterion_set_title, Movie.id, File.file_path)
        .join(File, File.movie_id == Movie.id)
        .filter(
            Movie.criterion_set_title.isnot(None),
            File.feature_type_id.is_(None),
            Movie.id.in_(on_disc),
        )
        .all()
    )
    library = current_app.config["LIBRARY_DIR"]
    sets = defaultdict(lambda: defaultdict(set))
    for set_title, movie_id, path in rows:
        sets[set_title][movie_id].add(_normal(os.path.join(library, path)))
    return {
        set_title: list(films.values())
        for set_title, films in sets.items()
        if len(films) >= BOX_SET_MIN_FILMS
    }


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


def _sync_collection(
    section, collections, by_path, groups, name, machine, sort=None, order=None
):
    """Make 1 collection of 1 movie section match the selected films.

    Each group holds the paths of 1 film. A film is in the collection
    through each Plex item that one of its paths matches. The collections
    argument maps the titles of the section to its collection entries.
    With order ({path: sort key}), the collection uses custom order, and
    the items follow the sort keys (#285).
    """

    key = section["key"]
    title = section.get("title")
    wanted = {by_path[path] for group in groups for path in group if path in by_path}
    unmatched = len([group for group in groups if not any(p in by_path for p in group)])

    collection = collections.get(name)
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
        collection = created["MediaContainer"]["Metadata"][0]
        collection_key = collection["ratingKey"]
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
    if groups and unmatched > len(groups) * MAX_UNMATCHED_FRACTION:
        current_app.logger.warning(
            f"Plex collection '{name}' in '{title}': {unmatched} of "
            f"{len(groups)} selected films have no Plex item. The sync "
            f"added films but removed none"
        )
        to_remove = []
    for member in to_remove:
        _plex_request("DELETE", f"/library/collections/{collection_key}/items/{member}")

    if order is not None:
        sort = SORT_CUSTOM
    if sort is not None and collection.get("collectionSort") != sort:
        _plex_request(
            "PUT",
            f"/library/metadata/{collection_key}/prefs",
            {"collectionSort": sort},
        )
    if order is not None:
        _arrange(collection_key, by_path, order)

    current_app.logger.info(
        f"Plex collection '{name}' in '{title}': {len(wanted)} items, "
        f"{added} added, {len(to_remove)} removed"
    )


def _arrange(collection_key, by_path, order):
    """Move the items of a collection into the order of their sort keys.

    The function compares the Plex order first. It moves nothing when the
    orders agree. An item without a sort key goes to the end."""

    keys = {}
    for path, key in order.items():
        if path in by_path:
            keys.setdefault(by_path[path], key)
    current = [
        entry["ratingKey"]
        for entry in _paged(f"/library/collections/{collection_key}/children")
    ]
    wanted = sorted(
        current,
        key=lambda item: (item not in keys, keys.get(item, 0), int(item)),
    )
    if current == wanted:
        return
    previous = None
    for item in wanted:
        _plex_request(
            "PUT",
            f"/library/collections/{collection_key}/items/{item}/move",
            {"after": previous} if previous else None,
        )
        previous = item


def sync_criterion_collection():
    """Mirror the Criterion collections into each Plex movie library (a queue task).

    These are the main collection (#281) and 1 collection for each box
    set (#282)."""

    with app.app_context():
        if not (
            current_app.config.get("PLEX_URL") and current_app.config.get("PLEX_TOKEN")
        ):
            return True

        try:
            from app.plex_player import _server_machine_id

            # The main collection: 1 group for each file. Each edition is
            # its own Plex item.

            spines = criterion_paths()
            plans = [
                (
                    current_app.config["PLEX_CRITERION_COLLECTION"],
                    [{path} for path in sorted(spines)],
                    None,
                    spines,
                )
            ]
            for set_title, groups in sorted(box_set_groups().items()):
                plans.append((set_title, groups, SORT_RELEASE, None))
            machine = _server_machine_id()
            sections = _plex_get("/library/sections").get("MediaContainer", {})
        except Exception:
            current_app.logger.warning(traceback.format_exc())
            return True

        for section in sections.get("Directory", []) or []:
            if section.get("type") != "movie":
                continue
            try:
                by_path = _items_by_path(section["key"])
                collections = {
                    entry.get("title"): entry
                    for entry in _plex_get(
                        f"/library/sections/{section['key']}/collections"
                    )
                    .get("MediaContainer", {})
                    .get("Metadata", [])
                    or []
                }
            except Exception:
                current_app.logger.warning(traceback.format_exc())
                continue
            for name, groups, sort, order in plans:
                try:
                    _sync_collection(
                        section,
                        collections,
                        by_path,
                        groups,
                        name,
                        machine,
                        sort,
                        order,
                    )
                except Exception:
                    current_app.logger.warning(traceback.format_exc())
        return True


def enqueue_criterion_sync():
    """Queue the sync now. A custom poster change can add or remove a film."""

    if not (
        current_app.config.get("PLEX_URL") and current_app.config.get("PLEX_TOKEN")
    ):
        return
    enqueue_task(
        "app.plex_collections.sync_criterion_collection",
        description="Syncing the Criterion collections into Plex",
    )
