"""The refresh sweep that the TMDB change lists drive.

The TMDB details payloads carry no last-updated stamp. The change lists
(/movie/changes, /tv/changes) are the cache-invalidation feed of the
API. They are a paginated list of every id edited in a window. The
window can go back a maximum of 14 days. A nightly task enumerates the
window since the last successful sweep. It intersects the window with
the ids of the library. Then it enqueues the standard TMDB refresh for
only the records that changed. Thus, the edits arrive within a day, and
the task does not fetch thousands of titles again for no reason.

The task enumerates the window in slices of 1 day. A single query near
the lookback cap can exceed the 500-page limit of the API (movies alone
run approximately 70 pages a day). A slice of 1 day never comes near
the limit. A Redis watermark records the last completed sweep. The
watermark moves only when both enumerations completed with no error.
Thus, the next sweep covers a partial night again. In the worst case,
Fitzflix refreshes a title 2 times. That is harmless. A watermark older
than the 14-day history cannot be recovered from here. The sweep clamps
to the cap. The log then points to the bulk refresh on the maintenance
page.

The TV filter selects only ended and canceled series.
refresh_in_production_tv fetches all the other series again each night.
This sweep exists to catch the metadata edits that the nightly sweep
leaves to "rarely changes" on purpose. It does not refresh again what
the nightly sweep covers.
"""

import traceback

from datetime import datetime, timedelta, timezone

from flask import current_app
from werkzeug.local import LocalProxy

from app import db, get_app
from app.models import Movie, TVSeries, tmdb_get

# The app instance of this process. Fitzflix resolves it lazily. Thus,
# the nightly task can run on a worker without a second application.

app = LocalProxy(get_app)

# The time of the last completed sweep (ISO datetime, UTC). The window
# of the next run starts here.

LAST_RUN_KEY = "fitzflix:tmdb-changes:last-run"

# TMDB keeps 14 days of change history. This endpoint cannot show
# older changes.

LOOKBACK_CAP_DAYS = 14

# The API refuses pages after page 500 on every paginated endpoint. A
# slice of 1 day never reaches the cap. Thus, a read that touches the
# cap is a truncated read.

PAGE_CAP = 500


def _changed_ids(media_type, window_start, window_end):
    """Return (ids, complete) for the TMDB ids edited in the window.

    The type is "movie" or "tv". This function enumerates the window in
    slices of 1 day to stay under the page limit. complete is False if
    a slice failed or was truncated. Then the caller keeps the
    watermark. Thus, the sweep of the next day covers the window
    again."""

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
    """Enqueue the standard TMDB refresh for each changed library record.

    This is a nightly task. A record counts as changed if its TMDB entry
    changed after the last sweep."""

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
            # This is the first sweep. Use the default window of the endpoint.
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

        # Select only the series that the nightly in-production sweep does
        # not touch. That predicate takes all rows except the ended and
        # canceled rows. Thus, this predicate takes exactly those rows. A
        # NULL in_production counts as not touched only together with an
        # ended or canceled status.

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
