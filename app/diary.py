"""Write the diary. This module is the strangler split from app.videos.

It holds each task that turns an outside signal into UserMovieReview
rows and watchlist state. That includes the Letterboxd account-export
importer. The importer merges the diary, ratings, reviews, likes, and
watchlist CSV files. A second import of the same export is idempotent.
The module also holds the Plex watch recorder for the scrobble and the
history poll, the API review task, and the shared verdict functions.
Those are star_rating_fields and the watchlist and not-interested
clearers that a new log always runs.

app.videos exports each name here again. Thus, the stored rq job
strings ("app.videos.letterboxd_import_task") and the import sites
continue to resolve. Record creation uses the TMDB helpers of
app.videos through lazy imports. Thus, the module import direction
stays one-way.
"""

import csv
import io
import math
import re
import time
import traceback
import zipfile

from datetime import datetime, timedelta, timezone

import requests
from flask import current_app
from werkzeug.local import LocalProxy

from app.cache import cache_json, cached_fetch
from app.tasks import enqueue_task
from app import db, get_app
from app.models import (
    Movie,
    User,
    UserMovieReview,
    UserMovieStatus,
    UserWatchlist,
    clip_movie_title,
)
from app.plex_api import plex_get
from app.tmdb_apply import tmdb_json

# The TMDB id of a Plex rating key, cached for 1 week. The rating keys
# are stable.

PLEX_TMDB_KEY = "fitzflix:plex:tmdb:{rating_key}"
PLEX_TMDB_SECONDS = 604800

# The marker of 1 recorded Plex watch per user, film and day. The
# webhook and the history poll both record a watch. The marker keeps
# the second from adding a row.

PLEX_WATCH_KEY = "fitzflix:plex:watch:{username}:{tmdb_id}:{day}"
PLEX_WATCH_SECONDS = 2 * 86400

# The marker of 1 watch of the Plex history, by film and exact viewedAt
# (#379). The history poll can read a watch again after the day marker
# expired. This marker keeps the watch from adding to the priority again.

PLEX_HISTORY_WATCH_KEY = "fitzflix:plex:history-watch:{tmdb_id}:{viewed_at}"
PLEX_HISTORY_WATCH_SECONDS = 30 * 86400

# The history entries at the second of the poll cursor that a poll has
# handled (#425).

PLEX_CURSOR_SEEN_KEY = "fitzflix:plex:history-cursor-seen"

# The last Plex account names that the history poll read. A poll whose
# /accounts call fails uses them. Then the username of the watch is the
# name that the webhook gives, and the day marker matches.

PLEX_ACCOUNTS_KEY = "fitzflix:plex:accounts"

# The polls in which a history ratingKey answered HTTP 404. A restored
# Plex library can bring a missing item back with the same ratingKey
# (the Oct 16 restore). Thus, the poll holds its cursor for a 404 until
# PLEX_MISSING_POLLS polls in a row saw it (#379 follow-up). Then the
# entry counts as "no TMDB id" for PLEX_MISSING_CACHE_SECONDS.

PLEX_MISSING_KEY = "fitzflix:plex:missing:{rating_key}"
PLEX_MISSING_POLLS = 3
PLEX_MISSING_SECONDS = 86400
PLEX_MISSING_CACHE_SECONDS = 3600

# The number of each history poll. A 404 counts once in each poll.

PLEX_POLL_COUNT_KEY = "fitzflix:plex:history-polls"


def clear_watchlist(user_id, movie_id):
    """Remove a film from the watchlist of a user, if the film is on it.

    A watch of the film completes the watchlist entry. The source of the
    watch is not important. The caller must commit the session."""

    UserWatchlist.query.filter_by(user_id=int(user_id), movie_id=int(movie_id)).delete()


def clear_not_interested(user_id, movie_id):
    """Remove the not-interested flag of a film, if the flag is set.

    A watch of the film contradicts "never saw it, do not want it". The
    source of the watch is not important. The caller must commit the
    session."""

    UserMovieStatus.query.filter_by(
        user_id=int(user_id), movie_id=int(movie_id), kind="not_interested"
    ).delete()


def _stars_changed(review, stars):
    """Return True if the rating columns of a review differ from stars.

    A Numeric column reads back as a Decimal. Thus, the values compare
    as floats."""

    for field, value in stars.items():
        current = getattr(review, field)
        if (current is None) != (value is None):
            return True
        if current is not None and float(current) != float(value):
            return True
    return False


