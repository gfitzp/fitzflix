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
import time
import traceback

import requests

from flask import current_app
from rq.timeouts import BaseTimeoutException
from werkzeug.local import LocalProxy

from app.tasks import enqueue_task
from app import db, get_app
from app.models import Movie, User, UserWatchlist

app = LocalProxy(get_app)

DISCOVER_URL = "https://discover.provider.plex.tv"
MATCHES_URL = "https://metadata.provider.plex.tv/library/metadata/matches"
ACCOUNT_URL = "https://plex.tv/api/v2/user"
SNAPSHOT_KEY = "fitzflix:plex:watchlist:synced"

# Films that cannot sync because of their structure. Plex accepted the
# add, but the item never appears (Buried Loot, 1935). Or Plex refused
# the add with a client error (A Turning Point, 1984). These films stay on the Fitzflix watchlist. The sync skips them
# without churn. Delete the set to retry them.

UNSYNCABLE_KEY = "fitzflix:plex:watchlist:unsyncable"
PAGE_SIZE = 100

# A Plex listing much smaller than the snapshot is an API anomaly. It
# is not a mass removal. The walk refuses a listing that holds fewer
# items than the totalSize of page 1 (ListingAnomaly). The sync also
# skips a run whose listing holds less than half of the snapshot, if
# the snapshot holds more than ANOMALY_FLOOR films. A real mass removal
# gives the same short listing on the next run. Thus, the sync accepts
# a short listing when SHRINK_KEY holds the same listing from the run
# before.

ANOMALY_FLOOR = 10
SHRINK_KEY = "fitzflix:plex:watchlist:shrink"
SHRINK_TTL = 2 * 24 * 3600

# The walk reads the listing again from page 1 when the totalSize
# changes between pages. It gives up after this number of reads.

LISTING_READS = 3

# Plex refuses some matched films with a client error (A Turning Point,
# 1984, gets a 400). A retry gets the same answer. Thus, these codes put
# the film into the unsyncable set. Auth and rate-limit codes are about
# the token or the load. They are not about the film, so they retry.

RETRYABLE_CLIENT_ERRORS = {401, 403, 408, 429}

# These codes are about the token or the load. The next request gets
# the same answer. Thus, the push loop stops at the first one. A 429
# with Retry-After also holds off the next runs for that time, up to
# BACKOFF_MAX seconds.

STOP_CODES = {401, 403, 429}
BACKOFF_KEY = "fitzflix:plex:watchlist:backoff"
BACKOFF_MAX = 24 * 3600

# A plex.tv API sometimes answers 1 page of a long watchlist with a
# server error (a 503). The same request usually succeeds a moment
# later. Thus, a GET tries again after each of these waits, in seconds.

GET_RETRY_WAITS = (2, 5)


def _refused_by_plex(error):
    """Return True if Plex refused the request for this item.

    That is an HTTP 4xx that is not about the token or the load."""

    response = getattr(error, "response", None)
    if not isinstance(error, requests.HTTPError) or response is None:
        return False
    status = response.status_code
    return 400 <= status < 500 and status not in RETRYABLE_CLIENT_ERRORS


def _stop_status(error):
    """Return the HTTP status if the push loop must stop for it, or None."""

    response = getattr(error, "response", None)
    if not isinstance(error, requests.HTTPError) or response is None:
        return None
    if response.status_code not in STOP_CODES:
        return None
    if response.status_code == 429:
        try:
            wait = int(response.headers.get("Retry-After") or 0)
        except ValueError:
            wait = 0
        if wait > 0:
            current_app.redis.set(BACKOFF_KEY, 1, ex=min(wait, BACKOFF_MAX))
    return response.status_code


def _transient(error):
    """Return True if the same GET can succeed a moment later.

    That is a server error (HTTP 5xx), a dropped connection, or a
    timeout."""

    if isinstance(error, (requests.ConnectionError, requests.Timeout)):
        return True
    response = getattr(error, "response", None)
    return (
        isinstance(error, requests.HTTPError)
        and response is not None
        and response.status_code >= 500
    )


class ListingAnomaly(Exception):
    """The Plex listing is incomplete. The sync must not use it."""


