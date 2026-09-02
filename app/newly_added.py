"""Per-provider "newly added" feeds and their landing shelves (#246).

The purpose is catalog discovery. This module shows films that the
database does not know and that recently arrived on a service that the
user pays for. A watchlisted film that becomes available is the job of
the availability-alert email (app.availability_alerts). It is not the
job of this shelf. The TMDb watch-provider payload has no availability
dates in either direction (verified 2026-08). Each entry has only a
provider id, name, logo, and priority. Thus, "newly added" can only
come from the feed of the provider, compared with a snapshot.

The infrastructure is generic over the provider id. There is a Redis
store per provider (`fitzflix:newly_added:{provider_id}`). Each item in
it has a `first_seen` date that the diff sets. There is a shelf per
subscribed provider with a stored feed. There is an "added <date>"
availability badge. But only one feed exists today: the newly-added
collection of the Criterion Channel. Fitzflix scrapes it through the
same VHX collection reader as the leaving page. The listing of full
provider catalogs from TMDb /discover is the job of #250. It writes
this same store shape. Its target is the recommendation universe, not
a shelf.

The diff follows the availability-alert snapshot tradition. The first
run only plants the store. Then first_seen stays null and nothing
shows, because a film that was already on the page is not news. A film
that was seen before keeps its first_seen. A film that is gone from the
page drops out of the store. The scraped `(title.lower(), year)` is the
diff key. Slugs are not stable. A matched item gets the TMDb title.
Thus, each item also records its scraped title and year for the diff
of the next run.
"""

import json
import traceback

from datetime import date, datetime, timedelta

from flask import current_app, g
from werkzeug.local import LocalProxy

from app import db, get_app
from app.leaving_criterion import (
    CRITERION_PROVIDER_ID,
    fetch_collection_films,
    match_tmdb_id,
    user_film_sets,
)
from app.models import File, Movie, UserMovieReview, UserWatchlist
from app.recommendations import score_movie, stored_profile
from app.streaming_rail import _payload_features, enriched_movie

# The app instance of this process. Fitzflix resolves it lazily. Thus, the
# nightly task can run on a worker without a second application.

app = LocalProxy(get_app)

# Every feed that a provider offers. The url is the page to scrape with
# fetch_collection_films. The label is the text of the shelf header
# ("Newly added to {label}"). Criterion is the only entry. The code
# downstream is generic over providers. Thus, a second collection to
# scrape is one line here.

FEEDS = {
    CRITERION_PROVIDER_ID: {
        "url": "https://www.criterionchannel.com/newly-added",
        "label": "the Criterion Channel",
    }
}

NEWLY_ADDED_KEY = "fitzflix:newly_added:{provider_id}"

# The number of days that an arrival stays "new" on the shelf and on the
# badge. The page of the provider removes films on its own schedule.
# Thus, this is a limit, not the usual case.

RECENT_DAYS = 30


def _recent(item, today=None):
    """Return True if the first_seen date of the item is inside the shelf
    window.

    A null first_seen (planted on the first run of the feed) is never
    recent."""

    first_seen = item.get("first_seen")
    if not first_seen:
        return False
    today = today or date.today()
    return today - date.fromisoformat(first_seen) <= timedelta(days=RECENT_DAYS)


def refresh_newly_added():
    """Refresh the newly-added feed of each provider (daily task).

    This task scrapes the feed. It compares the feed with the stored
    snapshot to set the first-seen dates. Then it stores the set with the
    embedded enriched payloads."""

    with app.app_context():
        for provider_id, feed in FEEDS.items():
            try:
                _refresh_feed(provider_id, feed)
            except Exception:
                current_app.logger.warning(traceback.format_exc())
        return True


