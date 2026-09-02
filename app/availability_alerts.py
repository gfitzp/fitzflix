"""Send the watchlist availability alerts (#156, #230).

A watchlist entry means "tell me when I can watch this film". Thus, a
nightly task compares the availability of each watchlisted film with a
stored snapshot. Then it tells the watchers what changed. There are 3
triggers:

- The first copy of the film arrives in the library. An upgrade of a
  film that the user already owns sends nothing. The snapshot compares
  set membership (owned or not owned). Thus, a replaced file never
  triggers an alert.
- The film appears on a flat-rate service that the user subscribes to.
  A free-with-ads service counts too. This matches streaming_matches
  in all other places. The comparison is per film against the nightly
  availability cache. Thus, this extends the Criterion-only "newly
  added" signal of issue #156 to each service on the profile of the
  user.
- The film becomes rentable on one of the services of the user. This
  is a second per-user opt-in. A rental is an extra fee. Thus, it only
  counts as "available" if the user says so.

There is also the leaving-Criterion urgency case from #156. A
watchlisted film that the user does not own can be in the stored
leaving set. Then Fitzflix warns the Criterion subscribers when it
first stores the set, and again inside the final week.

Delivery is 1 batched digest email per user per run. Fitzflix never
sends a mail per film. The mail is strictly opt-in
(User.notify_availability). It is the first per-user mail other than
the password reset. The Profile page is the unsubscribe path. Each
event also stamps a per-user "recently available" record. The
watchlist page shows that record as a badge for 1 month, opted in or
not. A Redis marker per user, film, and event kind removes duplicates
across runs. Thus, an availability flap cannot send the mail again.

The snapshot rules follow the Plex history poller. A film that
Fitzflix sees for the first time (new to the snapshot, or newly
watchlisted) only plants its entry. Fitzflix sends no notification for
a condition that was already true when the user started to watch for
it. If the availability of a film is not cached tonight, its previous
entry stays as it is. Thus, a cache gap cannot make a false "newly
available" event when the payload returns.
"""

import json

from collections import defaultdict
from datetime import date, timedelta

from flask import current_app, render_template, url_for
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

# This is the app instance of this process. Fitzflix resolves it lazily.
# Thus, the nightly task can run on a worker and not build a second
# application.

app = LocalProxy(get_app)

SNAPSHOT_KEY = "fitzflix:availability:watchlist-providers"
OWNED_KEY = "fitzflix:availability:owned-movies"
SENT_KEY = "fitzflix:availability:notified:{user_id}:{movie_id}:{kind}"
RECENT_KEY = "fitzflix:availability:recent:{user_id}"

# This is the dedup horizon. It is long enough that a service that
# drops and adds a film again cannot send the mail again. The same
# applies to a library file replaced in place. It is short enough that
# a real arrival 1 year later counts as news again.

SENT_SECONDS = 180 * 86400
LEAVING_SENT_SECONDS = 60 * 86400

# RECENT_DAYS is how long the watchlist badge calls an event "recent".
# RECENT_KEY_SECONDS is the expiry of the badge store. It is a margin
# past the badge window. Thus, the prune step has something to prune,
# but the key of an abandoned account still expires.

RECENT_DAYS = 31
RECENT_KEY_SECONDS = 45 * 86400

LEAVING_SOON_DAYS = 7

# This is the badge label for a local arrival. It is shared because
# ownership gates the poster folds (requested by Glenn, 2026-08-27).
# This is the only green fold of an owned film. A service arrival or a
# feed arrival never folds a film that is already on the shelf.

NEW_IN_LIBRARY_LABEL = "New in library"


def _text(value):
    """Return a str from a Redis field that can be bytes."""

    return value.decode() if isinstance(value, bytes) else value


