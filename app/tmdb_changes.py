"""The TMDB change-driven refresh sweep.

TMDB's details payloads carry no last-updated stamp; the changes lists
(/movie/changes, /tv/changes) are the API's cache-invalidation feed — a
paginated list of every id edited in a window, capped at 14 days back.
A nightly task enumerates the window since the last successful sweep,
intersects it with the library's ids, and enqueues the standard TMDB
refresh for just the records that actually changed — so edits land
within a day without blindly re-fetching thousands of titles.

The window is enumerated in one-day slices: a single query near the
lookback cap can exceed the API's 500-page ceiling (movies alone run
~70 pages a day), while a day slice never comes close. A Redis
watermark records the last completed sweep and only advances when both
enumerations finished cleanly, so a partial night is re-covered by the
next — at worst a title is refreshed twice, which is harmless. A
watermark beyond the 14-day history is unrecoverable from here; the
sweep clamps to the cap and points the log at the maintenance page's
bulk refresh.

TV is filtered to ended and canceled series: everything else is
already re-fetched nightly by refresh_in_production_tv, and this sweep
exists to catch the metadata edits that sweep deliberately leaves to
"rarely changes" — without double-refreshing what it covers.
"""

import traceback

from datetime import datetime, timedelta, timezone

from flask import current_app
from werkzeug.local import LocalProxy

from app import db, get_app
from app.models import Movie, TVSeries, tmdb_get

# This process's app instance, resolved lazily so the nightly task can
# run on a worker without building a second application

app = LocalProxy(get_app)

# When the last completed sweep ran (ISO datetime, UTC); the next run's
# window opens here

LAST_RUN_KEY = "fitzflix:tmdb-changes:last-run"

# TMDB keeps 14 days of change history; anything older is unknowable
# through this endpoint

LOOKBACK_CAP_DAYS = 14

# The API refuses pages beyond 500 on every paginated endpoint; a day
# slice never reaches it, so hitting the cap means a truncated read

PAGE_CAP = 500


def _changed_ids(media_type, window_start, window_end):
    """(ids, complete): every TMDB id of the given type ("movie" or
    "tv") edited in the window, enumerated in one-day slices to stay
    under the page ceiling. complete is False when any slice failed or
    truncated — the caller keeps the watermark so tomorrow's sweep
    re-covers the window."""

    ids = set()
    complete = True
    day = window_start.date()
    while day <= window_end.date():
        page = 1
        total_pages = 1
        while page <= total_pages:
            try:
                r = tmdb_get(
                    current_app.config["TMDB_API_URL"] + f"/{media_type}/changes",
                    params={
                        "api_key": current_app.config["TMDB_API_KEY"],
                        "start_date": day.isoformat(),
                        "end_date": (day + timedelta(days=1)).isoformat(),
                        "page": page,
                    },
                    timeout=10,
                )
                r.raise_for_status()
                body = r.json() or {}
            except Exception:
                current_app.logger.warning(traceback.format_exc())
                complete = False
                break
            ids.update(
                item["id"] for item in body.get("results") or [] if item.get("id")
            )
            total_pages = body.get("total_pages") or 1
            if total_pages > PAGE_CAP:
                current_app.logger.warning(
                    f"TMDB changes: {media_type} slice {day} reports "
                    f"{total_pages} pages, beyond the API's {PAGE_CAP}-page "
                    f"ceiling; the read is truncated"
                )
                total_pages = PAGE_CAP
                complete = False
            page += 1
        day += timedelta(days=1)
    return ids, complete


def refresh_changed_records():
    """Nightly task: enqueue the standard TMDB refresh for every
    library record whose TMDB entry changed since the last sweep."""

    with app.app_context():
        if not current_app.config["TMDB_API_KEY"]:
            return 0
        redis = current_app.redis
        now = datetime.now(timezone.utc)

        window_start = None
        raw = redis.get(LAST_RUN_KEY)
        if raw:
            try:
                window_start = datetime.fromisoformat(raw.decode())
            except ValueError:
                pass
        if window_start is None:
            # First sweep: the endpoint's own default window
            window_start = now - timedelta(days=1)
        cap = now - timedelta(days=LOOKBACK_CAP_DAYS)
        if window_start < cap:
            current_app.logger.warning(
                f"TMDB changes: last sweep at {window_start:%Y-%m-%d} predates "
                f"TMDB's {LOOKBACK_CAP_DAYS}-day change history; edits before "
                f"{cap:%Y-%m-%d} are unknowable — run the maintenance page's "
                f"bulk TMDB refresh to catch the library up"
            )
            window_start = cap

        changed_movies, movies_complete = _changed_ids("movie", window_start, now)
        changed_tv, tv_complete = _changed_ids("tv", window_start, now)

        movies_queued = 0
        movie_rows = (
            db.session.query(Movie.id, Movie.tmdb_id, Movie.title, Movie.year)
            .filter(Movie.tmdb_id != None)
            .filter(Movie.tmdb_ignored == False)
            .order_by(Movie.title.asc())
            .all()
        )
        for movie_id, tmdb_id, title, year in movie_rows:
            if tmdb_id not in changed_movies:
                continue
            current_app.request_queue.enqueue(
                "app.videos.refresh_tmdb_info",
                args=("Movies", movie_id, tmdb_id),
                job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
                description=f"Refreshing TMDB data for '{title} ({year})'",
            )
            movies_queued += 1

        # Only the series the in-production nightly sweep leaves alone:
        # its predicate takes everything except ended/canceled rows, so
        # this one takes exactly those (a NULL in_production still
        # counts as left-alone only alongside an ended/canceled status)

        tv_queued = 0
        tv_rows = (
            db.session.query(TVSeries.id, TVSeries.tmdb_id, TVSeries.title)
            .filter(TVSeries.tmdb_id != None)
            .filter(TVSeries.tmdb_ignored == False)
            .filter(TVSeries.tmdb_status.in_(["Ended", "Canceled"]))
            .filter(
                db.or_(
                    TVSeries.tmdb_in_production == False,
                    TVSeries.tmdb_in_production == None,
                )
            )
            .order_by(TVSeries.title.asc())
            .all()
        )
        for series_id, tmdb_id, title in tv_rows:
            if tmdb_id not in changed_tv:
                continue
            current_app.request_queue.enqueue(
                "app.videos.refresh_tmdb_info",
                args=("TV Shows", series_id, tmdb_id),
                job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
                description=f"Refreshing TMDB data for '{title}'",
            )
            tv_queued += 1

        if movies_complete and tv_complete:
            redis.set(LAST_RUN_KEY, now.isoformat())
        else:
            current_app.logger.warning(
                "TMDB changes: enumeration was cut short; keeping the "
                "watermark so the next sweep re-covers the window"
            )

        current_app.logger.info(
            f"TMDB changes: {len(changed_movies)} changed movies and "
            f"{len(changed_tv)} changed series since "
            f"{window_start:%Y-%m-%d %H:%M} UTC; queued {movies_queued} "
            f"movie and {tv_queued} TV refreshes"
        )
        return movies_queued + tv_queued