def _refresh_feed(provider_id, feed):
    """Scrape, diff, and store the feed of one provider."""

    films = fetch_collection_films(feed["url"])
    if not films:
        # Keep the previous snapshot. A scrape outage must not empty the
        # store and make the whole page of tomorrow "new".
        current_app.logger.warning(f"Newly-added: no films found at {feed['url']}")
        return

    key = NEWLY_ADDED_KEY.format(provider_id=provider_id)
    raw = current_app.redis.get(key)
    previous = json.loads(raw) if raw else None
    previous_items = {
        ((item.get("scraped_title") or "").lower(), item.get("scraped_year")): item
        for item in (previous or {}).get("items", [])
    }

    today = date.today().isoformat()
    items = []
    fresh = 0
    arrived = []
    for film in films:
        prior = previous_items.get((film["title"].lower(), film["year"]))
        if prior is not None and prior.get("tmdb_id"):
            # The stored payload of a matched film goes with it unchanged.
            # Only a film that is new to the page costs a lookup. A film
            # that is still unmatched also costs a lookup, but the 60-day
            # match cache absorbs the retry.
            items.append(prior)
            continue
        if prior is not None:
            first_seen = prior.get("first_seen")
        else:
            first_seen = today if previous is not None else None
            if previous is not None:
                fresh += 1
        tmdb_id = match_tmdb_id(film["title"], film["year"], film["director"])
        payload = enriched_movie(tmdb_id) if tmdb_id is not None else None
        base = {**payload, "tmdb_id": tmdb_id} if payload else {**film, "tmdb_id": None}
        if previous is not None and tmdb_id is not None:
            # The film is new to the page, or it was on the page but
            # unmatched until this run. In both cases, the film counts
            # as streaming here only from now.
            arrived.append(tmdb_id)
        items.append(
            {
                **base,
                "first_seen": first_seen,
                "scraped_title": film["title"],
                "scraped_year": film["year"],
            }
        )

    current_app.redis.set(
        key,
        json.dumps(
            {
                "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "source": feed["url"],
                "items": items,
            }
        ),
    )
    current_app.logger.info(
        f"Newly-added: stored {len(items)} films for provider {provider_id}"
        + (
            f", {fresh} new since the last run"
            if previous is not None
            else " (planted)"
        )
    )
    # An owned day-one arrival joins the Criterion channel of the DVR
    # through the synthesized match. Thus, the dial catches up the same
    # day.
    if arrived and provider_id == CRITERION_PROVIDER_ID:
        from app.dvr import enqueue_lineup_rebuild

        enqueue_lineup_rebuild(
            f"{len(arrived)} newly-added Criterion films", tmdb_ids=arrived
        )


def newly_added_shelves(user):
    """Return [{provider_id, label, source, items}], one taste-ranked
    discovery shelf per subscribed provider with recent arrivals.

    Return [] if the user has no taste profile. The shelf rules are the
    same as for the leaving shelf. An owned film drops out, because it is
    not a discovery. A refused, logged, or watchlisted film also drops
    out. A watchlisted arrival belongs to the watchlist shelf of the
    landing page. The alert email is the primary channel for that case.
    The scrape frequently knows some days before TMDb does.
    """

    subscribed = {row.provider_id for row in user.streaming_providers}
    profile = None
    shelves = []
    for provider_id, feed in FEEDS.items():
        if provider_id not in subscribed:
            continue
        raw = current_app.redis.get(NEWLY_ADDED_KEY.format(provider_id=provider_id))
        if not raw:
            continue
        stored = json.loads(raw)
        recent = [
            item
            for item in stored.get("items", [])
            if item.get("tmdb_id") and _recent(item)
        ]
        if not recent:
            continue
        if profile is None:
            profile = stored_profile(current_app.redis, user.id)
            if not profile:
                return []

        tmdb_ids = [item["tmdb_id"] for item in recent]
        owned, logged, watchlisted, refused = user_film_sets(user, tmdb_ids)

        rows = []
        for item in recent:
            tmdb_id = item["tmdb_id"]
            if tmdb_id in owned or tmdb_id in refused:
                continue
            if tmdb_id in logged or tmdb_id in watchlisted:
                continue
            score, contributions = score_movie(_payload_features(item), profile)
            rows.append(
                {
                    "tmdb_id": tmdb_id,
                    "title": item.get("title"),
                    "year": item.get("year"),
                    "poster_path": item.get("poster_path"),
                    "runtime": item.get("runtime"),
                    "first_seen": item.get("first_seen"),
                    "because": [
                        label
                        for contribution, label in contributions[:3]
                        if contribution > 0
                    ],
                    "score": round(score, 4),
                }
            )
        if not rows:
            continue
        rows.sort(key=lambda row: row["score"], reverse=True)
        shelves.append(
            {
                "provider_id": provider_id,
                "label": feed["label"],
                "source": feed["url"],
                "items": rows,
            }
        )
    return shelves


