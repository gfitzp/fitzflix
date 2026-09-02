"""Sync the Plex watchlist and the Fitzflix watchlist in both directions.

There is one watchlist at the account level. The sync keeps the two
ends in agreement.

Plex watchlists live on the plex.tv ACCOUNT (the discover API), not on
the local server. Thus, the sync pairs the account of the configured
PLEX_TOKEN with the Fitzflix user whose plex_username matches it. Each
run is a two-way reconcile against the last-synced snapshot. The next
state is (kept by both) plus (new adds from each side). The sync pushes
each side to that state. Then the snapshot advances to the films that
both sides hold. The FIRST run has an empty snapshot. Thus, the same
formula gives a full union with no special case. Glenn chose this
bootstrap. Films that fail to push stay out of the snapshot. Thus, they
retry on each run. The sync never reads them as a removal.
"""

import json
import traceback

import requests

from flask import current_app
from werkzeug.local import LocalProxy

from app import db, get_app
from app.models import Movie, User, UserWatchlist

app = LocalProxy(get_app)

DISCOVER_URL = "https://discover.provider.plex.tv"
MATCHES_URL = "https://metadata.provider.plex.tv/library/metadata/matches"
ACCOUNT_URL = "https://plex.tv/api/v2/user"
SNAPSHOT_KEY = "fitzflix:plex:watchlist:synced"

# Films that cannot sync because of their structure. Plex accepted the
# add, but the item never appears. Buried Loot (1935) was the first
# case. These films stay on the Fitzflix watchlist. The sync skips them
# without churn. Delete the set to retry them.

UNSYNCABLE_KEY = "fitzflix:plex:watchlist:unsyncable"
PAGE_SIZE = 100

# A Plex listing much smaller than the snapshot is an API anomaly. It
# is not a mass removal. Do not propagate deletions from it.

ANOMALY_FLOOR = 10


