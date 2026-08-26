"""Watchlist availability alerts (#156/#230).

Watchlisting a film is a "tell me when I can watch this" intent, so a
nightly task diffs each watchlisted film's availability against a
stored snapshot and tells the watchers what changed. Three triggers:

- The film's first copy arrives in the library. Upgrades of an
  already-owned film stay silent — the snapshot diff is on set
  membership (owned at all), so a replaced file never fires.
- The film turns up on a flat-rate service the user subscribes to
  (free-with-ads counts too, matching streaming_matches everywhere
  else). The diff is per film against the nightly availability cache,
  so this generalizes issue #156's Criterion-specific "newly added"
  signal to every service on the user's profile.
- The film becomes rentable on one of the user's services — an
  additional per-user opt-in, since a rental is an extra fee and only
  reads as "available" if the user says so.

Plus the leaving-Criterion urgency case from #156: a watchlisted,
unowned film in the stored leaving set warns Criterion subscribers
when the set is first stored and again inside the final week.

Delivery is one batched digest email per user per run — never a mail
per film — and strictly opt-in (User.notify_availability, the first
per-user mail besides password resets), with the Profile page as the
unsubscribe path. Every event also stamps a per-user "recently
available" record that the watchlist page renders as a badge for a
month, opted in or not. A Redis marker per user/film/event kind
dedups across runs, so an availability flap can't re-mail.

Snapshot rules, in the Plex-history-poller tradition: a film seen for
the first time (new to the snapshot, or newly watchlisted) only
plants its entry — no notification for what was already true when the
user started watching for it. A film whose availability is uncached
tonight keeps its previous entry untouched, so a cache gap can't
manufacture a false "newly available" when the payload returns.
"""

import json

from collections import defaultdict
from datetime import date, timedelta

from flask import current_app, render_template
from sqlalchemy.orm import contains_eager
from werkzeug.local import LocalProxy

from app import db, get_app
from app.email import task_send_email
from app.models import File, Movie, User, UserWatchlist
from app.streaming import (
    batch_title_availability,
    provider_registry,
    user_provider_ids,
)

# This process's app instance, resolved lazily so the nightly task can
# run on a worker without building a second application

app = LocalProxy(get_app)

SNAPSHOT_KEY = "fitzflix:availability:watchlist-providers"
OWNED_KEY = "fitzflix:availability:owned-movies"
SENT_KEY = "fitzflix:availability:notified:{user_id}:{movie_id}:{kind}"
RECENT_KEY = "fitzflix:availability:recent:{user_id}"

# The dedup horizon: long enough that a service dropping and re-adding
# a film (or a library file replaced in place) can't re-mail, short
# enough that a genuine re-arrival a year later reads as news again

SENT_SECONDS = 180 * 86400
LEAVING_SENT_SECONDS = 60 * 86400

# How long the watchlist badge calls an event "recent", and the badge
# store's own expiry (a margin past the badge window so pruning has
# something to prune, but an abandoned account's key still dies)

RECENT_DAYS = 31
RECENT_KEY_SECONDS = 45 * 86400

LEAVING_SOON_DAYS = 7


def _text(value):
    """A str from a Redis-returned field that may be bytes."""

    return value.decode() if isinstance(value, bytes) else value


def snapshot_provider_diff(tmdb_ids):
    """(newly_streaming, newly_rentable, provider_names) for the given
    films, updating the stored snapshot in place.

    The first two map tmdb_id -> the set of provider ids that appeared
    since the last run, split by kind: "streaming" is flatrate plus
    free-with-ads (the streaming_matches definition), "rentable" is
    the rent list. provider_names maps provider id -> display name,
    from the registry plus the payloads themselves. Films new to the
    snapshot only plant their entry; films with no cached availability
    keep their previous entry and produce no diff; snapshot entries
    for films nobody watchlists anymore are pruned.
    """

    redis = current_app.redis
    payloads, _ = batch_title_availability(tmdb_ids, fetch_limit=0)
    names = {
        p["provider_id"]: p["provider_name"]
        for p in provider_registry()
        if p.get("provider_name")
    }

    current = {}
    for tmdb_id, payload in payloads.items():
        if payload is None:
            continue
        streaming, rent = set(), set()
        for kind, bucket in (
            ("flatrate", streaming),
            ("ads", streaming),
            ("rent", rent),
        ):
            for provider in payload.get(kind) or []:
                if provider.get("provider_id") is None:
                    continue
                bucket.add(provider["provider_id"])
                if provider.get("provider_name"):
                    names[provider["provider_id"]] = provider["provider_name"]
        current[tmdb_id] = {"streaming": sorted(streaming), "rent": sorted(rent)}

    previous = {
        int(_text(field)): json.loads(value)
        for field, value in (redis.hgetall(SNAPSHOT_KEY) or {}).items()
    }

    newly_streaming, newly_rentable = {}, {}
    for tmdb_id, state in current.items():
        before = previous.get(tmdb_id)
        if before is None:
            continue
        newly_streaming[tmdb_id] = set(state["streaming"]) - set(
            before.get("streaming") or []
        )
        newly_rentable[tmdb_id] = set(state["rent"]) - set(before.get("rent") or [])

    wanted = {int(tmdb_id) for tmdb_id in tmdb_ids if tmdb_id is not None}
    stale = [str(tmdb_id) for tmdb_id in previous if tmdb_id not in wanted]
    pipe = redis.pipeline()
    for tmdb_id, state in current.items():
        pipe.hset(SNAPSHOT_KEY, str(tmdb_id), json.dumps(state))
    if stale:
        pipe.hdel(SNAPSHOT_KEY, *stale)
    pipe.execute()
    return newly_streaming, newly_rentable, names