def _plex_get(url, params=None):
    """Do 1 authenticated JSON GET against a plex.tv API.

    A transient failure tries again after each wait in GET_RETRY_WAITS.
    Then the last error goes to the caller."""

    for wait in (*GET_RETRY_WAITS, None):
        try:
            r = requests.get(
                url,
                params={
                    **(params or {}),
                    "X-Plex-Token": current_app.config["PLEX_TOKEN"],
                },
                headers={"Accept": "application/json"},
                timeout=30,
            )
            r.raise_for_status()
            return r.json()
        except requests.RequestException as error:
            if wait is None or not _transient(error):
                raise
            time.sleep(wait)


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
    for _ in range(LISTING_READS):
        films = _read_listing(fitzflix_ids, snapshot)
        if films is not None:
            return films
    raise ListingAnomaly(
        f"the Plex watchlist changed size during each of {LISTING_READS} reads"
    )


def _read_listing(fitzflix_ids, snapshot):
    """Read the full listing 1 time. Return the films, or None.

    None tells the caller that the totalSize changed between pages. A
    removal on Plex between 2 pages moves the later items 1 position
    back. Thus, an offset walk across the change can skip an item. An
    empty page before the walk reaches the totalSize of page 1 raises
    ListingAnomaly. A first page with items and no totalSize also
    raises it, as does a totalSize under the items of page 1 (#544)."""

    films = {}
    start = 0
    total = None
    while True:
        payload = _plex_get(
            f"{DISCOVER_URL}/library/sections/watchlist/all",
            params={
                "includeGuids": 1,
                "X-Plex-Container-Start": start,
                "X-Plex-Container-Size": PAGE_SIZE,
            },
        )
        container = payload.get("MediaContainer", {}) or {}
        page = container.get("Metadata", []) or []
        size = container.get("totalSize")
        if total is None:
            if size is None:
                if page:
                    raise ListingAnomaly("page 1 has items but no totalSize")
                return films
            total = int(size)
            # A count under the items of page 1 is wrong (#544). Then a
            # short page passed as complete, and its missing films became
            # removals.
            if len(page) > total:
                raise ListingAnomaly(
                    f"page 1 has {len(page)} items but totalSize {total}"
                )
        elif size is not None and int(size) != total:
            return None
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
        if start >= total:
            return films
        if not page:
            raise ListingAnomaly(
                f"the listing stopped at item {start} of {total} items"
            )


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


def _short_listing(plex, snapshot):
    """Return True if the run must stop because the listing is short.

    A listing with less than half of a snapshot of more than
    ANOMALY_FLOOR films is short. The run after the first short listing
    accepts the same listing. That is a real mass removal on Plex."""

    redis = current_app.redis
    if len(snapshot) <= ANOMALY_FLOOR or len(plex) >= len(snapshot) // 2:
        redis.delete(SHRINK_KEY)
        return False
    listing = json.dumps(sorted(plex))
    previous = redis.get(SHRINK_KEY)
    if (
        previous is not None
        and (previous.decode() if isinstance(previous, bytes) else previous) == listing
    ):
        redis.delete(SHRINK_KEY)
        current_app.logger.warning(
            f"Plex watchlist: the listing of {len(plex)} items against a "
            f"{len(snapshot)}-item snapshot is the same as on the last run. "
            f"Applying it as a removal on Plex."
        )
        return False
    redis.set(SHRINK_KEY, listing, ex=SHRINK_TTL)
    current_app.logger.warning(
        f"Plex watchlist: the API returned {len(plex)} items against a "
        f"{len(snapshot)}-item snapshot. Anomaly, skipping this run. The "
        f"next run applies the same listing."
    )
    return True


