"""Per-provider "newly added" feeds and their landing shelves (#246).

The point is catalog discovery: surfacing films the database has never
heard of that just landed on a service the user already pays for. A
watchlisted film becoming available is the availability-alert email's
job (app.availability_alerts), not this shelf's. TMDb's watch-provider
payload carries no availability dates in either direction (verified
Aug 2026 — entries are just provider id/name/logo/priority), so "newly
added" can only come from a provider's own feed, snapshot-diffed.

The infrastructure is generic over provider id — a Redis store per
provider (`fitzflix:newly_added:{provider_id}`) whose items carry a
`first_seen` date stamped by the diff, a shelf per subscribed provider
with a stored feed, and an "added <date>" availability badge — but
only one feed exists today: the Criterion Channel's newly-added
collection, scraped through the same VHX collection reader as the
leaving page. Enumerating full provider catalogs from TMDb /discover
is #250's job (it writes this same store shape), aimed at the
recommendation universe rather than a shelf.

Diff semantics, in the availability-alert snapshot tradition: the
first run only plants (first_seen stays null, nothing surfaces — what
was already on the page isn't news); a film seen before keeps its
first_seen; a film gone from the page drops out of the store. Scraped
`(title.lower(), year)` is the diff key — slugs aren't stable, and
matched items get TMDb's title, so each item also records its scraped
title and year for the next run's diff.
"""

import json
import traceback

from datetime import date, datetime, timedelta

from flask import current_app, g
from werkzeug.local import LocalProxy

from app import get_app
from app.leaving_criterion import (
    CRITERION_PROVIDER_ID,
    fetch_collection_films,
    match_tmdb_id,
    user_film_sets,
)
from app.recommendations import score_movie, stored_profile
from app.streaming_rail import _payload_features, enriched_movie

# This process's app instance, resolved lazily so the nightly task can
# run on a worker without building a second application

app = LocalProxy(get_app)

# Every feed a provider offers: url to scrape with
# fetch_collection_films, label as the shelf header reads it
# ("Newly added to {label}"). Criterion is the only entry — the
# machinery downstream is provider-generic, so a second scrapeable
# collection is one line here

FEEDS = {
    CRITERION_PROVIDER_ID: {
        "url": "https://www.criterionchannel.com/newly-added",
        "label": "the Criterion Channel",
    }
}

NEWLY_ADDED_KEY = "fitzflix:newly_added:{provider_id}"

# How long an arrival stays "new" on the shelf and the badge. The
# provider's page rotates films off on its own schedule, so this is a
# cap, not the usual case

RECENT_DAYS = 30


def _recent(item, today=None):
    """True when the item's first_seen date is inside the shelf
    window. A null first_seen (planted on the feed's first run) is
    never recent."""

    first_seen = item.get("first_seen")
    if not first_seen:
        return False
    today = today or date.today()
    return today - date.fromisoformat(first_seen) <= timedelta(days=RECENT_DAYS)


def refresh_newly_added():
    """Daily task: scrape each provider's newly-added feed, diff it
    against the stored snapshot to stamp first-seen dates, and store
    the set with embedded enriched payloads."""

    with app.app_context():
        for provider_id, feed in FEEDS.items():
            try:
                _refresh_feed(provider_id, feed)
            except Exception:
                current_app.logger.warning(traceback.format_exc())
        return True


def _refresh_feed(provider_id, feed):
    """Scrape, diff, and store one provider's feed."""

    films = fetch_collection_films(feed["url"])
    if not films:
        # Keep the previous snapshot untouched: a scrape outage must
        # not empty the store and turn tomorrow's whole page "new"
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
    for film in films:
        prior = previous_items.get((film["title"].lower(), film["year"]))
        if prior is not None and prior.get("tmdb_id"):
            # A matched film's stored payload rides along unchanged —
            # only films new to the page (or still unmatched, where
            # the 60-day match cache absorbs the retry) cost lookups
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


def newly_added_shelves(user):
    """[{provider_id, label, source, items}] — one taste-ranked
    discovery shelf per subscribed provider whose stored feed has
    recent arrivals; [] without a taste profile.

    Shelf semantics mirror the leaving shelf: owned films drop out
    (they're not discoveries), refused films drop out, logged films
    drop out unless watchlisted, and a watchlisted arrival badges and
    sorts first — though the alert email is the primary channel for
    that case, the scrape often knows days before TMDb does.
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
            if tmdb_id in logged and tmdb_id not in watchlisted:
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
                    "watchlisted": tmdb_id in watchlisted,
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
        rows.sort(key=lambda row: (row["watchlisted"], row["score"]), reverse=True)
        shelves.append(
            {
                "provider_id": provider_id,
                "label": feed["label"],
                "source": feed["url"],
                "items": rows,
            }
        )
    return shelves


def newly_added_since(tmdb_id, provider_id):
    """The arrival date, as "August 5", when the film joined the
    provider's newly-added feed inside the recent window; None
    otherwise. The availability badges ask this per match, so the
    stores are parsed once per app context and kept on flask.g — one
    Redis read per provider per page, in the leaving_departure
    tradition."""

    if tmdb_id is None:
        return None
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
    return index.get((provider_id, tmdb_id))
