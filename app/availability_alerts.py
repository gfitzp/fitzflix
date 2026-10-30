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
import mimetypes
import os

from collections import defaultdict
from datetime import date, timedelta

from flask import current_app, render_template
from sqlalchemy.orm import contains_eager
from werkzeug.local import LocalProxy

from app import db, get_app
from app.custom_artwork import custom_artwork_dir
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

# A digest that the mail server refused waits here for the next run
# (#377). The snapshots moved on already, so the next diff does not find
# the events again. The events of the next run join it. A digest older
# than UNSENT_SECONDS is dropped with an error. The key lives 2 days
# longer. Thus, the next run sees the age and logs the loss.

UNSENT_KEY = "fitzflix:availability:unsent:{user_id}"
UNSENT_SECONDS = 7 * 86400
UNSENT_KEY_SECONDS = UNSENT_SECONDS + 2 * 86400

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

# The number of empty availability payloads in a row that make the empty
# state of a film real (#326). A smaller number of empty runs is a gap in
# the TMDB data. Thus, the snapshot keeps the providers of the film.

EMPTY_RUNS_TO_ACCEPT = 3


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
    availability keeps its previous entry and makes no diff. An empty
    payload after a non-empty entry is a gap until EMPTY_RUNS_TO_ACCEPT
    runs in a row are empty. This
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

    previous = {
        int(_text(field)): json.loads(value)
        for field, value in (redis.hgetall(SNAPSHOT_KEY) or {}).items()
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

        # TMDB can serve an empty payload for one night (#326, the same
        # glitch as #251). An empty payload after a non-empty entry is a
        # gap. The previous entry stays, and the film makes no diff. After
        # EMPTY_RUNS_TO_ACCEPT empty runs in a row, the empty state is
        # real, and the film left every service.

        before = previous.get(int(tmdb_id))
        if (
            not streaming
            and not rent
            and before
            and (before.get("streaming") or before.get("rent"))
        ):
            empty_runs = int(before.get("empty_runs") or 0) + 1
            if empty_runs < EMPTY_RUNS_TO_ACCEPT:
                current[tmdb_id] = {
                    "streaming": before.get("streaming") or [],
                    "rent": before.get("rent") or [],
                    "empty_runs": empty_runs,
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
    provider that flaps. The run plants it when it finds the event. A
    refused digest keeps only its events for the next run."""

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


# The Content-ID of an inline custom poster in the digest (#468).

POSTER_CID = "poster-{movie_id}@fitzflix"


def _custom_poster_file(movie):
    """Return the path of the w342 custom poster of a movie, or None."""

    if not movie or not movie.custom_poster:
        return None
    path = os.path.join(
        custom_artwork_dir("movie", movie.id), "w342", movie.custom_poster
    )
    return path if os.path.isfile(path) else None


def _poster_url(movie):
    """Return the artwork URL for the digest email, or None.

    A mail client fetches an image with no session. The custom posters
    of this site need a signed-in user (#468). Thus, the TMDB rendition
    comes first. A film with only a custom poster gets a cid: URL. The
    digest attaches that poster inline. Return None if there is no
    artwork. Then the template drops the image cell and does not send a
    placeholder."""

    if movie.tmdb_poster_path:
        return current_app.config["TMDB_IMAGE_URL"] + "/w154" + movie.tmdb_poster_path
    if _custom_poster_file(movie):
        return "cid:" + POSTER_CID.format(movie_id=movie.id)
    return None


def _inline_posters(events):
    """Set the poster of each digest item again, and return the inline attachments.

    An event that waited unsent in Redis can hold an old poster URL.
    Thus, each item gets the URL of its record now. Each cid: poster
    becomes 1 inline attachment, in the (filename, content type, data,
    disposition, headers) form of build_message."""

    attachments = []
    attached = set()
    for items in events.values():
        for item in items:
            movie = db.session.get(Movie, item["movie_id"])
            item["poster"] = _poster_url(movie) if movie else None
            if not (item["poster"] or "").startswith("cid:"):
                continue
            if movie.id in attached:
                continue
            path = _custom_poster_file(movie)
            with open(path, "rb") as handle:
                data = handle.read()
            content_type = mimetypes.guess_type(path)[0] or "image/jpeg"
            attachments.append(
                (
                    os.path.basename(path),
                    content_type,
                    data,
                    "inline",
                    {"Content-ID": f"<{POSTER_CID.format(movie_id=movie.id)}>"},
                )
            )
            attached.add(movie.id)
    return attachments


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
    attachments = _inline_posters(events)
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
        attachments=attachments or None,
    )


def _with_unsent(user_id, events):
    """Return (events, since): the events of a user and of a digest that was not sent.

    since is the date of the oldest digest that waits, or today. A
    stored film counts only while it is on the watchlist of the user. A
    streaming, rent, or leaving event of a film that is in the library
    now goes too. A film that is in a list 2 times stays 1 time. A
    digest older than UNSENT_SECONDS is lost. That is an error in the
    log."""

    today = date.today().isoformat()
    stored = current_app.redis.getdel(UNSENT_KEY.format(user_id=int(user_id)))
    if not stored:
        return events, today
    unsent = json.loads(stored)

    # A digest stored before the #631 deploy carries markers that the
    # run did not plant. Delete these 2 lines after 2026-11-09, when
    # every such digest has expired.

    for key, ttl in (unsent.get("marks") or {}).get("sent", {}).items():
        current_app.redis.set(key, "1", nx=True, ex=ttl)

    since = unsent.get("since") or today
    if date.fromisoformat(since) < date.today() - timedelta(seconds=UNSENT_SECONDS):
        current_app.logger.error(
            f"Watchlist availability: the digest of user {user_id} waited "
            f"since {since} and was never sent. Its events are lost"
        )
        return events, today
    listed = {
        movie_id
        for (movie_id,) in db.session.query(UserWatchlist.movie_id).filter_by(
            user_id=int(user_id)
        )
    }
    owned = {
        movie_id
        for (movie_id,) in db.session.query(Movie.id).filter(
            Movie.id.in_(listed or [0]),
            Movie.files.any(File.feature_type_id.is_(None)),
        )
    }
    merged = {}
    for kind, items in events.items():
        kept = [
            item
            for item in (unsent.get("events") or {}).get(kind, [])
            if item.get("movie_id") in listed
            and (kind == "local" or item.get("movie_id") not in owned)
        ]
        seen = {item["movie_id"] for item in kept}
        merged[kind] = kept + [item for item in items if item["movie_id"] not in seen]
    return merged, since


def _keep_unsent(user_id, events, since):
    """Keep the events of a digest that was not sent for the next run."""

    current_app.redis.set(
        UNSENT_KEY.format(user_id=int(user_id)),
        json.dumps({"since": since, "events": events}),
        ex=UNSENT_KEY_SECONDS,
    )


def notify_watchlist_availability():
    """Run the nightly alert task, after the availability refresh.

    This compares the availability and the library presence of each
    watchlisted film with the stored snapshots. It stamps the per-user
    badge records. It sends each opted-in user a maximum of 1 digest
    email. The mails go out after the events of all the users are
    recorded. A mail that fails stops no other mail (#377). Its events
    wait for the next run."""

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
        outbox = []
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

            events_total += sum(len(items) for items in events.values())
            if not (user.notify_availability and user.email):
                current_app.redis.delete(UNSENT_KEY.format(user_id=user.id))
                continue
            events, since = _with_unsent(user.id, events)
            if any(events.values()):
                outbox.append((user, events, since))

        for user, events, since in outbox:
            try:
                _send_digest(user, events)
            except Exception as e:
                current_app.logger.warning(
                    f"Watchlist availability: the digest of user {user.id} was "
                    f"not sent. It waits for the next run: {e}"
                )
                _keep_unsent(user.id, events, since)
                continue
            digests += 1

        current_app.logger.info(
            f"Watchlist availability: {events_total} event(s) across "
            f"{len(users)} watchlist user(s), {digests} digest(s) mailed"
        )
        return True