def snapshot_provider_diff(tmdb_ids):
    """Return (newly_streaming, newly_rentable, provider_names).

    This also updates the stored snapshot in place. The first 2 values
    map tmdb_id to the set of provider ids that appeared after the last
    run. They are split by kind. "streaming" is flatrate plus
    free-with-ads (the streaming_matches definition). "rentable" is the
    rent list. provider_names maps a provider id to a display name. The
    names come from the registry and from the payloads. A film that is
    new to the snapshot only plants its entry. A film with no cached
    availability keeps its previous entry and makes no diff. This
    prunes the snapshot entries of the films that nobody watchlists
    now.
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
    """Return (owned, newly_owned).

    owned is each movie id with a main-feature file. newly_owned is the
    movie ids that got their first file after the stored snapshot. This
    compares membership only. Thus, an upgraded copy of an owned film
    never counts. The first run plants the snapshot and reports
    nothing."""

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
    """Return (tmdb ids, departure date) of the stored leaving set.

    This applies to the leaving-Criterion set if its departure date has
    not passed. In all other cases, return (set(), None)."""

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
    """Return True 1 time per user, film, and kind in the dedup horizon.

    This is the Redis marker that prevents a second mail from a
    provider that flaps."""

    return bool(
        current_app.redis.set(
            SENT_KEY.format(user_id=int(user_id), movie_id=int(movie_id), kind=kind),
            "1",
            nx=True,
            ex=ttl,
        )
    )


def _record_recent(user_id, movie_id, label):
    """Stamp one film as recently available for the watchlist badge.

    The stamp has the date of today. Thus, the badge expires after
    RECENT_DAYS."""

    key = RECENT_KEY.format(user_id=int(user_id))
    current_app.redis.hset(
        key,
        str(int(movie_id)),
        json.dumps({"date": date.today().isoformat(), "label": label}),
    )
    current_app.redis.expire(key, RECENT_KEY_SECONDS)


def recent_availability(user):
    """Return {movie_id: {"date", "label"}} for the events of the user.

    This includes only the events in the badge window. It prunes the
    expired entries as it reads."""

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
    """Return the display names of a set of provider ids as one string.

    The names are in alphabetical order and separated by commas. The
    raw id replaces a name that is missing."""

    return ", ".join(
        sorted(str(names.get(provider_id, provider_id)) for provider_id in provider_ids)
    )


def _poster_url(movie):
    """Return an absolute artwork URL for the digest email.

    The source order is the same as in the tile macro. The custom poster
    comes first. This site serves it. Thus, it needs the external static
    URL. If there is no custom poster, use the rendition that TMDB
    hosts. Return None if there is no artwork. Then the template drops
    the image cell and does not send a placeholder."""

    if movie.custom_poster:
        return url_for(
            "static",
            filename=f"custom/movie/{movie.id}/w342/{movie.custom_poster}",
            _external=True,
        )
    if movie.tmdb_poster_path:
        return current_app.config["TMDB_IMAGE_URL"] + "/w154" + movie.tmdb_poster_path
    return None


def _send_digest(user, events):
    """Send one batched digest mail for the events of one user.

    Fitzflix never sends a mail per film. The subject starts with the
    availability count. A digest with only leaving warnings says that
    instead."""

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
    """Run the nightly alert task, after the availability refresh.

    This compares the availability and the library presence of each
    watchlisted film with the stored snapshots. It stamps the per-user
    badge records. It sends each opted-in user a maximum of 1 digest
    email."""

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
                item = {
                    "title": title,
                    "year": movie.year,
                    "movie_id": movie.id,
                    "poster": _poster_url(movie),
                }

                # The order is the watchlist bucket order: owned before
                # streaming before rent. A film that just arrived
                # locally never also reports its streaming debut. An
                # owned film reports only its own arrival.

                if movie.id in newly_owned:
                    if _first_event(user.id, movie.id, "local"):
                        events["local"].append({**item, "note": "Added to the library"})
                        _record_recent(user.id, movie.id, NEW_IN_LIBRARY_LABEL)
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

            # This is the leaving-Criterion warning (#156). It occurs 1
            # time when the set first arrives, and 1 more time inside
            # the final week. There is no badge. A departure is not
            # availability. The leaving badge already shows in each
            # place that shows the providers of the film.

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
                                "poster": _poster_url(movie),
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