def star_rating_fields(rating):
    """Return the UserMovieReview rating columns for a 0-5 star rating.

    The rating can also be None."""

    if rating is None:
        return {
            "rating": None,
            "modified_rating": None,
            "whole_stars": None,
            "half_stars": None,
        }
    modified_rating = round(rating * 2) / 2
    return {
        "rating": rating,
        "modified_rating": modified_rating,
        "whole_stars": math.floor(modified_rating),
        "half_stars": 0 if modified_rating % 1 == 0 else 1,
    }


def parse_letterboxd_export(zip_bytes):
    """Parse a Letterboxd account-export zip into one record per film.

    This combines diary.csv (the watch dates and the per-watch ratings),
    ratings.csv (the current rating of each film), reviews.csv (the
    review text), and likes/films.csv (the hearts). It returns a list of
    films. Each film has a list of entries. The entries follow the rules
    of the importer of Letterboxd: one entry per watched date, plus an
    entry without a date for a film that the user only rated or liked.
    """

    def rows(zf, name):
        if name not in zf.namelist():
            return []
        with zf.open(name) as f:
            return list(csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")))

    def film_key(row):
        title = (row.get("Name") or "").strip()
        year = (row.get("Year") or "").strip()
        if not title or not year.isdigit():
            return None
        return (title, int(year))

    films = {}

    def film(key):
        if key not in films:
            films[key] = {
                "title": key[0],
                "year": key[1],
                "rating": None,
                "liked": False,
                "watchlist": False,
                "entries": {},
            }
        return films[key]

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for row in rows(zf, "ratings.csv"):
            key = film_key(row)
            if key and row.get("Rating"):
                film(key)["rating"] = float(row["Rating"])

        for row in rows(zf, "likes/films.csv"):
            key = film_key(row)
            if key:
                film(key)["liked"] = True

        # watchlist.csv is the CURRENT want-to-watch list. Thus, it wins
        # over the past watches in the same export.

        for row in rows(zf, "watchlist.csv"):
            key = film_key(row)
            if key:
                film(key)["watchlist"] = True

        # A diary row and a review row with the same watched date describe
        # the same watch. Thus, the watched date is the key of the entry.

        for name in ("diary.csv", "reviews.csv"):
            for row in rows(zf, name):
                key = film_key(row)
                if not key:
                    continue
                watched = (row.get("Watched Date") or "").strip() or None
                entry = film(key)["entries"].setdefault(
                    watched,
                    {
                        "watched": watched,
                        "logged": None,
                        "rating": None,
                        "review": None,
                        "rewatch": None,
                    },
                )
                if row.get("Date"):
                    entry["logged"] = entry["logged"] or row["Date"].strip()
                if row.get("Rating"):
                    entry["rating"] = float(row["Rating"])
                if row.get("Review"):
                    entry["review"] = row["Review"]

                # Store the value as given. Letterboxd knows the viewings
                # that came before this app. Thus, a blank cell is a first
                # watch, not an unknown. Only a row without the column
                # stays None.

                if "Rewatch" in row:
                    entry["rewatch"] = (row.get("Rewatch") or "").strip() == "Yes"

    results = []
    for f in films.values():
        f["entries"] = sorted(
            f["entries"].values(), key=lambda e: (e["watched"] is None, e["watched"])
        )
        if not f["entries"] and (f["rating"] is not None or f["liked"]):
            f["entries"] = [
                {
                    "watched": None,
                    "logged": None,
                    "rating": None,
                    "review": None,
                    "rewatch": None,
                }
            ]
        if f["entries"] or f["watchlist"]:
            results.append(f)
    return results


def _normalize_title(title):
    """Casefold a title and make its punctuation standard.

    Letterboxd and TMDB show the same title with different punctuation.
    Replace the en dash and the em dash with a hyphen. Replace curly
    quotes with straight quotes. Then two equal strings have the same
    words."""

    text = (title or "").casefold()
    for dash in ("–", "—", "−"):
        text = text.replace(dash, "-")
    text = text.replace("‘", "'").replace("’", "'")
    text = text.replace("“", '"').replace("”", '"')
    return " ".join(text.split())


def _pick_tmdb_match(title, year, year_filtered, title_only):
    """Choose the TMDB search result for a Letterboxd film.

    An exact (normalized) title among the year-filtered results wins.
    If there is none, use the exact match of the title-only search with
    the nearest year. Accept it if the year is within 2 years. Also
    accept it at any distance if it is the only exact match. The TMDB
    year and the Letterboxd year can differ a lot (The Men Who Tread on
    the Tiger's Tail: 1945 and 1952). Only then use the first
    year-filtered result. That result matched through an alternative
    title in the correct year (Waking Ned Devine to Waking Ned). Do not
    use that first result FIRST. That is how "300" (2006) one time
    imported as the short film "My Poetic Works 300 Yen".
    """

    wanted = _normalize_title(title)
    for candidate in year_filtered:
        if _normalize_title(candidate.get("title")) == wanted:
            return candidate
    exacts = []
    for candidate in title_only:
        candidate_year = (candidate.get("release_date") or "")[:4]
        if (
            _normalize_title(candidate.get("title")) == wanted
            and candidate_year.isdigit()
        ):
            exacts.append((abs(int(candidate_year) - year), candidate))
    if exacts:
        distance, best = min(exacts, key=lambda pair: pair[0])
        if distance <= 2 or len(exacts) == 1:
            return best
    if year_filtered:
        return year_filtered[0]
    return None


def letterboxd_import_task(user_id, films):
    """Run the network phase of a Letterboxd import.

    This matches each film to the library or to TMDB. Then it gives the
    resolved list to apply_letterboxd_import on the sql queue. This runs
    on the user-request queue, because unowned films need TMDB searches.
    Nothing here writes to the database.
    """

    with app.app_context():
        try:
            tmdb_api_key = current_app.config["TMDB_API_KEY"]
            resolved = []
            skipped = []

            for film in films:
                title, year = film["title"], film["year"]

                movie = Movie.query.filter_by(title=title, year=year).first()
                if movie:
                    film["movie_id"] = movie.id
                    resolved.append(film)
                    continue

                if not tmdb_api_key:
                    skipped.append(f"{title} ({year})")
                    continue

                # Search with the year first. The title-only search runs
                # only if no year-filtered result has the exact title.
                # _pick_tmdb_match decides between the two.

                year_filtered = (
                    tmdb_json(
                        "/search/movie",
                        {"query": title, "primary_release_year": year},
                        timeout=30,
                    ).get("results")
                    or []
                )
                title_only = []
                wanted = _normalize_title(title)
                if not any(
                    _normalize_title(c.get("title")) == wanted for c in year_filtered
                ):
                    title_only = (
                        tmdb_json("/search/movie", {"query": title}, timeout=30).get(
                            "results"
                        )
                        or []
                    )
                result = _pick_tmdb_match(title, year, year_filtered, title_only)

                if not result:
                    skipped.append(f"{title} ({year})")
                    continue

                existing = Movie.query.filter_by(tmdb_id=result.get("id")).first()
                if existing:
                    film["movie_id"] = existing.id
                else:
                    film["tmdb_id"] = result.get("id")
                    film["canonical_title"] = result.get("title") or title
                    release_year = (result.get("release_date") or "")[:4]
                    film["canonical_year"] = (
                        int(release_year) if release_year.isdigit() else year
                    )
                resolved.append(film)

            if skipped:
                current_app.logger.warning(
                    f"Letterboxd import: no match for {len(skipped)} film(s): "
                    f"{', '.join(skipped)}"
                )

            if resolved:
                enqueue_task(
                    "app.videos.apply_letterboxd_import",
                    user_id,
                    resolved,
                    description=f"Importing Letterboxd data for {len(resolved)} film(s)",
                )

        except Exception:
            current_app.logger.error(traceback.format_exc())
            return False

        else:
            return True


def _feed_claimed_twin(user_id, movie_id, date_watched, import_days, claimed):
    """Return the feed row of a watch 1 day away from a Letterboxd date, or None.

    The feed sync gives a Plex scrobble near midnight to the Letterboxd
    entry of the next or previous date. The row keeps its clock time,
    for example 00:10 on 10-07 for the entry of 10-06 (#381). A twin is
    a row with a guid on the next day before noon, or on the day before
    at noon or later. Thus, a twin is next to 1 date only. A day that
    has its own entry in the import is not a twin. claimed holds the ids
    of the rows that this import gave to an entry already. Such a row
    is not a twin again. The function adds the id of the twin."""

    rows = UserMovieReview.query.filter(
        UserMovieReview.user_id == user_id,
        UserMovieReview.movie_id == movie_id,
        UserMovieReview.letterboxd_guid.isnot(None),
        UserMovieReview.date_watched >= date_watched - timedelta(days=1),
        UserMovieReview.date_watched < date_watched + timedelta(days=2),
    ).order_by(UserMovieReview.date_watched)
    for row in rows:
        day, clock = row.date_watched.date(), row.date_watched.time()
        offset = (day - date_watched.date()).days
        early = clock.hour < 12
        if (
            row.id not in claimed
            and day not in import_days
            and clock != datetime.min.time()
            and ((offset == 1 and early) or (offset == -1 and not early))
        ):
            claimed.add(row.id)
            return row
    return None


def apply_letterboxd_import(user_id, films):
    """Run the database phase of a Letterboxd import.

    This creates the missing movie records. Then it inserts or updates
    one review row per watch entry. It follows the rules of the importer
    of Letterboxd. An entry updates the existing review with the same
    film and watched date. It does not make a duplicate. Thus, a second
    import of the same export is idempotent. The standard TMDB refresh
    pipeline enriches the movies that this creates.
    """

    from app.tmdb_refresh import find_or_create_tmdb_movie

    with app.app_context():
        try:
            created_movie_ids = []
            imported = 0

            for film in films:
                movie = None
                if film.get("movie_id"):
                    movie = Movie.query.filter_by(id=film["movie_id"]).first()
                elif film.get("tmdb_id") is not None:
                    # The canonical name can belong to a record of another
                    # film. find_or_create_tmdb_movie then returns None,
                    # and the entry is skipped. The review must not go to
                    # the wrong film (#357).

                    movie, created = find_or_create_tmdb_movie(
                        film["tmdb_id"],
                        film["canonical_title"],
                        film["canonical_year"],
                    )
                    if created:
                        db.session.flush()
                        created_movie_ids.append(movie.id)

                if movie is None:
                    continue

                claimed = set()
                import_days = {
                    datetime.strptime(entry["watched"], "%Y-%m-%d").date()
                    for entry in film["entries"]
                    if entry["watched"]
                }
                for entry in film["entries"]:
                    date_watched = (
                        datetime.strptime(entry["watched"], "%Y-%m-%d")
                        if entry["watched"]
                        else None
                    )
                    date_reviewed = (
                        datetime.strptime(entry["logged"], "%Y-%m-%d")
                        if entry["logged"]
                        else None
                    )
                    rating = (
                        entry["rating"]
                        if entry["rating"] is not None
                        else film["rating"]
                    )

                    # Match per calendar day. A watch that Plex recorded has
                    # a time of day. A second import of the same Letterboxd
                    # date must update that row, not add a row next to it.
                    # Then the scrobble that the feed gave to this date
                    # from the day next to it (#381).

                    if date_watched is not None:
                        review = UserMovieReview.query.filter(
                            UserMovieReview.user_id == user_id,
                            UserMovieReview.movie_id == movie.id,
                            UserMovieReview.date_watched >= date_watched,
                            UserMovieReview.date_watched
                            < date_watched + timedelta(days=1),
                        ).first() or _feed_claimed_twin(
                            user_id, movie.id, date_watched, import_days, claimed
                        )
                    else:
                        review = UserMovieReview.query.filter_by(
                            user_id=user_id, movie_id=movie.id, date_watched=None
                        ).first()
                    if review is None:
                        review = UserMovieReview(
                            user_id=user_id,
                            movie_id=movie.id,
                            review=entry["review"] or "",
                            date_watched=date_watched,
                            date_reviewed=date_reviewed,
                            rewatch=entry.get("rewatch"),
                            **star_rating_fields(rating),
                        )
                        db.session.add(review)
                    else:
                        if rating is not None:
                            for field, value in star_rating_fields(rating).items():
                                setattr(review, field, value)
                        if entry["review"]:
                            review.review = entry["review"]
                        if date_reviewed and not review.date_reviewed:
                            review.date_reviewed = date_reviewed
                        if entry.get("rewatch") is not None:
                            review.rewatch = entry["rewatch"]
                    review.liked = review.liked or film["liked"]
                    imported += 1

                # A watched import completes an old watchlist entry. But
                # watchlist.csv shows the CURRENT list of Letterboxd. Thus,
                # it adds the entry again after that and wins over the past
                # watches.

                if film["entries"]:
                    clear_watchlist(user_id, movie.id)
                    clear_not_interested(user_id, movie.id)
                if film.get("watchlist"):
                    listed = UserWatchlist.query.filter_by(
                        user_id=user_id, movie_id=movie.id
                    ).first()
                    if listed is None:
                        db.session.add(
                            UserWatchlist(user_id=user_id, movie_id=movie.id)
                        )

            db.session.commit()

            # Enrich the new movies through the standard two-phase refresh
            # pipeline. The TMDB fetch runs on the request queue. The
            # database apply runs back on this queue.

            for movie_id in created_movie_ids:
                movie = Movie.query.filter_by(id=movie_id).first()
                enqueue_task(
                    "app.videos.refresh_tmdb_info",
                    "Movies",
                    movie_id,
                    movie.tmdb_id,
                    description=f"Refreshing TMDB data for '{movie.title} ({movie.year})'",
                )

            current_app.logger.info(
                f"Letterboxd import: {imported} review entries across "
                f"{len(films)} films ({len(created_movie_ids)} new movie records)"
            )

        except Exception:
            current_app.logger.error(traceback.format_exc())
            db.session.rollback()
            return False

        else:
            return True


def apply_plex_watch(tmdb_id, plex_username, viewed_at, source):
    """Record one Plex movie watch, from the webhook or from the poller.

    Each watch increases the household shopping-cart priority of the
    movie. The Plex account can map to a Fitzflix user through
    User.plex_username. Then the watch also goes into the diary of that
    user as an unrated review row. The key of the row is the user, the
    movie, and the date. The rewatch flag is True if an earlier row
    exists.

    A Redis marker with the key account, movie, and date makes the two
    sources idempotent. The source that records the watch first wins. A
    repeat of the same film on the same day does not count two times.
    """

    with app.app_context():
        marker = history_marker = None
        try:
            # This is the full timestamp in local wall-clock time, as in
            # each other diary writer. The calendar day for dedup and
            # display must be the day of the household, not the UTC day.
            # A watch at 9 PM is not a viewing of tomorrow.

            watched_at = (
                datetime.fromisoformat(viewed_at).astimezone().replace(tzinfo=None)
            )
            day_start = datetime(watched_at.year, watched_at.month, watched_at.day)
            if source == "history":
                history_marker = PLEX_HISTORY_WATCH_KEY.format(
                    tmdb_id=tmdb_id, viewed_at=viewed_at
                )
                if not current_app.redis.set(
                    history_marker, source, nx=True, ex=PLEX_HISTORY_WATCH_SECONDS
                ):
                    history_marker = None
                    return True
            marker = PLEX_WATCH_KEY.format(
                username=plex_username,
                tmdb_id=tmdb_id,
                day=day_start.strftime("%Y-%m-%d"),
            )
            if not current_app.redis.set(
                marker, source, nx=True, ex=PLEX_WATCH_SECONDS
            ):
                current_app.logger.debug(
                    f"Plex watch already recorded ({marker}); skipping"
                )
                marker = None
                return True

            movie = Movie.query.filter_by(tmdb_id=int(tmdb_id)).first()
            if movie is None:
                current_app.logger.info(
                    f"Plex watch of tmdb:{tmdb_id} by '{plex_username}' matches "
                    f"no movie in the library; ignoring"
                )
                return True

            movie.shopping_cart_add_date = datetime.now(timezone.utc)
            movie.shopping_cart_priority = (movie.shopping_cart_priority or 0) + 1

            user = None
            if plex_username:
                user = User.query.filter_by(plex_username=plex_username).first()

            if user is not None:
                # The watch completes a watchlist entry. It also clears a
                # not-interested flag, because a real watch contradicts
                # that flag. There is one diary row per calendar day. The
                # times are not important.

                clear_watchlist(user.id, movie.id)
                clear_not_interested(user.id, movie.id)
                existing = UserMovieReview.query.filter(
                    UserMovieReview.user_id == user.id,
                    UserMovieReview.movie_id == movie.id,
                    UserMovieReview.date_watched >= day_start,
                    UserMovieReview.date_watched < day_start + timedelta(days=1),
                ).first()

                # A film that runs past midnight is logged on Letterboxd
                # for the day before. The feed row of that day has a
                # midnight stamp. A watch before noon is the same viewing
                # (the mirror of #381). Without this, the diary got 2
                # rows (#425).

                if existing is None and watched_at.hour < 12:
                    existing = UserMovieReview.query.filter(
                        UserMovieReview.user_id == user.id,
                        UserMovieReview.movie_id == movie.id,
                        UserMovieReview.letterboxd_guid.isnot(None),
                        UserMovieReview.date_watched == day_start - timedelta(days=1),
                    ).first()
                if existing is None:
                    rewatch = (
                        db.session.query(UserMovieReview.id)
                        .filter_by(user_id=user.id, movie_id=movie.id)
                        .first()
                        is not None
                    )
                    db.session.add(
                        UserMovieReview(
                            user_id=user.id,
                            movie_id=movie.id,
                            review="",
                            date_watched=watched_at,
                            rewatch=rewatch,
                            **star_rating_fields(None),
                        )
                    )

            db.session.commit()
            current_app.logger.info(
                f"Plex watch ({source}): '{movie.title} ({movie.year})' by "
                f"'{plex_username}'"
                + ("" if user is None else f" — recorded in {user.email}'s diary")
            )

        except Exception:
            current_app.logger.error(traceback.format_exc())
            db.session.rollback()

            # The watch is not recorded. Thus, release the marker. Then
            # the other source (the webhook or the history poll) records
            # the watch. Else the marker blocks it for 2 days.

            for key in (marker, history_marker):
                if key:
                    try:
                        current_app.redis.delete(key)
                    except Exception:
                        pass
            return False

        else:
            return True


def _history_identity(entry):
    """Return the text that names 1 Plex history entry."""

    key = entry.get("historyKey")
    if key:
        return str(key)
    return f"{entry.get('ratingKey')}:{entry.get('accountID')}:{entry.get('viewedAt')}"


def _plex_tmdb_id(entry, failures=None, poll=None):
    """Resolve a Plex history entry to a TMDB id through its Guid list.

    Redis caches the result, because the rating keys are stable. A
    failed lookup appends the entry to failures, if given. Thus, the
    caller can tell a failure from an entry with no TMDB guid. Plex
    keeps the history of a deleted item. Its lookup gives HTTP 404. A
    404 is a failure until PLEX_MISSING_POLLS different polls saw it.
    poll names the current poll. Then the entry has no TMDB id for
    PLEX_MISSING_CACHE_SECONDS, and the cursor can pass it (#379)."""

    rating_key = entry.get("ratingKey")
    if not rating_key:
        return None

    def fetch():
        tmdb_id = None
        try:
            body = plex_get(f"/library/metadata/{rating_key}", timeout=30)
            items = (body.get("MediaContainer") or {}).get("Metadata") or []
            guids = (items[0].get("Guid") or []) if items else []
            for guid in guids:
                match = re.match(r"tmdb://(\d+)", guid.get("id") or "")
                if match:
                    tmdb_id = int(match.group(1))
                    break
            if tmdb_id is None and items:
                # With the legacy metadata agent, the guid is one string.
                match = re.search(r"themoviedb://(\d+)", items[0].get("guid") or "")
                if match:
                    tmdb_id = int(match.group(1))
        except requests.HTTPError as e:
            if e.response is None or e.response.status_code != 404:
                return failed()
            missing = PLEX_MISSING_KEY.format(rating_key=rating_key)
            redis = current_app.redis
            redis.sadd(missing, str(poll))
            redis.expire(missing, PLEX_MISSING_SECONDS)
            if redis.scard(missing) < PLEX_MISSING_POLLS:
                return failed()
            redis.delete(missing)
            current_app.logger.info(
                f"Plex history entry '{entry.get('title')}' names the item "
                f"{rating_key}, which {PLEX_MISSING_POLLS} polls did not find; "
                f"ignoring"
            )
            # The short cache of this answer. An entry with no TMDB guid
            # keeps the long cache of cached_fetch.
            cache_json(cache_key, None, PLEX_MISSING_CACHE_SECONDS)
            return None, False
        except Exception:
            return failed()
        current_app.redis.delete(PLEX_MISSING_KEY.format(rating_key=rating_key))
        return tmdb_id, True

    def failed():
        # Do not cache a transient failure.
        current_app.logger.warning(traceback.format_exc())
        if failures is not None:
            failures.append(entry)
        return None, False

    # A null value means that the entry cannot resolve (no TMDB guid).

    cache_key = PLEX_TMDB_KEY.format(rating_key=rating_key)
    return cached_fetch(cache_key, PLEX_TMDB_SECONDS, fetch)


def plex_history_poll():
    """Poll the watch history of Plex for movie scrobbles after the cursor.

    This is the self-healing backstop of the real-time webhook. This
    collects each watch that Plex scrobbled while Fitzflix was down. The
    shared dedup marker in apply_plex_watch prevents a double count from
    the two sources. The first run only plants the cursor. Thus, this
    does not ingest the history from before the feature.
    """

    with app.app_context():
        config = current_app.config
        if not (config["PLEX_URL"] and config["PLEX_TOKEN"]):
            return True

        redis_conn = current_app.redis
        cursor_key = "fitzflix:plex:history-cursor"

        cursor = redis_conn.get(cursor_key)
        if cursor is None:
            redis_conn.set(cursor_key, int(time.time()))
            current_app.logger.info(
                "Plex history poll: cursor initialized; watches from now on "
                "will be recorded"
            )
            return True
        cursor = int(cursor)
        poll = redis_conn.incr(PLEX_POLL_COUNT_KEY)

        # A second watch can share the second of the cursor, and Plex can
        # list it only on the next poll. A read after the cursor second
        # lost it (#425). Thus, the poll reads the cursor second again.
        # It skips the entries of that second that it handled already.

        seen_at_cursor = {
            member.decode() if isinstance(member, bytes) else member
            for member in redis_conn.smembers(PLEX_CURSOR_SEEN_KEY)
        }

        try:
            body = plex_get(
                "/status/sessions/history/all",
                {
                    "viewedAt>": cursor - 1,
                    "sort": "viewedAt:asc",
                    "X-Plex-Container-Size": "500",
                },
                timeout=30,
            )
            entries = (body.get("MediaContainer") or {}).get("Metadata") or []
        except Exception:
            current_app.logger.error(traceback.format_exc())
            return False

        # This maps a server-account id to a Plex username for the watcher
        # attribution. After a failure here, the watches still count
        # toward the household priority.

        accounts = {}
        try:
            body = plex_get("/accounts", timeout=30)
            for account in (body.get("MediaContainer") or {}).get("Account") or []:
                accounts[account.get("id")] = account.get("name")
            names = {str(key): name for key, name in accounts.items() if name}
            if names:
                redis_conn.delete(PLEX_ACCOUNTS_KEY)
                redis_conn.hset(PLEX_ACCOUNTS_KEY, mapping=names)
        except Exception:
            current_app.logger.warning(traceback.format_exc())
            accounts = {
                int(key): name.decode()
                for key, name in redis_conn.hgetall(PLEX_ACCOUNTS_KEY).items()
            }

        # The cursor never passes an entry whose lookup failed. The next
        # poll reads it again. The entries after it are read again too.
        # The dedup marker of apply_plex_watch makes that safe.

        newest = cursor
        failed_at = None
        failed_ids = set()
        queued = 0
        for entry in entries:
            viewed_at = int(entry.get("viewedAt") or 0)
            newest = max(newest, viewed_at)
            if entry.get("type") != "movie" or viewed_at < cursor:
                continue
            if viewed_at == cursor and _history_identity(entry) in seen_at_cursor:
                continue

            # A live TV airing also has the type movie (#182). A
            # virtual-channel play is never a diary watch.

            if entry.get("live"):
                continue
            failures = []
            tmdb_id = _plex_tmdb_id(entry, failures, poll=poll)
            if failures:
                failed_ids.add(_history_identity(entry))
                failed_at = (
                    viewed_at if failed_at is None else min(failed_at, viewed_at)
                )
                continue
            if tmdb_id is None:
                current_app.logger.info(
                    f"Plex history entry '{entry.get('title')}' has no TMDB "
                    f"guid; ignoring"
                )
                continue
            account_id = entry.get("accountID")
            username = accounts.get(account_id) or f"account-{account_id}"
            enqueue_task(
                "app.videos.apply_plex_watch",
                tmdb_id,
                username,
                datetime.fromtimestamp(viewed_at, tz=timezone.utc).isoformat(),
                "history",
                description=f"Recording Plex watch of tmdb:{tmdb_id} by {username}",
            )
            queued += 1

        # The cursor never goes back. A failed lookup in the cursor second
        # holds the cursor there, and the entry stays out of the seen set.
        # The next poll then reads only that entry again.

        if failed_at is not None:
            newest = max(cursor, min(newest, failed_at - 1))
            current_app.logger.info(
                "Plex history poll: a TMDB lookup failed. The next poll reads "
                "that watch again"
            )
        # The entries of the cursor second that this poll handled go in
        # the seen set. The next poll reads that second again and skips
        # only them.

        seen_now = {
            _history_identity(entry)
            for entry in entries
            if int(entry.get("viewedAt") or 0) == newest
        } - failed_ids
        if newest <= cursor:
            seen_now |= seen_at_cursor
        pipeline = redis_conn.pipeline()
        pipeline.set(cursor_key, newest)
        pipeline.delete(PLEX_CURSOR_SEEN_KEY)
        if seen_now:
            pipeline.sadd(PLEX_CURSOR_SEEN_KEY, *seen_now)
        pipeline.execute()
        if queued:
            current_app.logger.info(f"Plex history poll: {queued} new watch(es)")
        return True


def review_task(user_id, title, rating):
    """Import movie reviews from a Netflix export."""

    with app.app_context():
        try:
            # A title alone can be ambiguous, because the Netflix export has
            # no year. If more than 1 movie has this title, continue to TMDB.
            # TMDB resolves the year. Do not guess with .first().

            movie_matches = Movie.query.filter_by(title=title).all()

            if len(movie_matches) == 1:
                movie = movie_matches[0]

            else:
                movie = None
                if len(movie_matches) > 1:
                    current_app.logger.warning(
                        f"'{title}' matches {len(movie_matches)} movies in the "
                        f"library; resolving via TMDB"
                    )

            if not movie:
                tmdb_info = {}
                if not current_app.config["TMDB_API_KEY"]:
                    return False
                current_app.logger.info(f"'{title}' not in database, searching in TMDB")
                search = tmdb_json("/search/movie", {"query": title}, timeout=30)
                current_app.logger.debug(f"/search/movie: {search}")
                if len(search.get("results")) > 0:
                    first_result = search.get("results")[0]
                    tmdb_id = first_result.get("id")

                    if tmdb_id and title == first_result.get("title"):
                        current_app.logger.info(f"'{title}' Getting details from TMDB")

                        # This reads only the canonical title and the release
                        # date. tmdb_movie_query below does the full
                        # enrichment of the movie.

                        tmdb_info = tmdb_json(f"/movie/{tmdb_id}", timeout=30)
                        current_app.logger.debug(f"/movie/{tmdb_id}: {tmdb_info}")

                        tmdb_title = clip_movie_title(tmdb_info.get("title"))
                        tmdb_year = None
                        if tmdb_info.get("release_date"):
                            tmdb_release_date = datetime.strptime(
                                tmdb_info.get("release_date"), "%Y-%m-%d"
                            )
                            tmdb_year = tmdb_release_date.year

                        if tmdb_title and tmdb_year:
                            # A movie with the canonical title can already
                            # exist. Then attach the review to that movie. Do
                            # not violate the unique title + year constraint.

                            movie = Movie.query.filter_by(
                                title=tmdb_title, year=tmdb_year
                            ).first()

                            if not movie:
                                movie = Movie(title=tmdb_title, year=tmdb_year)
                                db.session.add(movie)

                                try:
                                    # Make a savepoint with begin_nested(). If a
                                    # metadata query fails, roll back to the
                                    # savepoint. Then commit the movie and its
                                    # review.

                                    db.session.begin_nested()
                                    movie.tmdb_movie_query()
                                    db.session.commit()

                                except Exception:
                                    current_app.logger.error(traceback.format_exc())
                                    db.session.rollback()

            if movie and round(rating * 2) / 2 == 0:
                # 0 stars is the ✕ of the ladder: the not-interested flag,
                # never a review (#426). A film with a diary row is seen.
                # Thus, it gets no flag.

                seen = (
                    db.session.query(UserMovieReview.id)
                    .filter_by(user_id=user_id, movie_id=movie.id)
                    .first()
                    is not None
                )
                flagged = (
                    UserMovieStatus.query.filter_by(
                        user_id=user_id, movie_id=movie.id, kind="not_interested"
                    ).first()
                    is not None
                )
                if not seen and not flagged:
                    db.session.add(
                        UserMovieStatus(
                            user_id=user_id, movie_id=movie.id, kind="not_interested"
                        )
                    )
                    clear_watchlist(user_id, movie.id)
                db.session.commit()
                current_app.logger.info(f"Marked '{title}' as not interested")

            elif movie:
                # The import is a rating without a date. A second upload
                # of the same file updates the undated row. It does not
                # add a second row (#426).

                review = UserMovieReview.query.filter_by(
                    user_id=user_id, movie_id=movie.id, date_watched=None
                ).first()
                stars = star_rating_fields(rating)
                # The liked flag follows the stars only when the stars
                # change. An unchanged rating keeps a liked flag from
                # Letterboxd (follow-up #442).
                if review is None:
                    review = UserMovieReview(
                        user_id=user_id,
                        movie_id=movie.id,
                        review="",
                        date_watched=None,
                        date_reviewed=None,
                        liked=rating >= 3,
                    )
                    db.session.add(review)
                elif _stars_changed(review, stars):
                    # A changed rating must reach the incremental
                    # Letterboxd export, which reads date_updated (#442).
                    # This agrees with apply_row_edit.
                    review.date_updated = datetime.now()
                    if review.date_reviewed is None:
                        review.date_reviewed = review.date_updated
                    review.liked = rating >= 3
                for field, value in stars.items():
                    setattr(review, field, value)
                db.session.commit()
                current_app.logger.info(f"Rated '{title}' {rating} out of 5 stars")

        except Exception:
            current_app.logger.error(traceback.format_exc())
            db.session.rollback()

        else:
            return True


# This is the app instance of this process. Fitzflix resolves it lazily.
# Thus, a process that already has an application can import this module
# and not build a second application.

app = LocalProxy(get_app)
