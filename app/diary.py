"""Diary writers (the strangler split from app.videos): every task
that turns an outside signal into UserMovieReview rows and watchlist
state.

The Letterboxd account-export importer (merging diary/ratings/reviews/
likes/watchlist CSVs idempotently), the Plex scrobble/history-poll
watch recorder, the API review task, and the shared verdict plumbing —
star_rating_fields and the watchlist/not-interested clearers that a
new log always runs.

app.videos re-exports every name here, so stored rq job strings
("app.videos.letterboxd_import_task") and import sites keep resolving;
record creation leans on app.videos' TMDb helpers via lazy imports,
keeping the module import direction one-way.
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

from app import db, get_app
from app.models import (
    Movie,
    User,
    UserMovieReview,
    UserMovieStatus,
    UserWatchlist,
    tmdb_get,
)


def clear_watchlist(user_id, movie_id):
    """Drop a film from a user's watchlist, if present — watching it,
    however the watch arrives, is what completes a watchlist entry.
    Callers commit."""

    UserWatchlist.query.filter_by(user_id=int(user_id), movie_id=int(movie_id)).delete()


def clear_not_interested(user_id, movie_id):
    """Drop a film's not-interested flag, if present — a watch, however
    it arrives, contradicts "never saw it, don't want it".
    Callers commit."""

    UserMovieStatus.query.filter_by(
        user_id=int(user_id), movie_id=int(movie_id), kind="not_interested"
    ).delete()


def star_rating_fields(rating):
    """The UserMovieReview rating columns for a 0-5 star rating (or None)."""

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

    Combines diary.csv (watch dates and per-watch ratings), ratings.csv
    (each film's current rating), reviews.csv (review text), and
    likes/films.csv (hearts). Returns a list of films, each with a list of
    entries mirroring how Letterboxd's own importer treats rows: one entry
    per watched date, plus a dateless entry for films that were only rated
    or liked.
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

        # watchlist.csv is the CURRENT want-to-watch list, so it wins
        # over any past watches in the same export

        for row in rows(zf, "watchlist.csv"):
            key = film_key(row)
            if key:
                film(key)["watchlist"] = True

        # Diary rows and review rows describe the same watch when they
        # share a watched date, so entries are keyed by that date

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

                # Stored as stated: Letterboxd knows about viewings that
                # predate this app, so a blank cell is a first watch, not
                # an unknown — only rows without the column stay None

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
    """Casefold a title and iron out the typography that separates
    Letterboxd's rendering from TMDb's — en/em dashes vs hyphens, curly
    vs straight quotes — so equality means the same words."""

    text = (title or "").casefold()
    for dash in ("–", "—", "−"):
        text = text.replace(dash, "-")
    text = text.replace("‘", "'").replace("’", "'")
    text = text.replace("“", '"').replace("”", '"')
    return " ".join(text.split())


def _pick_tmdb_match(title, year, year_filtered, title_only):
    """Choose the TMDb search result for a Letterboxd film.

    An exact (normalized) title among the year-filtered results wins;
    otherwise the title-only search's exact match with the nearest year,
    accepted within two years — or at any distance when it is the only
    exact match, since TMDb and Letterboxd years can drift far apart
    (The Men Who Tread on the Tiger's Tail: 1945 vs 1952). Only then
    the year-filtered head, which matched through an alternative title
    in the right year (Waking Ned Devine → Waking Ned). Taking that
    head FIRST is how "300" (2006) once imported as the short film
    "My Poetic Works 300 Yen".
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
    """Network phase of a Letterboxd import: match each film to the library
    or to TMDb, then hand the resolved list to apply_letterboxd_import on
    the sql queue.

    Runs on the user-request queue since resolving unowned films means
    TMDb searches; nothing here writes to the database.
    """

    with app.app_context():
        try:
            tmdb_api_key = current_app.config["TMDB_API_KEY"]
            tmdb_api_url = current_app.config["TMDB_API_URL"]
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

                # Search with the year first; the title-only search runs
                # only when no year-filtered result carries the exact
                # title, and _pick_tmdb_match arbitrates between the two

                r = tmdb_get(
                    tmdb_api_url + "/search/movie",
                    params={
                        "api_key": tmdb_api_key,
                        "query": title,
                        "primary_release_year": year,
                    },
                )
                r.raise_for_status()
                year_filtered = r.json().get("results") or []
                title_only = []
                wanted = _normalize_title(title)
                if not any(
                    _normalize_title(c.get("title")) == wanted for c in year_filtered
                ):
                    r = tmdb_get(
                        tmdb_api_url + "/search/movie",
                        params={"api_key": tmdb_api_key, "query": title},
                    )
                    r.raise_for_status()
                    title_only = r.json().get("results") or []
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
                current_app.sql_queue.enqueue(
                    "app.videos.apply_letterboxd_import",
                    args=(user_id, resolved),
                    job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
                    description=(
                        f"Importing Letterboxd data for {len(resolved)} film(s)"
                    ),
                )

        except Exception:
            current_app.logger.error(traceback.format_exc())
            return False

        else:
            return True


def apply_letterboxd_import(user_id, films):
    """Database phase of a Letterboxd import: create any missing movie
    records, then insert or update one review row per watch entry.

    Mirrors Letterboxd's own importer semantics: an entry updates the
    existing review with the same film and watched date instead of
    duplicating it, so re-importing the same export is idempotent. Movies
    created here are enriched afterwards through the standard TMDb
    refresh pipeline.
    """

    with app.app_context():
        try:
            created_movie_ids = []
            imported = 0

            for film in films:
                movie = None
                if film.get("movie_id"):
                    movie = Movie.query.filter_by(id=film["movie_id"]).first()
                elif film.get("tmdb_id") is not None:
                    movie = Movie.query.filter_by(tmdb_id=film["tmdb_id"]).first()
                    if movie is None:
                        # The canonical name may collide with an existing
                        # record; reuse it rather than violating the unique
                        # title + year constraint

                        movie = Movie.query.filter_by(
                            title=film["canonical_title"],
                            year=film["canonical_year"],
                        ).first()
                        if movie is not None and movie.tmdb_id is None:
                            movie.tmdb_id = film["tmdb_id"]
                    if movie is None:
                        movie = Movie(
                            title=film["canonical_title"],
                            year=film["canonical_year"],
                            tmdb_id=film["tmdb_id"],
                        )
                        db.session.add(movie)
                        db.session.flush()
                        created_movie_ids.append(movie.id)

                if movie is None:
                    continue

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

                    # Match per calendar day: a Plex-recorded watch carries a
                    # time of day, and re-importing the same Letterboxd date
                    # must update that row, not sit beside it

                    if date_watched is not None:
                        review = UserMovieReview.query.filter(
                            UserMovieReview.user_id == user_id,
                            UserMovieReview.movie_id == movie.id,
                            UserMovieReview.date_watched >= date_watched,
                            UserMovieReview.date_watched
                            < date_watched + timedelta(days=1),
                        ).first()
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

                # A watched import completes any old watchlist entry, but
                # watchlist.csv reflects Letterboxd's CURRENT list — so it
                # re-adds afterwards and wins over past watches

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

            # Enrich the newly created movies through the standard two-phase
            # refresh pipeline (TMDb fetch on the request queue, database
            # apply back on this queue)

            for movie_id in created_movie_ids:
                movie = Movie.query.filter_by(id=movie_id).first()
                current_app.request_queue.enqueue(
                    "app.videos.refresh_tmdb_info",
                    args=("Movies", movie_id, movie.tmdb_id),
                    job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
                    description=(
                        f"Refreshing TMDB data for '{movie.title} ({movie.year})'"
                    ),
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
    """Record one Plex movie watch, from either the webhook or the poller.

    Every watch bumps the movie's household shopping-cart priority.
    When the Plex account
    maps to a Fitzflix user via User.plex_username, the watch also lands
    in their diary as an unrated review row keyed on user/movie/date —
    with rewatch computed from whether any earlier row exists.

    A Redis marker keyed on account/movie/date makes the two sources
    idempotent: whichever records the watch first wins, and repeats of the
    same film on the same day don't double-count.
    """

    with app.app_context():
        try:
            # Full timestamp in local wall-clock time, like every other
            # diary writer: the calendar day used for dedup and display
            # must be the household's day, not UTC's (a 9 PM watch is not
            # tomorrow's viewing)

            watched_at = (
                datetime.fromisoformat(viewed_at).astimezone().replace(tzinfo=None)
            )
            day_start = datetime(watched_at.year, watched_at.month, watched_at.day)
            marker = (
                f"fitzflix:plex:watch:{plex_username}:{tmdb_id}:"
                f"{day_start.strftime('%Y-%m-%d')}"
            )
            if not current_app.redis.set(marker, source, nx=True, ex=172800):
                current_app.logger.debug(
                    f"Plex watch already recorded ({marker}); skipping"
                )
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
                # The watch completes any watchlist entry — and clears a
                # not-interested flag, which a real watch contradicts —
                # with one diary row per calendar day, whatever the times

                clear_watchlist(user.id, movie.id)
                clear_not_interested(user.id, movie.id)
                existing = UserMovieReview.query.filter(
                    UserMovieReview.user_id == user.id,
                    UserMovieReview.movie_id == movie.id,
                    UserMovieReview.date_watched >= day_start,
                    UserMovieReview.date_watched < day_start + timedelta(days=1),
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
            return False

        else:
            return True


def _plex_tmdb_id(entry, headers):
    """Resolve a Plex history entry to a TMDb id via its metadata Guid
    list, cached in Redis since rating keys are stable."""

    rating_key = entry.get("ratingKey")
    if not rating_key:
        return None
    cache_key = f"fitzflix:plex:tmdb:{rating_key}"
    cached = current_app.redis.get(cache_key)
    if cached is not None:
        # An empty value means known-unresolvable (no TMDb guid)
        return int(cached) if cached else None

    tmdb_id = None
    try:
        r = requests.get(
            f"{current_app.config['PLEX_URL']}/library/metadata/{rating_key}",
            headers=headers,
            timeout=30,
        )
        r.raise_for_status()
        items = (r.json().get("MediaContainer") or {}).get("Metadata") or []
        guids = (items[0].get("Guid") or []) if items else []
        for guid in guids:
            match = re.match(r"tmdb://(\d+)", guid.get("id") or "")
            if match:
                tmdb_id = int(match.group(1))
                break
        if tmdb_id is None and items:
            # Legacy metadata agent: the guid is a single string
            match = re.search(r"themoviedb://(\d+)", items[0].get("guid") or "")
            if match:
                tmdb_id = int(match.group(1))
    except Exception:
        # Don't cache transient failures
        current_app.logger.warning(traceback.format_exc())
        return None

    current_app.redis.set(cache_key, str(tmdb_id) if tmdb_id else "", ex=604800)
    return tmdb_id


def plex_history_poll():
    """Poll Plex's watch history for movie scrobbles past the stored cursor.

    The self-healing backstop to the real-time webhook: anything Plex
    scrobbled while Fitzflix was down is picked up here, and the shared
    dedup marker in apply_plex_watch keeps the two sources from
    double-counting. The first run only plants the cursor, so history
    predating the feature isn't ingested.
    """

    with app.app_context():
        config = current_app.config
        if not (config["PLEX_URL"] and config["PLEX_TOKEN"]):
            return True

        redis_conn = current_app.redis
        headers = {"X-Plex-Token": config["PLEX_TOKEN"], "Accept": "application/json"}
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

        try:
            r = requests.get(
                f"{config['PLEX_URL']}/status/sessions/history/all",
                headers={**headers, "X-Plex-Container-Size": "500"},
                params={"viewedAt>": cursor, "sort": "viewedAt:asc"},
                timeout=30,
            )
            r.raise_for_status()
            entries = (r.json().get("MediaContainer") or {}).get("Metadata") or []
        except Exception:
            current_app.logger.error(traceback.format_exc())
            return False

        # Server-account id -> Plex username, for watcher attribution; a
        # failure here still counts watches toward household priority

        accounts = {}
        try:
            r = requests.get(
                f"{config['PLEX_URL']}/accounts", headers=headers, timeout=30
            )
            r.raise_for_status()
            for account in (r.json().get("MediaContainer") or {}).get("Account") or []:
                accounts[account.get("id")] = account.get("name")
        except Exception:
            current_app.logger.warning(traceback.format_exc())

        newest = cursor
        queued = 0
        for entry in entries:
            viewed_at = int(entry.get("viewedAt") or 0)
            newest = max(newest, viewed_at)
            if entry.get("type") != "movie" or viewed_at <= cursor:
                continue
            tmdb_id = _plex_tmdb_id(entry, headers)
            if tmdb_id is None:
                current_app.logger.info(
                    f"Plex history entry '{entry.get('title')}' has no TMDb "
                    f"guid; ignoring"
                )
                continue
            account_id = entry.get("accountID")
            username = accounts.get(account_id) or f"account-{account_id}"
            current_app.sql_queue.enqueue(
                "app.videos.apply_plex_watch",
                args=(
                    tmdb_id,
                    username,
                    datetime.fromtimestamp(viewed_at, tz=timezone.utc).isoformat(),
                    "history",
                ),
                job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
                description=f"Recording Plex watch of tmdb:{tmdb_id} by {username}",
            )
            queued += 1

        redis_conn.set(cursor_key, newest)
        if queued:
            current_app.logger.info(f"Plex history poll: {queued} new watch(es)")
        return True


def review_task(user_id, title, rating):
    """Import movie reviews from a Netflix export."""

    with app.app_context():
        try:
            # A title alone can be ambiguous, since the Netflix export has no
            # year; if multiple movies share this title, fall through to TMDb,
            # which resolves the year, rather than guessing with .first()

            movie_matches = Movie.query.filter_by(title=title).all()

            if len(movie_matches) == 1:
                movie = movie_matches[0]

            else:
                movie = None
                if len(movie_matches) > 1:
                    current_app.logger.warning(
                        f"'{title}' matches {len(movie_matches)} movies in the "
                        f"library; resolving via TMDb"
                    )

            if not movie:
                tmdb_info = {}
                if not current_app.config["TMDB_API_KEY"]:
                    return False
                tmdb_api_key = current_app.config["TMDB_API_KEY"]
                tmdb_api_url = current_app.config["TMDB_API_URL"]
                current_app.logger.info(f"'{title}' not in database, searching in TMDB")
                r = tmdb_get(
                    tmdb_api_url + "/search/movie",
                    params={
                        "api_key": tmdb_api_key,
                        "query": title,
                    },
                )
                r.raise_for_status()
                current_app.logger.debug(f"{r.url}: {r.json()}")
                if len(r.json().get("results")) > 0:
                    first_result = r.json().get("results")[0]
                    tmdb_id = first_result.get("id")

                    if tmdb_id and title == first_result.get("title"):
                        current_app.logger.info(f"'{title}' Getting details from TMDB")

                        # Only the canonical title and release date are read
                        # here — the movie's full enrichment happens in
                        # tmdb_movie_query below

                        r = tmdb_get(
                            tmdb_api_url + "/movie/" + str(tmdb_id),
                            params={"api_key": tmdb_api_key},
                        )
                        r.raise_for_status()
                        current_app.logger.debug(f"{r.url}: {r.json()}")
                        tmdb_info = r.json()

                        tmdb_title = tmdb_info.get("title")
                        tmdb_year = None
                        if tmdb_info.get("release_date"):
                            tmdb_release_date = datetime.strptime(
                                tmdb_info.get("release_date"), "%Y-%m-%d"
                            )
                            tmdb_year = tmdb_release_date.year

                        if tmdb_title and tmdb_year:
                            # A movie with the canonical title may already
                            # exist; attach the review to it instead of
                            # violating the unique title + year constraint

                            movie = Movie.query.filter_by(
                                title=tmdb_title, year=tmdb_year
                            ).first()

                            if not movie:
                                movie = Movie(title=tmdb_title, year=tmdb_year)
                                db.session.add(movie)

                                try:
                                    # Establish a savepoint with db.session.begin_nested(),
                                    # so if any of the queries to get show metadata fail,
                                    # we can just roll back those changes to the savepoint
                                    # and still commit the movie and its review.

                                    db.session.begin_nested()
                                    movie.tmdb_movie_query()
                                    db.session.commit()

                                except Exception:
                                    current_app.logger.error(traceback.format_exc())
                                    db.session.rollback()

            if movie:
                modified_rating = round(rating * 2) / 2
                whole_stars = math.floor(modified_rating)
                if modified_rating % 1 == 0:
                    half_stars = 0
                else:
                    half_stars = 1

                review = UserMovieReview(
                    user_id=user_id,
                    movie_id=movie.id,
                    rating=rating,
                    modified_rating=modified_rating,
                    whole_stars=whole_stars,
                    half_stars=half_stars,
                    review="",
                    date_watched=None,
                    date_reviewed=None,
                )
                db.session.add(review)
                db.session.commit()
                current_app.logger.info(f"Rated '{title}' {rating} out of 5 stars")

        except Exception:
            current_app.logger.error(traceback.format_exc())
            db.session.rollback()

        else:
            return True


# This process's app instance, resolved lazily so importing this module from
# a process that already has an application doesn't build a second one

app = LocalProxy(get_app)