def snapshot_owned_diff():
    """(owned, newly_owned): every movie id with a main-feature file,
    and the ones that gained their first since the stored snapshot —
    membership diff only, so an upgraded copy of an owned film never
    counts. The first run plants the snapshot and reports nothing."""

    redis = current_app.redis
    owned = {
        movie_id
        for (movie_id,) in db.session.query(Movie.id).filter(
            Movie.files.any(File.feature_type_id.is_(None))
        )
    }
    stored = redis.get(OWNED_KEY)
    redis.set(OWNED_KEY, json.dumps(sorted(owned)))
    if stored is None:
        return owned, set()
    return owned, owned - set(json.loads(stored))


def _leaving_set():
    """(tmdb ids, departure date) of the stored leaving-Criterion set
    while its departure hasn't passed; (set(), None) otherwise."""

    from app.leaving_criterion import LEAVING_KEY

    payload = current_app.redis.get(LEAVING_KEY)
    if not payload:
        return set(), None
    stored = json.loads(payload)
    departs = date.fromisoformat(stored["departs"])
    if departs < date.today():
        return set(), None
    return (
        {item["tmdb_id"] for item in stored.get("items", []) if item.get("tmdb_id")},
        departs,
    )


def _first_event(user_id, movie_id, kind, ttl=SENT_SECONDS):
    """True exactly once per user/film/kind inside the dedup horizon —
    the Redis marker that keeps a flapping provider from re-mailing."""

    return bool(
        current_app.redis.set(
            SENT_KEY.format(user_id=int(user_id), movie_id=int(movie_id), kind=kind),
            "1",
            nx=True,
            ex=ttl,
        )
    )


def _record_recent(user_id, movie_id, label):
    """Stamp one film recently-available for this user's watchlist
    badge, with today's date so the badge ages out after RECENT_DAYS."""

    key = RECENT_KEY.format(user_id=int(user_id))
    current_app.redis.hset(
        key,
        str(int(movie_id)),
        json.dumps({"date": date.today().isoformat(), "label": label}),
    )
    current_app.redis.expire(key, RECENT_KEY_SECONDS)


def recent_availability(user):
    """{movie_id: {"date", "label"}} for this user's events inside the
    badge window, pruning aged-out entries as it reads."""

    key = RECENT_KEY.format(user_id=int(user.id))
    cutoff = date.today() - timedelta(days=RECENT_DAYS)
    recent, stale = {}, []
    for field, value in (current_app.redis.hgetall(key) or {}).items():
        entry = json.loads(value)
        if date.fromisoformat(entry["date"]) < cutoff:
            stale.append(field)
        else:
            recent[int(_text(field))] = entry
    if stale:
        current_app.redis.hdel(key, *stale)
    return recent


def _provider_labels(provider_ids, names):
    """The display names for a set of provider ids, alphabetical, as
    one comma-joined string; the raw id stands in for a nameless one."""

    return ", ".join(
        sorted(str(names.get(provider_id, provider_id)) for provider_id in provider_ids)
    )