def _plex_get(url, params=None):
    """Do 1 authenticated JSON GET against a plex.tv API."""

    r = requests.get(
        url,
        params={**(params or {}), "X-Plex-Token": current_app.config["PLEX_TOKEN"]},
        headers={"Accept": "application/json"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def _plex_put(path, rating_key):
    """Do 1 watchlist action (addToWatchlist or removeFromWatchlist)."""

    r = requests.put(
        f"{DISCOVER_URL}/actions/{path}",
        params={
            "ratingKey": rating_key,
            "X-Plex-Token": current_app.config["PLEX_TOKEN"],
        },
        headers={"Accept": "application/json"},
        timeout=30,
    )
    r.raise_for_status()


def fetch_plex_watchlist(fitzflix_ids=(), snapshot=()):
    """Return {tmdb_id: {title, year, rating_key}} for the full watchlist
    of the account.

    This reads the pages one by one. The API silently limits a page to
    20 items. This reads the tmdb ids from the bulk includeGuids
    payload. This returns movies only. The account watchlist also holds
    TV shows. The tmdb guids of a TV show are TMDB TV-series ids, not
    film ids.

    An item can carry SEVERAL tmdb guids. For example, The Animatrix
    shows the compilation and all 9 segments. Thus, the ids that
    Fitzflix already tracks represent the item. If there are none, the
    ids that the snapshot knows represent it. For a new item, the first
    guid represents it. This does not use the full set of guids. The
    full set would add phantom segment films to the Fitzflix
    watchlist."""

    fitzflix_ids = set(fitzflix_ids)
    snapshot = set(snapshot)
    films = {}
    start = 0
    while True:
        payload = _plex_get(
            f"{DISCOVER_URL}/library/sections/watchlist/all",
            params={
                "includeGuids": 1,
                "X-Plex-Container-Start": start,
                "X-Plex-Container-Size": PAGE_SIZE,
            },
        )
        container = payload.get("MediaContainer", {})
        page = container.get("Metadata", []) or []
        for item in page:
            # The tmdb guid of a show is a TV-series id. As a film, it
            # becomes a bare Movie row (The Flight Attendant, Severance).
            if item.get("type") != "movie":
                continue
            item_ids = [
                int(guid["id"][7:])
                for guid in item.get("Guid", []) or []
                if (guid.get("id") or "").startswith("tmdb://")
            ]
            if not item_ids:
                continue
            represented = (
                (set(item_ids) & fitzflix_ids)
                or (set(item_ids) & snapshot)
                or {item_ids[0]}
            )
            for tmdb_id in represented:
                films[tmdb_id] = {
                    "title": item.get("title"),
                    "year": item.get("year"),
                    "rating_key": item.get("ratingKey"),
                }
        start += len(page)
        if not page or start >= container.get("totalSize", 0):
            break
    return films


def plex_rating_key(tmdb_id):
    """Return the discover ratingKey for a TMDB film, or None.

    The matches endpoint resolves tmdb://<id> in a deterministic way.
    The tail of the plex guid is the key."""

    payload = _plex_get(MATCHES_URL, params={"type": 1, "guid": f"tmdb://{tmdb_id}"})
    for item in payload.get("MediaContainer", {}).get("Metadata", []) or []:
        guid = item.get("guid") or ""
        if guid.startswith("plex://"):
            return guid.rsplit("/", 1)[-1]
    return None


def _sync_user():
    """Return the Fitzflix user whose plex_username matches the plex.tv
    account of the token, or None (logged).

    The watchlist is at the account level. Thus, only the list of that
    user can sync."""

    account = _plex_get(ACCOUNT_URL)
    username = (account.get("username") or "").strip().lower()
    if not username:
        current_app.logger.warning("Plex watchlist: token account has no username")
        return None
    user = User.query.filter(db.func.lower(User.plex_username) == username).first()
    if user is None:
        current_app.logger.warning(
            f"Plex watchlist: no Fitzflix user maps to plex.tv account "
            f"'{username}' — set it on the profile page"
        )
    return user


def sync_plex_watchlist():
    """Reconcile the Plex and Fitzflix watchlists in both directions.

    This is a task. It is safe to run at any time. A failed run does not
    change the snapshot. Thus, the sync never reads a failure as a
    removal.
    """

    with app.app_context():
        if not current_app.config.get("PLEX_TOKEN"):
            return True

        snapshot_raw = current_app.redis.get(SNAPSHOT_KEY)
        snapshot = set(json.loads(snapshot_raw)) if snapshot_raw else set()
        unsyncable = {
            int(member) for member in current_app.redis.smembers(UNSYNCABLE_KEY)
        }

        try:
            user = _sync_user()
        except Exception:
            current_app.logger.warning(traceback.format_exc())
            return True
        if user is None:
            return True

        fitzflix = {
            tmdb_id: (movie_id, watchlist_id)
            for tmdb_id, movie_id, watchlist_id in db.session.query(
                Movie.tmdb_id, Movie.id, UserWatchlist.id
            )
            .join(UserWatchlist, UserWatchlist.movie_id == Movie.id)
            .filter(UserWatchlist.user_id == user.id, Movie.tmdb_id.isnot(None))
        }

        try:
            plex = fetch_plex_watchlist(fitzflix, snapshot)
        except Exception:
            current_app.logger.warning(traceback.format_exc())
            return True

        if not plex and len(snapshot) > ANOMALY_FLOOR:
            current_app.logger.warning(
                f"Plex watchlist: API returned 0 items against a "
                f"{len(snapshot)}-item snapshot — anomaly, skipping this run"
            )
            return True

        plex_ids = set(plex)
        fitz_ids = set(fitzflix)

        # The reconcile: the films that both sides kept, plus the new
        # adds from each side. On the first run, the snapshot is empty.
        # Thus, this is the union.

        target = (plex_ids & fitz_ids) | (plex_ids - snapshot) | (fitz_ids - snapshot)

        added_here = removed_here = pushed = pulled = failed = 0
        synced = set(target)
        created_movies = []

        # The Fitzflix side: add the films that Plex has. Remove the
        # films that Plex removed.

        from app.videos import find_or_create_tmdb_movie

        for tmdb_id in sorted(target - fitz_ids):
            item = plex.get(tmdb_id) or {}
            movie = Movie.query.filter_by(tmdb_id=tmdb_id).first()
            if movie is None:
                movie, created = find_or_create_tmdb_movie(
                    tmdb_id, item.get("title") or f"TMDB {tmdb_id}", item.get("year")
                )
                if movie is None:
                    synced.discard(tmdb_id)
                    failed += 1
                    continue
                if created:
                    db.session.flush()
                    created_movies.append((movie.id, tmdb_id))
            db.session.add(UserWatchlist(user_id=user.id, movie_id=movie.id))
            added_here += 1

        for tmdb_id in sorted(fitz_ids - target):
            _, watchlist_id = fitzflix[tmdb_id]
            row = db.session.get(UserWatchlist, watchlist_id)
            if row is not None:
                db.session.delete(row)
            removed_here += 1
        db.session.commit()

        # The Plex side: push the new Fitzflix adds. Remove the films
        # that Fitzflix removed. Match failures stay OUT of the
        # snapshot. Thus, they retry on the next run. The sync does not
        # read them as removals later. The sync skips known unsyncable
        # films without churn.

        attempted = set()
        for tmdb_id in sorted(target - plex_ids):
            if tmdb_id in unsyncable:
                synced.discard(tmdb_id)
                continue
            try:
                rating_key = plex_rating_key(tmdb_id)
                if rating_key is None:
                    raise LookupError(f"no Plex match for tmdb {tmdb_id}")
                _plex_put("addToWatchlist", rating_key)
                attempted.add(tmdb_id)
                pushed += 1
            except Exception as e:
                current_app.logger.warning(
                    f"Plex watchlist: couldn't add tmdb {tmdb_id}: {e}"
                )
                synced.discard(tmdb_id)
                failed += 1

        for tmdb_id in sorted(plex_ids - target):
            try:
                _plex_put("removeFromWatchlist", plex[tmdb_id]["rating_key"])
                pulled += 1
            except Exception as e:
                current_app.logger.warning(
                    f"Plex watchlist: couldn't remove tmdb {tmdb_id}: {e}"
                )
                failed += 1

        # Make sure that the pushes arrived. Plex answers 200 for some
        # adds and then silently drops them (Buried Loot, 1935). A
        # phantom add in the snapshot would read as a Plex-side removal
        # on the next run. That would delete the film from the Fitzflix
        # watchlist. Thus, phantoms leave the snapshot and go into the
        # unsyncable set instead.

        if attempted:
            try:
                landed = fetch_plex_watchlist(fitz_ids | attempted, snapshot)
            except Exception:
                current_app.logger.warning(traceback.format_exc())
                landed = None
            if landed is None:
                # The sync cannot verify. Keep the pushes OUT of the
                # snapshot. Thus, the next run verifies them again.
                # This prevents the phantom-removal trap.

                synced -= attempted
            else:
                for tmdb_id in sorted(attempted - set(landed)):
                    current_app.logger.warning(
                        f"Plex watchlist: add for tmdb {tmdb_id} answered OK "
                        f"but never appeared — marking unsyncable (it stays "
                        f"on the Fitzflix watchlist; clear {UNSYNCABLE_KEY} "
                        f"to retry)"
                    )
                    current_app.redis.sadd(UNSYNCABLE_KEY, tmdb_id)
                    synced.discard(tmdb_id)
                    pushed -= 1
                    failed += 1

        current_app.redis.set(SNAPSHOT_KEY, json.dumps(sorted(synced)))

        for movie_id, tmdb_id in created_movies:
            current_app.request_queue.enqueue(
                "app.videos.refresh_tmdb_info",
                args=("Movies", movie_id, tmdb_id),
                job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
                description=f"Refreshing TMDB data for movie {movie_id}",
            )

        if added_here or removed_here or pushed or pulled or failed:
            current_app.logger.info(
                f"Plex watchlist sync for '{user.plex_username}': "
                f"+{added_here}/-{removed_here} here, "
                f"+{pushed}/-{pulled} at Plex, {failed} failed, "
                f"snapshot {len(synced)}"
            )
        return True