def newly_added_inventory(user):
    """Return [{provider_id, label, source, films, unmatched}], the
    complete recent-arrival set of every provider for the /newly-added
    page.

    Unlike the home shelf, this excludes nothing. An owned film stays in
    the list with its library badge. A seen film stays with its Seen
    badge. A film that the TMDB matcher could not resolve comes last as a
    plain scraped row. Thus, the inventory is the whole arrival set.
    Watchlisted films come first. Then unowned films come by taste score.
    Owned films come after. This is the order of the leaving inventory.
    """

    profile = stored_profile(current_app.redis, user.id)
    sections = []
    for provider_id, feed in FEEDS.items():
        raw = current_app.redis.get(NEWLY_ADDED_KEY.format(provider_id=provider_id))
        if not raw:
            continue
        stored = json.loads(raw)
        recent = [item for item in stored.get("items", []) if _recent(item)]
        matched = [item for item in recent if item.get("tmdb_id")]
        unmatched = [item for item in recent if not item.get("tmdb_id")]
        if not matched and not unmatched:
            continue
        tmdb_ids = [item["tmdb_id"] for item in matched]

        owned = {}
        seen = set()
        watchlisted = set()
        if tmdb_ids:
            owned = dict(
                db.session.query(Movie.tmdb_id, Movie.id)
                .filter(Movie.tmdb_id.in_(tmdb_ids))
                .filter(Movie.files.any(File.feature_type_id.is_(None)))
            )
            seen = {
                tmdb_id
                for (tmdb_id,) in db.session.query(Movie.tmdb_id)
                .join(UserMovieReview, UserMovieReview.movie_id == Movie.id)
                .filter(Movie.tmdb_id.in_(tmdb_ids))
                .filter(UserMovieReview.user_id == int(user.id))
            }
            watchlisted = {
                tmdb_id
                for (tmdb_id,) in db.session.query(Movie.tmdb_id)
                .join(UserWatchlist, UserWatchlist.movie_id == Movie.id)
                .filter(Movie.tmdb_id.in_(tmdb_ids))
                .filter(UserWatchlist.user_id == int(user.id))
            }

        films = []
        for item in matched:
            tmdb_id = item["tmdb_id"]
            if profile:
                score, contributions = score_movie(_payload_features(item), profile)
            else:
                score, contributions = 0.0, []
            films.append(
                {
                    "tmdb_id": tmdb_id,
                    "title": item.get("title"),
                    "year": item.get("year"),
                    "poster_path": item.get("poster_path"),
                    "runtime": item.get("runtime"),
                    "overview": item.get("overview"),
                    "first_seen": item.get("first_seen"),
                    "movie_id": owned.get(tmdb_id),
                    "owned": tmdb_id in owned,
                    "seen": tmdb_id in seen,
                    "watchlisted": tmdb_id in watchlisted,
                    "because": [
                        label
                        for contribution, label in contributions[:3]
                        if contribution > 0
                    ],
                    "score": round(score, 4),
                }
            )
        films.sort(
            key=lambda film: (
                not film["watchlisted"],
                film["owned"],
                -film["score"],
                (film["title"] or "").lower(),
            )
        )
        sections.append(
            {
                "provider_id": provider_id,
                "label": feed["label"],
                "source": feed["url"],
                "films": films,
                "unmatched": sorted(
                    (
                        {
                            "title": film.get("title"),
                            "year": film.get("year"),
                            "director": film.get("director"),
                        }
                        for film in unmatched
                    ),
                    key=lambda film: (film["title"] or "").lower(),
                ),
            }
        )
    return sections