def sync_plex_watchlist():
    """Reconcile the Plex and Fitzflix watchlists in both directions.

    This is a task. It is safe to run at any time. A failed run does not
    change the snapshot. Thus, the sync never reads a failure as a
    removal.
    """

    with app.app_context():
        if not current_app.config.get("PLEX_TOKEN"):
            return True
        if current_app.redis.exists(BACKOFF_KEY):
            current_app.logger.info(
                "Plex watchlist: plex.tv asked for a wait (Retry-After). "
                "Skipping this run."
            )
            return True

        snapshot_raw = current_app.redis.get(SNAPSHOT_KEY)
        snapshot = set(json.loads(snapshot_raw)) if snapshot_raw else set()
        unsyncable = {
            int(member) for member in current_app.redis.smembers(UNSYNCABLE_KEY)
        }

        try:
            user = _sync_user()
        except BaseTimeoutException:
            raise
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
        except ListingAnomaly as e:
            current_app.logger.warning(
                f"Plex watchlist: the Plex listing is incomplete ({e}). "
                f"Skipping this run."
            )
            return True
        except BaseTimeoutException:
            raise
        except requests.RequestException as e:
            # A Plex outage is not a code fault. One line is sufficient.

            current_app.logger.warning(
                f"Plex watchlist: could not read the Plex watchlist: {e}. "
                f"The next run tries again."
            )
            return True
        except Exception:
            current_app.logger.warning(traceback.format_exc())
            return True

        if _short_listing(plex, snapshot):
            return True

        plex_ids = set(plex)
        fitz_ids = set(fitzflix)

        # An unsyncable film that is now on Plex is in sync. Out of the
        # set, a later Fitzflix re-add of the film can push again.

        listed = unsyncable & plex_ids
        if listed:
            current_app.redis.srem(UNSYNCABLE_KEY, *listed)
            unsyncable -= listed

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
            # A watchlist row of a film with no tmdb id is not in the
            # Fitzflix side above. find_or_create_tmdb_movie can give that
            # film this tmdb id. A second row for the film breaks the
            # unique key, and each later run fails the same way.

            if (
                UserWatchlist.query.filter_by(
                    user_id=user.id, movie_id=movie.id
                ).first()
                is None
            ):
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
        stopped = None
        for tmdb_id in sorted(target - plex_ids):
            if stopped is not None:
                synced.discard(tmdb_id)
                continue
            if tmdb_id in unsyncable:
                synced.discard(tmdb_id)
                continue
            # The lookup and the add fail in different ways. A failed
            # lookup is always a retry, even a 4xx: a Plex outage or an
            # API change must not quarantine films. Only a refused add
            # can make a film unsyncable.

            try:
                rating_key = plex_rating_key(tmdb_id)
                if rating_key is None:
                    raise LookupError(f"no Plex match for tmdb {tmdb_id}")
            except BaseTimeoutException:
                raise
            except Exception as e:
                stopped = _stop_status(e)
                current_app.logger.warning(
                    f"Plex watchlist: couldn't look up tmdb {tmdb_id}: {e}"
                )
                synced.discard(tmdb_id)
                failed += 1
                continue
            try:
                _plex_put("addToWatchlist", rating_key)
                attempted.add(tmdb_id)
                pushed += 1
            except BaseTimeoutException:
                raise
            except Exception as e:
                stopped = _stop_status(e)
                if _refused_by_plex(e):
                    current_app.logger.warning(
                        f"Plex watchlist: Plex refused the add for tmdb "
                        f"{tmdb_id} ({e.response.status_code}). Marking it "
                        f"unsyncable. It stays on the Fitzflix watchlist. "
                        f"Clear {UNSYNCABLE_KEY} to retry."
                    )
                    current_app.redis.sadd(UNSYNCABLE_KEY, tmdb_id)
                else:
                    current_app.logger.warning(
                        f"Plex watchlist: couldn't add tmdb {tmdb_id}: {e}"
                    )
                synced.discard(tmdb_id)
                failed += 1

        for tmdb_id in sorted(plex_ids - target):
            if stopped is not None:
                # The film stays in the snapshot. The next run tries the
                # removal again.
                synced.add(tmdb_id)
                continue
            try:
                _plex_put("removeFromWatchlist", plex[tmdb_id]["rating_key"])
                pulled += 1
            except BaseTimeoutException:
                raise
            except Exception as e:
                stopped = _stop_status(e)
                current_app.logger.warning(
                    f"Plex watchlist: couldn't remove tmdb {tmdb_id}: {e}"
                )
                # The film stays in the snapshot. Thus, the next run tries
                # the removal again. Out of the snapshot, the film on Plex
                # would read as a new Plex add and go back on the Fitzflix
                # watchlist.

                synced.add(tmdb_id)
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
            except BaseTimeoutException:
                raise
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
            enqueue_task(
                "app.videos.refresh_tmdb_info",
                "Movies",
                movie_id,
                tmdb_id,
                description=f"Refreshing TMDB data for movie {movie_id}",
            )

        if stopped is not None:
            current_app.logger.warning(
                f"Plex watchlist: plex.tv answered {stopped}. Stopped the "
                f"pushes. The next run tries the rest again."
            )
        if added_here or removed_here or pushed or pulled or failed:
            current_app.logger.info(
                f"Plex watchlist sync for '{user.plex_username}': "
                f"+{added_here}/-{removed_here} here, "
                f"+{pushed}/-{pulled} at Plex, {failed} failed, "
                f"snapshot {len(synced)}"
            )
        return True