def _send_digest(user, events):
    """One batched digest mail for one user's events — never a mail
    per film. The subject leads with the availability count; a digest
    of nothing but leaving warnings says that instead."""

    available = sum(len(events[kind]) for kind in ("local", "streaming", "rent"))
    if available:
        subject = (
            f"Fitzflix - {available} watchlist film"
            f"{'s' if available != 1 else ''} now available"
        )
    else:
        subject = "Fitzflix - Watchlist films leaving the Criterion Channel"
    task_send_email(
        subject,
        sender=("Fitzflix", current_app.config["SERVER_EMAIL"]),
        recipients=[user.email],
        text_body=render_template(
            "email/availability_digest.txt", user=user, events=events
        ),
        html_body=render_template(
            "email/availability_digest.html", user=user, events=events
        ),
    )


def notify_watchlist_availability():
    """Nightly task, after the availability refresh: diff every
    watchlisted film's availability and library presence against the
    stored snapshots, stamp the per-user badge records, and send each
    opted-in user at most one digest email."""

    with app.app_context():
        entries = (
            UserWatchlist.query.join(Movie, Movie.id == UserWatchlist.movie_id)
            .options(contains_eager(UserWatchlist.movie))
            .all()
        )
        tmdb_ids = sorted(
            {entry.movie.tmdb_id for entry in entries if entry.movie.tmdb_id}
        )
        newly_streaming, newly_rentable, provider_names = snapshot_provider_diff(
            tmdb_ids
        )
        owned_ids, newly_owned = snapshot_owned_diff()
        leaving_ids, leaving_departs = _leaving_set()

        by_user = defaultdict(list)
        for entry in entries:
            by_user[entry.user_id].append(entry.movie)

        from app.leaving_criterion import CRITERION_PROVIDER_ID

        digests = 0
        events_total = 0
        users = User.query.filter(User.id.in_(list(by_user) or [0])).all()
        for user in users:
            provider_ids = user_provider_ids(user)
            events = {"local": [], "streaming": [], "rent": [], "leaving": []}
            for movie in by_user[user.id]:
                title = movie.tmdb_title or movie.title
                item = {"title": title, "year": movie.year, "movie_id": movie.id}

                # Owned beats streaming beats renting, the watchlist
                # bucket order: a film that just arrived locally never
                # also reports its streaming debut, and an owned film
                # reports nothing but its own arrival

                if movie.id in newly_owned:
                    if _first_event(user.id, movie.id, "local"):
                        events["local"].append({**item, "note": "Added to the library"})
                        _record_recent(user.id, movie.id, "New in library")
                    continue
                if movie.id in owned_ids or not movie.tmdb_id:
                    continue

                new_streaming = newly_streaming.get(movie.tmdb_id, set()) & provider_ids
                new_rent = newly_rentable.get(movie.tmdb_id, set()) & provider_ids
                if new_streaming:
                    if _first_event(user.id, movie.id, "streaming"):
                        labels = _provider_labels(new_streaming, provider_names)
                        events["streaming"].append({**item, "note": f"Now on {labels}"})
                        _record_recent(user.id, movie.id, f"New on {labels}")
                elif new_rent and user.notify_rentals:
                    if _first_event(user.id, movie.id, "rent"):
                        labels = _provider_labels(new_rent, provider_names)
                        events["rent"].append(
                            {**item, "note": f"Available to rent on {labels}"}
                        )
                        _record_recent(user.id, movie.id, "New to rent")

            # The leaving-Criterion warning (#156): once when the set
            # first lands, once more inside the final week. No badge —
            # departure isn't availability, and the leaving badge
            # already renders wherever the film's providers do

            if leaving_departs and CRITERION_PROVIDER_ID in provider_ids:
                departs_label = leaving_departs.strftime("%B %-d")
                kinds = [f"leaving-{leaving_departs.isoformat()}"]
                if (leaving_departs - date.today()).days <= LEAVING_SOON_DAYS:
                    kinds.append(f"leaving-soon-{leaving_departs.isoformat()}")
                for movie in by_user[user.id]:
                    if movie.tmdb_id not in leaving_ids or movie.id in owned_ids:
                        continue
                    fired = [
                        kind
                        for kind in kinds
                        if _first_event(
                            user.id, movie.id, kind, ttl=LEAVING_SENT_SECONDS
                        )
                    ]
                    if fired:
                        events["leaving"].append(
                            {
                                "title": movie.tmdb_title or movie.title,
                                "year": movie.year,
                                "movie_id": movie.id,
                                "note": (
                                    "Leaving the Criterion Channel " f"{departs_label}"
                                ),
                            }
                        )

            count = sum(len(items) for items in events.values())
            events_total += count
            if count and user.notify_availability and user.email:
                _send_digest(user, events)
                digests += 1

        current_app.logger.info(
            f"Watchlist availability: {events_total} event(s) across "
            f"{len(users)} watchlist user(s), {digests} digest(s) mailed"
        )
        return True