def newly_added_since(tmdb_id, provider_id):
    """Return the arrival date, as "August 5", if the film joined the
    newly-added feed of the provider inside the recent window.

    Return None in all other cases. The availability badges call this
    function per match. Thus, Fitzflix parses the stores one time per app
    context and keeps them on flask.g. That is one Redis read per provider
    per page, in the leaving_departure tradition."""

    if tmdb_id is None:
        return None
    index = _fold_index()
    return index.get((provider_id, tmdb_id))


def newly_added_fold(tmdb_id, provider_ids):
    """Return the label of the green poster fold ("Added to the Criterion
    Channel August 1") if the film recently arrived on a feed of one of
    the given subscribed providers.

    Return None in all other cases."""

    if tmdb_id is None:
        return None
    index = _fold_index()
    for provider_id in provider_ids:
        feed = FEEDS.get(provider_id)
        if feed is None:
            continue
        added = index.get((provider_id, tmdb_id))
        if added:
            return f"Added to {feed['label']} {added}"
    return None


def poster_fold(user, tmdb_id, movie_id=None):
    """Return ("leaving" | "new", label) for the one corner fold that the
    standalone poster of a film shows for this user, or None.

    A gallery tile paints its fold client-side from /movie_states. The
    large posters of the movie page and the log page render server-side.
    They ask here instead, with the same rules. The red leaving fold
    (Criterion subscribers only) outranks the green newly-added fold. The
    recently-available record of the alert diff keeps priority for the
    green label."""

    # The import is here because streaming reaches this module lazily from
    # streaming_matches. A top-level import would be circular.
    from app.availability_alerts import NEW_IN_LIBRARY_LABEL, recent_availability
    from app.leaving_criterion import leaving_departure
    from app.streaming import user_provider_ids

    label = None
    if movie_id is not None:
        entry = recent_availability(user).get(int(movie_id))
        if entry:
            label = entry.get("label")

    # Ownership gates the folds (requested by Glenn, 2026-08-27). The copy
    # of an owned film stays. Thus, the film never warns of a departure.
    # Its only green fold is the recent arrival of the local file.

    if (
        movie_id is not None
        and db.session.query(
            Movie.query.filter(Movie.id == int(movie_id))
            .filter(Movie.files.any(File.feature_type_id.is_(None)))
            .exists()
        ).scalar()
    ):
        return ("new", label) if label == NEW_IN_LIBRARY_LABEL else None

    provider_ids = set(user_provider_ids(user))
    if CRITERION_PROVIDER_ID in provider_ids:
        departs = leaving_departure(tmdb_id)
        if departs:
            return ("leaving", f"Leaving the Criterion Channel {departs}")
    label = label or newly_added_fold(tmdb_id, sorted(set(FEEDS) & provider_ids))
    return ("new", label) if label else None


def _fold_index():
    """Return {(provider_id, tmdb_id): "August 5"} for every recent arrival
    across the stored feeds.

    Fitzflix parses the index one time per app context and keeps it on
    flask.g. That is one Redis read per provider per page, in the
    leaving_departure tradition."""

    index = getattr(g, "_newly_added_index", None)
    if index is None:
        index = {}
        for pid in FEEDS:
            raw = current_app.redis.get(NEWLY_ADDED_KEY.format(provider_id=pid))
            if not raw:
                continue
            stored = json.loads(raw)
            for item in stored.get("items", []):
                if item.get("tmdb_id") and _recent(item):
                    index[(pid, item["tmdb_id"])] = date.fromisoformat(
                        item["first_seen"]
                    ).strftime("%B %-d")
        g._newly_added_index = index
    return index
