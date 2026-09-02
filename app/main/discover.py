"""Serve the discovery surfaces (the routes.py split).

The surfaces are the landing rails, the Recommendations page, and the
TMDB log page. They also include the poster popover cards (film and
series), the watchlist, and the Radarr hand-off."""

import os
import traceback

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from datetime import date, datetime, timezone

from flask import (
    abort,
    current_app,
    jsonify,
    render_template,
    flash,
    redirect,
    url_for,
    request,
)

# Flask 2.4 removed flask.Markup. Import it from its actual home.
from flask_login import current_user, login_required
from sqlalchemy.orm import contains_eager, selectinload

from app import db
from app.main.forms import (
    MovieReviewForm,
    RadarrForm,
    WatchlistForm,
)
from app.models import (
    File,
    FileAudioTrack,
    Movie,
    MovieCast,
    MovieCrew,
    TMDBCredit,
    TMDBGenre,
    TVCast,
    TVSeries,
    UserMovieReview,
    UserMovieStatus,
    UserWatchlist,
    tmdb_get,
)
from app.main import bp
from app.plex_player import remote_playback_configured
from app.main.helpers import (
    _card_fetch,
    _enqueue_profile_recompute,
    _ladder_fetch,
    _ladder_state,
    _mark_not_interested,
    _quick_rating,
    _same_day_rerate,
    _upgrade_threshold,
    library_upgradable,
    series_upgradable,
    tv_meta_line,
    _watched_timestamp,
    admin_required,
)
from app.recommendations import (
    TMDB_PATCH_SCORES_KEY,
    TOP_BILLING_CUTOFF,
    coarse_interest_score,
    estimated_rating,
    marker_bar,
    not_interested_movie_ids,
    resolved_score,
    resolved_tmdb_score,
    daily_shelf,
    shuffle_daily,
    stored_profile,
    stored_recommendations,
    stored_scores,
    watch_again_shelf,
)
from app.availability_alerts import NEW_IN_LIBRARY_LABEL, recent_availability
from app.streaming import (
    batch_title_availability,
    rental_matches,
    streaming_matches,
    user_provider_ids,
    user_streaming,
)
from app.elicitation import set_last_response
from app.rec_shelves import build_shelves, parse_criteria, replacement_film
from app.criterion_now import criterion_now_card, is_criterion_subscriber
from app.radarr_push import (
    RadarrError,
    radarr_configured,
    radarr_tmdb_ids,
    request_movie,
    withdraw_movie,
)
from app.leaving_criterion import (
    CRITERION_PROVIDER_ID,
    leaving_departure,
    leaving_inventory,
    leaving_shelf,
)
from app.newly_added import (
    FEEDS as NEWLY_ADDED_FEEDS,
    newly_added_fold,
    newly_added_inventory,
    newly_added_shelves,
    poster_fold,
)
from app.streaming_rail import ENRICHED_KEY, enriched_movie, stored_rail
from app.videos import (
    clear_not_interested,
    clear_watchlist,
    find_or_create_tmdb_movie,
    star_rating_fields,
)
from rq.registry import ScheduledJobRegistry, StartedJobRegistry

# The top watchlist shelf is bigger than the 12-card discovery shelves
# (Glenn, 2026-08-30). It holds the films that the user already wants.
# Thus, it gets 3 rows before the discovery shelves start.

WATCHLIST_SHELF_SIZE = 18


def _fits(movie, minutes):
    """Return True when the film fits the runtime filter of the evening."""

    return bool(movie.tmdb_runtime and movie.tmdb_runtime <= minutes)


def _movie_key(movie):
    """Return the page-wide claim id for a movie-backed card.

    The id is the tmdb id that the streaming-sourced shelves also use.
    Thus, the cross-shelf no-repeat set recognizes the same film
    everywhere. A local-only fallback covers the rare film that TMDB
    does not know."""

    return movie.tmdb_id if movie.tmdb_id else f"m{movie.id}"


def watchlist_shelf_rows(user):
    """Return (urgent, rows) for the top watchlist shelf of the landing page.

    The rows are the films that the user already wants and that the user
    can watch tonight. A film is watchable when it is owned locally, or
    when it streams on one of the services of the user. The answer comes
    from the availability cache that the nightly refresh keeps full. A
    film added since last night waits for the warm of tonight.

    `urgent` is the films that leave soon. These are the streaming
    departures that the fold rules already treat as the loudest badge
    of the page. They are sorted best-first by suggested rating. They
    hold the leading slots of the shelf in order. A watchlisted film
    that leaves soon is the most urgent card on the whole page.
    `rows` is everything else, sorted best-first by suggested rating
    from the shared score source. Owned films never count as leaving.
    The disc does not go anywhere."""

    user_id = int(user.id)
    entries = (
        UserWatchlist.query.filter_by(user_id=user_id)
        .join(Movie, Movie.id == UserWatchlist.movie_id)
        .options(contains_eager(UserWatchlist.movie))
        .all()
    )
    if not entries:
        return [], []
    movies = [entry.movie for entry in entries]
    owned_ids = {
        movie_id
        for (movie_id,) in db.session.query(Movie.id)
        .filter(Movie.id.in_([movie.id for movie in movies]))
        .filter(Movie.files.any(File.feature_type_id.is_(None)))
    }
    provider_ids = user_provider_ids(user)
    availability = {}
    if provider_ids:
        availability, _ = batch_title_availability(
            (
                movie.tmdb_id
                for movie in movies
                if movie.id not in owned_ids and movie.tmdb_id
            ),
            fetch_limit=0,
        )

    watchable = []
    for movie in movies:
        owned = movie.id in owned_ids
        streaming = bool(
            provider_ids
            and movie.tmdb_id
            and streaming_matches(
                availability.get(movie.tmdb_id), provider_ids, tmdb_id=movie.tmdb_id
            )
        )
        if owned or streaming:
            watchable.append((movie, owned, streaming))
    if not watchable:
        return [], []

    # The suggested ratings come from the one shared score source. The
    # stored map covers the unlogged candidates. The cut of the recompute
    # goes deeper by the watchlisted films exactly for this. The rest are
    # mostly rewatch intents, already logged. They score live one time
    # and patch into the map.

    profile = stored_profile(current_app.redis, user_id)
    scores = stored_scores(current_app.redis, user_id) if profile else {}
    rows = []
    for movie, owned, streaming in watchable:
        score = (
            resolved_score(current_app.redis, user_id, movie, profile, scores=scores)
            if profile
            else None
        )
        rows.append(
            {
                "movie": movie,
                "owned": owned,
                "leaving": (
                    leaving_departure(movie.tmdb_id)
                    if streaming and not owned
                    else None
                ),
                "score": score if score is not None else 0.0,
            }
        )
    rows.sort(key=lambda row: row["score"], reverse=True)
    return (
        [row for row in rows if row["leaving"]],
        [row for row in rows if not row["leaving"]],
    )


@bp.route("/")
@bp.route("/index")
@login_required
def index():
    """Render the landing page: what to watch tonight (GitHub #46/#61).

    Since 2026-08-30, the page opens with the watchlist shelf. That
    shelf holds the films that the user already wants and can watch
    tonight. The departures that leave soon lead it. Every shelf below
    it is pure discovery. The watchlist is excluded at the source. The
    discovery shelves are the library rail, the Criterion departures,
    the newly-added feeds, the streaming rail, and the rewatch shelf.
    The one shared daily_shelf recipe picks all of them with a
    page-wide no-repeat set.

    ?minutes=N filters every shelf at view time to the films that fit
    the evening. The computed recommendations never consider the
    length. Films with unknown runtimes hide only from filtered views.

    The shelves of the default view are frozen for the calendar day
    (#204). The first render makes a snapshot of the cards of each
    rail. Later renders replay the snapshot. Only the slot of a film
    that is no longer eligible is replaced. See frozen_shelf. The
    ?minutes= view stays a live pick. It is a transient planning lens,
    not the shelf."""

    minutes = request.args.get("minutes", type=int)
    if minutes is not None and minutes < 1:
        minutes = None
    day = date.today()
    freeze = minutes is None

    # The watchlist is a SOURCE now, not a per-shelf sort key (Glenn,
    # 2026-08-30). Its watchable films fill the top shelf. Every
    # discovery shelf below excludes the watchlisted films at the
    # source. The page reads "what you already want", then "ways to
    # discover something else if none of that appeals".

    watchlisted_ids = {
        movie_id
        for (movie_id,) in db.session.query(UserWatchlist.movie_id).filter(
            UserWatchlist.user_id == int(current_user.id)
        )
    }
    watchlisted_tmdb = set()
    if watchlisted_ids:
        watchlisted_tmdb = {
            tmdb_id
            for (tmdb_id,) in db.session.query(Movie.tmdb_id)
            .filter(Movie.id.in_(watchlisted_ids))
            .filter(Movie.tmdb_id.isnot(None))
        }

    # The top shelf always claims its films first. The discovery shelves
    # claim after it, in a day-shuffled order. Thus, no single shelf gets
    # the first pick of the shared candidates every day.

    shown = set()
    wl_urgent, wl_rows = watchlist_shelf_rows(current_user)
    # The candidates that pass every other test. Thus, an empty rail can
    # separate "nothing fits the filter" from "nothing left to recommend"
    # (#198).
    watchlist_eligible = len(wl_urgent) + len(wl_rows)
    if minutes:
        wl_urgent = [row for row in wl_urgent if _fits(row["movie"], minutes)]
        wl_rows = [row for row in wl_rows if _fits(row["movie"], minutes)]
    watchlist_items = daily_shelf(
        current_app.redis,
        current_user.id,
        "watchlist",
        wl_rows,
        shown,
        key=lambda row: _movie_key(row["movie"]),
        urgent=wl_urgent,
        day=day,
        count=WATCHLIST_SHELF_SIZE,
        freeze=freeze,
    )

    stored = stored_recommendations(current_app.redis, current_user.id)

    has_history = (
        db.session.query(UserMovieReview.id)
        .filter(UserMovieReview.user_id == int(current_user.id))
        .first()
        is not None
    )

    # The source of the library rail: the nightly ranking, minus the
    # films logged or refused since the recompute, and minus the
    # watchlist. Those films drop immediately. They do not stay until
    # tonight.

    rec_rows = []
    computed_at = None
    if stored:
        computed_at = stored.get("computed_at")
        seen = {
            movie_id
            for (movie_id,) in db.session.query(UserMovieReview.movie_id)
            .filter(UserMovieReview.user_id == int(current_user.id))
            .filter(UserMovieReview.movie_id.isnot(None))
        }
        seen |= not_interested_movie_ids(current_user.id)
        movie_ids = [item["movie_id"] for item in stored.get("items", [])]
        movies = {
            movie.id: movie
            for movie in Movie.query.filter(Movie.id.in_(movie_ids or [0]))
        }
        for item in stored.get("items", []):
            movie = movies.get(item["movie_id"])
            if movie is None or item["movie_id"] in seen:
                continue
            if item["movie_id"] in watchlisted_ids:
                continue
            rec_rows.append({"movie": movie, "because": item.get("because", [])[:3]})
    elif has_history:
        # There are diary rows but nothing stored yet (the first deploy,
        # or a new reviewer). Compute one time now instead of a wait for
        # tonight. The marker prevents a new enqueue on each page load.

        if current_app.redis.set(
            f"fitzflix:recs:requested:{int(current_user.id)}", "1", nx=True, ex=3600
        ):
            current_app.maintenance_queue.enqueue(
                "app.recommendations.recompute_recommendations",
                job_timeout="1h",
                description="Computing film recommendations",
            )

    # The source of the rewatch shelf: owned films that the user liked
    # and whose last watch is long past. Old favorites have no other
    # surface, because the candidates of the engine exclude logged
    # films. Films added again (a declared rewatch intent) live on the
    # watchlist shelf now.

    again_rows = []
    again_ranked = watch_again_shelf(current_user.id)
    if again_ranked:
        again_movies = {
            m.id: m
            for m in Movie.query.filter(
                Movie.id.in_([item["movie_id"] for item in again_ranked])
            )
        }
        for item in again_ranked:
            again_movie = again_movies.get(item["movie_id"])
            if again_movie is None or item["movie_id"] in watchlisted_ids:
                continue
            again_rows.append(
                {"movie": again_movie, "last_watched": item["last_watched"]}
            )

    # The source of the streaming rail: films that stream on the
    # services of this user, from the nightly discover-pool recompute.
    # Films logged or acquired since the run drop out immediately. A user
    # with a profile and provider picks but no stored rail gets one
    # compute enqueued.

    rail_rows = []
    rail_computed_at = None
    rail_payload = stored_rail(current_app.redis, current_user.id)
    if rail_payload:
        rail_computed_at = rail_payload.get("computed_at")
        rail_ids = [item["tmdb_id"] for item in rail_payload.get("items", [])]
        dropped = set()
        if rail_ids:
            owned_now = db.session.query(Movie.tmdb_id).filter(
                Movie.tmdb_id.in_(rail_ids),
                Movie.files.any(File.feature_type_id.is_(None)),
            )
            logged_now = (
                db.session.query(Movie.tmdb_id)
                .join(UserMovieReview, UserMovieReview.movie_id == Movie.id)
                .filter(Movie.tmdb_id.in_(rail_ids))
                .filter(UserMovieReview.user_id == int(current_user.id))
            )
            refused_now = (
                db.session.query(Movie.tmdb_id)
                .join(UserMovieStatus, UserMovieStatus.movie_id == Movie.id)
                .filter(Movie.tmdb_id.in_(rail_ids))
                .filter(UserMovieStatus.user_id == int(current_user.id))
                .filter(UserMovieStatus.kind == "not_interested")
            )
            dropped = (
                {t for (t,) in owned_now}
                | {t for (t,) in logged_now}
                | {t for (t,) in refused_now}
            )
        rail_rows = [
            item
            for item in rail_payload.get("items", [])
            if item["tmdb_id"] not in dropped
            and item["tmdb_id"] not in watchlisted_tmdb
        ]
    elif user_provider_ids(current_user) and stored_profile(
        current_app.redis, current_user.id
    ):
        if current_app.redis.set(
            f"fitzflix:rail:requested:{int(current_user.id)}", "1", nx=True, ex=3600
        ):
            current_app.maintenance_queue.enqueue(
                "app.streaming_rail.recompute_streaming_rail",
                job_timeout="1h",
                description="Computing the streaming rail",
            )

    # The source of the departure shelf: the films that leave the
    # Criterion Channel at the end of the month, taste-ranked, for
    # Criterion subscribers. Watchlisted departures live on the
    # watchlist shelf and lead it.

    shelf = leaving_shelf(current_user)
    leaving_rows = shelf["items"] if shelf else []
    shelf_departs = shelf["departs"].strftime("%B %-d") if shelf else None
    shelf_url = shelf["url"] if shelf else None

    # The sources of the newly-added discovery shelves (#246): recent
    # arrivals on the own newly-added feed of a subscribed provider. The
    # code is generic over providers. Today, only the Criterion Channel
    # feeds a shelf. The availability-alert email already covers the
    # watchlisted arrivals. These shelves are for films that the database
    # has never seen.

    newly_feeds = newly_added_shelves(current_user)

    # Every discovery shelf runs the one shared recipe (daily_shelf).
    # Taste-ranked rows walk the no-repeat quality tiers into day-stable
    # random slots. The slots are frozen for the day (#204). A shelf
    # never repeats a film that a different shelf already claimed. The
    # claim order shuffles daily. Thus, the candidates that the shelves
    # share spread around. They do not always go to the shelf that
    # picked first. The render order of the page stays fixed.

    def movie_fits(row):
        """Apply the runtime filter to a movie-backed row."""

        return _fits(row["movie"], minutes)

    def payload_fits(item):
        """Apply the runtime filter to a stored-payload row."""

        return bool(item.get("runtime") and item["runtime"] <= minutes)

    def movie_row_key(row):
        """Return the page-wide claim id of a movie-backed row."""

        return _movie_key(row["movie"])

    def payload_key(item):
        """Return the page-wide claim id of a stored-payload row."""

        return item["tmdb_id"]

    specs = {
        "recs": (rec_rows, movie_row_key, movie_fits),
        "again": (again_rows, movie_row_key, movie_fits),
        "rail": (rail_rows, payload_key, payload_fits),
        "leaving": (leaving_rows, payload_key, payload_fits),
    }
    for feed in newly_feeds:
        specs[f"newly:{feed['provider_id']}"] = (
            feed["items"],
            payload_key,
            payload_fits,
        )

    eligible_counts = {name: len(rows) for name, (rows, _, _) in specs.items()}
    picked = {}
    for name in shuffle_daily(
        sorted(specs), f"order:{int(current_user.id)}:{day.isoformat()}"
    ):
        rows, row_key, fits = specs[name]
        if not rows:
            picked[name] = []
            continue
        if minutes:
            rows = [row for row in rows if fits(row)]
        picked[name] = daily_shelf(
            current_app.redis,
            current_user.id,
            name,
            rows,
            shown,
            key=row_key,
            day=day,
            freeze=freeze,
        )

    recs = picked["recs"]
    again_items = picked["again"]
    rail = picked["rail"]
    shelf_items = picked["leaving"]

    # A rail that the runtime filter emptied says so. It does not
    # disappear. Silence reads as "there is nothing here" (#198). Each
    # flag means that the rail had films and the filter removed them all.

    def filtered_out(shown_cards, eligible):
        """Return True when a rail had films and the runtime filter
        removed every one of them."""

        return bool(minutes) and not shown_cards and eligible > 0

    new_shelves = []
    for feed in newly_feeds:
        films = picked[f"newly:{feed['provider_id']}"]
        new_shelves.append(
            {
                "provider_id": feed["provider_id"],
                "label": feed["label"],
                "source": feed["source"],
                # Use "films", not "items". In Jinja, the .items of a dict
                # is the method, not the key.
                "films": films,
                "filtered_out": filtered_out(
                    films, eligible_counts[f"newly:{feed['provider_id']}"]
                ),
            }
        )

    return render_template(
        "index.html",
        title="Home",
        watchlist_items=watchlist_items,
        watchlist_filtered_out=filtered_out(watchlist_items, watchlist_eligible),
        recs=recs,
        computed_at=computed_at,
        has_history=has_history,
        recs_stored=bool(stored),
        recs_filtered_out=filtered_out(recs, eligible_counts["recs"]),
        again_filtered_out=filtered_out(again_items, eligible_counts["again"]),
        rail_filtered_out=filtered_out(rail, eligible_counts["rail"]),
        shelf_filtered_out=filtered_out(shelf_items, eligible_counts["leaving"]),
        again=again_items,
        rail=rail,
        rail_computed_at=rail_computed_at,
        shelf=shelf_items,
        shelf_departs=shelf_departs,
        shelf_url=shelf_url,
        new_shelves=new_shelves,
        now_playing=criterion_now_card(current_user),
        criterion_subscriber=is_criterion_subscriber(current_user),
        review_form=MovieReviewForm(),
        minutes=minutes,
    )


@bp.route("/criterion-now")
@login_required
def criterion_now():
    """Render the Criterion24/7 card fragment.

    The home page fetches it again every minute while the page is
    visible. Thus, an open tab follows the feed. The film changes when
    the poller stores the next one. The "minutes in" and "next film"
    line keeps time between the polls. The response is empty (200) when
    there is nothing to show: the user is not a subscriber, there is no
    stored film, or the film is stale. Then the container on the page
    empties and the card disappears, the same as after a reload."""

    return render_template(
        "_criterion_now_card.html",
        now_playing=criterion_now_card(current_user),
        review_form=MovieReviewForm(),
    )


@bp.route("/file-activity")
@login_required
def file_activity():
    """Render the File Activity dashboard.

    This page merges the old Recently Added page and the Pipeline
    Activity trails (Glenn, 2026-08). A file that arrived renders as a
    full card here. The queue poll of base.html fills the trail chips of
    each card. A file still in flight shows as chips on the job rows of
    the queue page. When the cataloging completes, the poll adds the card
    of the file here through /file-activity/card."""

    page = request.args.get("page", 1, type=int)

    # Show only the files added or updated in the last 7 days. This is
    # the recency horizon of the page. It matches
    # pipeline.TRAIL_TTL_SECONDS. Thus, a card keeps its trail chips
    # while it stays on the page. This window once meant "still in S3
    # Standard, downloadable again without a Glacier thaw". But the live
    # lifecycle rule has moved untouched/ to Deep Archive at 0 days for
    # some time (verified against the bucket, 2026-08). Thus, the files
    # here are usually already frozen after their first day.

    # The cards read the tracks, film, and series of each file. The
    # selectin loads fetch those per page instead of per file (291
    # queries for 100 cards before 2026-08).

    recently_added = (
        File.query.outerjoin(FileAudioTrack, (FileAudioTrack.file_id == File.id))
        .distinct()  # .distinct() makes the result numbers per page correct
        .outerjoin(Movie, (Movie.id == File.movie_id))
        .outerjoin(TVSeries, (TVSeries.id == File.series_id))
        .options(
            selectinload(File.audiotrack),
            selectinload(File.subtrack),
            selectinload(File.movie),
            selectinload(File.tv_series),
        )
        .filter(
            db.func.coalesce(File.date_updated, File.date_added)
            >= db.func.adddate(db.func.current_date(), -7)
        )
        .order_by(db.func.coalesce(File.date_updated, File.date_added).desc())
        .paginate(page=page, per_page=100, error_out=False)
    )

    next_url = (
        url_for("main.file_activity", page=recently_added.next_num)
        if recently_added.has_next
        else None
    )
    prev_url = (
        url_for("main.file_activity", page=recently_added.prev_num)
        if recently_added.has_prev
        else None
    )

    # The import pipeline activity comes from the live state: the running
    # and queued imports, the deferred retries, and the contents of the
    # rejects folder.

    import_activity = None
    if page == 1:
        active = []
        started_ids = StartedJobRegistry(
            "fitzflix-import", connection=current_app.redis
        ).get_job_ids()
        for job_id in started_ids + list(current_app.import_queue.job_ids):
            job = current_app.import_queue.fetch_job(job_id)
            if job is None or job.func_name != "app.videos.localization_task":
                continue
            active.append(
                {
                    "description": job.meta.get("description")
                    or job.description
                    or job_id,
                    "status": "running" if job_id in started_ids else "queued",
                }
            )

        deferred = []
        registry = ScheduledJobRegistry(queue=current_app.import_queue)
        for job_id in registry.get_job_ids():
            job = current_app.import_queue.fetch_job(job_id)
            if job is not None and job.func_name == "app.videos.localization_task":
                deferred.append(
                    {
                        "description": job.description or job.id,
                        "next_run": registry.get_scheduled_time(job_id),
                    }
                )
        deferred.sort(key=lambda entry: entry["next_run"])

        # A file can collect several scheduled retries. Show each file one
        # time, with its soonest retry time.

        unique_deferred = []
        seen_descriptions = set()
        for entry in deferred:
            if entry["description"] not in seen_descriptions:
                seen_descriptions.add(entry["description"])
                unique_deferred.append(entry)
        deferred = unique_deferred

        rejects = []
        rejects_dir = current_app.config["REJECTS_DIR"]
        if os.path.isdir(rejects_dir):
            for entry in os.scandir(rejects_dir):
                if entry.is_file() and not entry.name.startswith("."):
                    rejects.append(
                        {
                            "basename": entry.name,
                            "reason": "",
                            "when": entry.stat().st_ctime,
                        }
                    )
                elif entry.is_dir():
                    for file_entry in os.scandir(entry.path):
                        if file_entry.is_file() and not file_entry.name.startswith("."):
                            rejects.append(
                                {
                                    "basename": file_entry.name,
                                    "reason": entry.name,
                                    "when": file_entry.stat().st_ctime,
                                }
                            )
        rejects.sort(key=lambda entry: entry["when"], reverse=True)
        reject_count = len(rejects)
        rejects = rejects[:10]
        for entry in rejects:
            entry["when"] = datetime.fromtimestamp(entry["when"], tz=timezone.utc)

        if active or deferred or rejects:
            import_activity = {
                "active": active,
                "deferred": deferred,
                "rejects": rejects,
                "reject_count": reject_count,
            }

    return render_template(
        "file_activity.html",
        title="File Activity",
        recently_added=recently_added.items,
        native_language=[current_app.config["NATIVE_LANGUAGE"], "und", "zxx"],
        import_activity=import_activity,
        next_url=next_url,
        prev_url=prev_url,
        pages=recently_added,
        upgrade_threshold=_upgrade_threshold(),
    )


def _file_for_trail_basename(basename):
    """Return the File row that a pipeline trail belongs to, or None.

    The basename of a trail can be the original import filename (the
    localization stages). It can be that name with its extension changed
    to .mkv (the container conversion). It can be the own basename of
    the File row (every file_id-keyed stage). Thus, first match exactly
    against both stored names. Then fall back to the stem, newest file
    first.
    """

    recency = db.func.coalesce(File.date_updated, File.date_added).desc()
    file = (
        File.query.filter(
            db.or_(File.basename == basename, File.untouched_basename == basename)
        )
        .order_by(recency)
        .first()
    )
    if file is not None:
        return file
    stem = basename.rsplit(".", 1)[0]
    if not stem:
        return None
    pattern = stem.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + ".%"
    return (
        File.query.filter(
            db.or_(
                File.basename.like(pattern, escape="\\"),
                File.untouched_basename.like(pattern, escape="\\"),
            )
        )
        .order_by(recency)
        .first()
    )


@bp.route("/file-activity/card")
@login_required
def file_activity_card():
    """Render the card fragment of one file for the File Activity dashboard.

    The queue poll fetches it when a trail with no card completes its
    cataloging. The chips become the full card (poster, quality badge,
    tracks, links) without a reload. The key is the basename of the
    trail. A 404 means that the File row is not visible yet. Then the
    poll tries again."""

    basename = request.args.get("basename", "", type=str)
    if not basename:
        abort(404)
    file = _file_for_trail_basename(basename)
    if file is None:
        abort(404)
    return render_template(
        "_file_activity_card.html",
        file=file,
        native_language=[current_app.config["NATIVE_LANGUAGE"], "und", "zxx"],
        upgrade_threshold=_upgrade_threshold(),
    )


@bp.route("/movie_card")
@login_required
def movie_card():
    """Render the card fragment of the poster popover (#45c).

    The card shows the title, the credits, the meta line, the synopsis,
    the availability, and the at-a-glance badges of one film. The meta
    line has the runtime, the genres, and the US rating. Thus, the card
    says what sort of film it is. The key is movie_id for library
    records, or tmdb_id for films with no local row (the streaming rail
    and the leaving shelf). The card is only informational since the
    revision of Glenn in 2026-08. The star ladder and the watchlist
    toggle live on the gallery tiles. Thus, the card carries the
    In-library badge in its shopping colors (amber = worth an upgrade,
    green = settled) and the watchlist badge instead. ?context=criterion
    (#77a) recolors that badge with the settled rule of the Criterion
    page instead of the generic shopping answer. That rule is green only
    when the disc is owned AND the copy matches the format of the
    release. The browser fetches the card only when the user hovers or
    taps a poster. Thus, the gallery pages stay light."""

    movie_id = request.args.get("movie_id", type=int)
    tmdb_id = request.args.get("tmdb_id", type=int)
    movie = None
    if movie_id:
        movie = db.session.get(Movie, movie_id)
        if movie is None:
            abort(404)
    elif tmdb_id:
        movie = Movie.query.filter_by(tmdb_id=tmdb_id).first()
    else:
        abort(404)

    if movie is not None:
        directors = list(
            db.session.query(TMDBCredit.id, TMDBCredit.name)
            .join(MovieCrew, MovieCrew.credit_id == TMDBCredit.id)
            .filter(MovieCrew.movie_id == movie.id)
            .filter(MovieCrew.job == "Director")
            .distinct()
        )
        top_cast = list(
            db.session.query(TMDBCredit.id, TMDBCredit.name)
            .join(MovieCast, MovieCast.credit_id == TMDBCredit.id)
            .filter(MovieCast.movie_id == movie.id)
            .order_by(MovieCast.billing_order.asc())
            .limit(TOP_BILLING_CUTOFF)
        )
        on_watchlist = (
            UserWatchlist.query.filter_by(
                user_id=int(current_user.id), movie_id=movie.id
            ).first()
            is not None
        )

        # The In-library badge shows the answer of the shopping list (the
        # revision of Glenn in 2026-08). That revision replaced the
        # quality-tier badge. The badge is amber when the copy is worth an
        # upgrade. It is green when the copy is settled. ?context=criterion
        # uses the rule of the Criterion catalog instead.

        upgradable = library_upgradable(
            movie, criterion=request.args.get("context") == "criterion"
        )
        in_library = upgradable is not None
        return render_template(
            "_movie_card.html",
            display_title=(
                f"{movie.tmdb_title if movie.tmdb_title else movie.title} "
                f"({movie.tmdb_release_date.strftime('%Y') if movie.tmdb_title and movie.tmdb_release_date else movie.year})"
            ),
            href=url_for("main.movie", movie_id=movie.id),
            runtime=movie.tmdb_runtime,
            genres=[genre.name for genre in movie.genres],
            certification=next(
                (
                    c.certification
                    for c in movie.certifications
                    if c.country == "US" and c.certification
                ),
                None,
            ),
            overview=movie.tmdb_overview,
            directors=directors,
            top_cast=top_cast,
            on_watchlist=on_watchlist,
            in_library=in_library,
            library_upgradable=upgradable,
            play_url=(
                url_for("main.movie_play", movie_id=movie.id)
                if in_library
                and (
                    (
                        current_user.plex_player_configured
                        and remote_playback_configured()
                    )
                    or (current_user.infuse_player_configured and movie.tmdb_id)
                )
                else None
            ),
            streaming=(
                user_streaming(
                    movie.tmdb_id,
                    current_user,
                    negative=not in_library,
                    local=in_library,
                    upgradable=upgradable,
                )
                if movie.tmdb_id
                else None
            ),
        )

    # There is no local record. The card renders from TMDB directly.

    if not current_app.config["TMDB_API_KEY"]:
        abort(404)
    try:
        r = tmdb_get(
            current_app.config["TMDB_API_URL"] + "/movie/" + str(tmdb_id),
            params={
                "api_key": current_app.config["TMDB_API_KEY"],
                "append_to_response": "credits,release_dates",
            },
            timeout=10,
        )
        r.raise_for_status()
    except Exception:
        current_app.logger.warning(traceback.format_exc())
        abort(503)

    details = r.json()
    film_title = details.get("title")
    release_year = (details.get("release_date") or "")[:4]
    if not film_title or not release_year.isdigit():
        abort(404)

    directors = [
        (person.get("id"), person.get("name"))
        for person in (details.get("credits") or {}).get("crew") or []
        if person.get("job") == "Director" and person.get("id") is not None
    ]
    billed_cast = sorted(
        (details.get("credits") or {}).get("cast") or [],
        key=lambda person: person.get("order", 99),
    )
    top_cast = [
        (person.get("id"), person.get("name"))
        for person in billed_cast[:TOP_BILLING_CUTOFF]
        if person.get("id") is not None
    ]

    # The US rating. The first certified release wins. This is the same
    # answer that the cataloger stores for library records.
    certification = next(
        (
            date.get("certification")
            for country in (details.get("release_dates") or {}).get("results") or []
            if country.get("iso_3166_1") == "US"
            for date in country.get("release_dates") or []
            if date.get("certification")
        ),
        None,
    )

    return render_template(
        "_movie_card.html",
        display_title=f"{film_title} ({release_year})",
        href=url_for("main.review_tmdb", tmdb_id=tmdb_id),
        runtime=details.get("runtime"),
        genres=[
            genre.get("name")
            for genre in details.get("genres") or []
            if genre.get("name")
        ],
        certification=certification,
        overview=details.get("overview"),
        directors=directors,
        top_cast=top_cast,
        on_watchlist=False,
        in_library=False,
        library_upgradable=None,
        streaming=user_streaming(tmdb_id, current_user, negative=True),
    )


@bp.route("/tv_card")
@login_required
def tv_card():
    """Render the card fragment of the poster popover for one TV series.

    This is the shape of the film card in TV terms. It shows the title,
    the meta line of the run, the synopsis, and the billed cast. It
    also shows the In-library badge in its shopping colors, and how
    much of the run is on the shelf. The key is series_id for library
    records, or tmdb_id
    for a series with no local row (the unowned television credits of a
    person). The browser fetches the card only when the user hovers or
    taps a poster. Thus, the TV Library and filmography pages stay
    light.

    The card has no streaming strip, no watchlist badge, and no play
    button. The watch providers, the watchlist, and the Apple TV
    hand-off are all film-keyed here. Thus, the card carries only what
    it can answer."""

    series_id = request.args.get("series_id", type=int)
    tmdb_id = request.args.get("tmdb_id", type=int)
    series = None
    if series_id:
        series = db.session.get(TVSeries, series_id)
        if series is None:
            abort(404)
    elif tmdb_id:
        series = TVSeries.query.filter_by(tmdb_id=tmdb_id).first()
    else:
        abort(404)

    if series is not None:
        top_cast = list(
            db.session.query(TMDBCredit.id, TMDBCredit.name)
            .join(TVCast, TVCast.credit_id == TMDBCredit.id)
            .filter(TVCast.tv_id == series.id)
            .order_by(TVCast.billing_order.asc(), TVCast.episode_count.desc())
            .limit(TOP_BILLING_CUTOFF)
        )

        # The contents of the shelf, counted the way the TV Library page
        # counts them: the seasons that have files, and the distinct
        # episode numbers in them. That page calls season 0 Specials. It
        # does not count it as a season. The card says it the same way.
        # Otherwise, a show with specials reads as if it owns more
        # seasons than TMDB says exist. A series record with no files is
        # a leftover, and it badges nothing.

        season_counts = (
            db.session.query(File.season, db.func.count(db.func.distinct(File.episode)))
            .filter(File.series_id == series.id)
            .group_by(File.season)
            .all()
        )
        owned_seasons = sum(1 for season, _ in season_counts if season)
        return render_template(
            "_tv_card.html",
            # No year is appended. Every other TV surface titles a series
            # by its TMDB name only. The meta line opens with the run of
            # years anyway.
            display_title=series.tmdb_name if series.tmdb_name else series.title,
            href=url_for("main.tv", series_id=series.id),
            meta_line=tv_meta_line(
                (
                    series.tmdb_first_air_date.year
                    if series.tmdb_first_air_date
                    else None
                ),
                series.tmdb_last_air_date.year if series.tmdb_last_air_date else None,
                series.tmdb_number_of_seasons,
                series.tmdb_number_of_episodes,
                [genre.name for genre in series.genres],
            ),
            content_rating=series.tmdb_content_rating,
            overview=series.tmdb_overview,
            top_cast=top_cast,
            in_library=bool(season_counts),
            upgradable=series_upgradable([series.id]).get(series.id, False),
            owned_seasons=owned_seasons,
            owned_specials=any(not season for season, _ in season_counts),
            owned_episodes=sum(count for _, count in season_counts),
        )

    # There is no local record. The card renders from TMDB directly.

    if not current_app.config["TMDB_API_KEY"]:
        abort(404)
    try:
        r = tmdb_get(
            current_app.config["TMDB_API_URL"] + "/tv/" + str(tmdb_id),
            params={
                "api_key": current_app.config["TMDB_API_KEY"],
                "append_to_response": "aggregate_credits,content_ratings",
            },
            timeout=10,
        )
        r.raise_for_status()
    except Exception:
        current_app.logger.warning(traceback.format_exc())
        abort(503)

    details = r.json()
    series_name = details.get("name")
    if not series_name:
        abort(404)
    first_year = (details.get("first_air_date") or "")[:4]
    last_year = (details.get("last_air_date") or "")[:4]

    # aggregate_credits bills the whole run. This matches the TVCast rows
    # that a local record would have.

    billed_cast = sorted(
        (details.get("aggregate_credits") or {}).get("cast") or [],
        key=lambda person: person.get("order", 99),
    )
    top_cast = [
        (person.get("id"), person.get("name"))
        for person in billed_cast[:TOP_BILLING_CUTOFF]
        if person.get("id") is not None
    ]

    return render_template(
        "_tv_card.html",
        display_title=series_name,
        href=None,
        meta_line=tv_meta_line(
            int(first_year) if first_year.isdigit() else None,
            int(last_year) if last_year.isdigit() else None,
            details.get("number_of_seasons"),
            details.get("number_of_episodes"),
            [genre.get("name") for genre in details.get("genres") or []],
        ),
        # The US content rating. This is the same answer that tmdb_tv_apply stores.
        content_rating=next(
            (
                country.get("rating")
                for country in (details.get("content_ratings") or {}).get("results")
                or []
                if country.get("iso_3166_1") == "US" and country.get("rating")
            ),
            None,
        ),
        overview=details.get("overview"),
        top_cast=top_cast,
        in_library=False,
        upgradable=None,
        owned_seasons=0,
        owned_specials=False,
        owned_episodes=0,
    )


# One request can carry at most this many films. Every gallery page
# shows a bounded set (rails of 12, paginated walls). Thus, the cap only
# guards against abuse.
MOVIE_STATES_LIMIT = 300

# How many enriched payloads one state batch can FETCH from TMDB for
# the tmdb-keyed lane. The size covers a whole filmography page in one
# hydration pass. The fetches run 10 at a time. Thus, approximately 100
# fetches complete in 2 seconds. The cap also bounds the burst that one
# request can aim at TMDB. Cached payloads and overlay hits cost
# nothing. They are never capped.

MOVIE_STATES_TMDB_FETCHES = 100


@bp.route("/movie_states")
@login_required
def movie_states():
    """Return the ladder and watchlist state for a batch of poster tiles.

    This is #45c, 2026-08 revision. One fetch per gallery page hydrates
    the star row and the watchlist toggle of every tile. Thus, no
    gallery route has to compute the per-film verdicts itself.
    ?movie_ids= and ?tmdb_ids= are comma-separated. Fitzflix answers
    tmdb ids under their own key. It maps them through a local record
    when one exists. Otherwise, it returns an empty state. The estimates
    come from the shared score source: the stored map. A bounded number
    of missing films score live through the resolver that patches the
    map. Every other estimate surface reads the same source."""

    def parse_ids(name):
        return [
            int(part)
            for part in (request.args.get(name) or "").split(",")
            if part.strip().isdigit()
        ]

    movie_ids = parse_ids("movie_ids")
    tmdb_ids = parse_ids("tmdb_ids")
    if len(movie_ids) + len(tmdb_ids) > MOVIE_STATES_LIMIT:
        abort(400)

    tmdb_to_movie = {}
    if tmdb_ids:
        tmdb_to_movie = {
            tmdb_id: movie_id
            for movie_id, tmdb_id in db.session.query(Movie.id, Movie.tmdb_id).filter(
                Movie.tmdb_id.in_(tmdb_ids)
            )
        }
    all_ids = set(movie_ids) | set(tmdb_to_movie.values())

    # The newest verdict per film. This is the row that the movie page
    # displays. One query gathers the rows, and this code reduces them
    # (newest review first, bare watches last, id breaks ties, the same
    # as _latest_review_row).

    latest = {}
    if all_ids:
        rows = (
            UserMovieReview.query.filter(
                UserMovieReview.user_id == int(current_user.id)
            )
            .filter(UserMovieReview.movie_id.in_(all_ids))
            .order_by(
                UserMovieReview.movie_id,
                UserMovieReview.date_reviewed.desc(),
                UserMovieReview.id.desc(),
            )
            .all()
        )
        for row in rows:
            latest.setdefault(row.movie_id, row)

    flagged_ids = set()
    listed_ids = set()
    if all_ids:
        flagged_ids = {
            movie_id
            for (movie_id,) in db.session.query(UserMovieStatus.movie_id)
            .filter(UserMovieStatus.user_id == int(current_user.id))
            .filter(UserMovieStatus.kind == "not_interested")
            .filter(UserMovieStatus.movie_id.in_(all_ids))
        }
        listed_ids = {
            movie_id
            for (movie_id,) in db.session.query(UserWatchlist.movie_id)
            .filter(UserWatchlist.user_id == int(current_user.id))
            .filter(UserWatchlist.movie_id.in_(all_ids))
        }

    # Poster folds (#197, plus the badge of #156/#230 and the feeds of
    # #246): the per-user corner overlays that every gallery tile paints
    # through this one batched fetch. The fold is green for a watchlist
    # film that recently became available (the record of the nightly
    # alert diff). It is also green for a film that recently arrived on
    # the newly-added feed of a subscribed provider. The fold is red for
    # a film in the leaving-Criterion set, for subscribers only. The
    # client paints at most one fold. Red outranks green. Ownership
    # overrides it all (Glenn, 2026-08-27). The copy of an owned film
    # does not go anywhere. Thus, it never shows the red fold. Its only
    # green fold is the recent arrival of the local file itself.
    # Movie-keyed tiles need their tmdb ids for both lookups. One query
    # covers them. Each set parses one time per request on flask.g.

    recent = recent_availability(current_user)
    provider_ids = set(user_provider_ids(current_user))
    criterion_member = CRITERION_PROVIDER_ID in provider_ids
    fold_feeds = sorted(set(NEWLY_ADDED_FEEDS) & provider_ids)
    movie_tmdb = {}
    owned_fold_ids = set()
    if all_ids:
        owned_fold_ids = {
            movie_id
            for (movie_id,) in db.session.query(Movie.id)
            .filter(Movie.id.in_(all_ids))
            .filter(Movie.files.any(File.feature_type_id.is_(None)))
        }
    if all_ids and (criterion_member or fold_feeds):
        movie_tmdb = dict(
            db.session.query(Movie.id, Movie.tmdb_id).filter(Movie.id.in_(all_ids))
        )

    profile = stored_profile(current_app.redis, current_user.id)
    scores = stored_scores(current_app.redis, current_user.id)

    # The estimate-eligible films that the map does not cover are the
    # records created since the last nightly recompute. They score live
    # through the shared resolver. Its patch makes the number permanent
    # for every other surface. The request order decides which films
    # make the cap.

    if profile:
        ordered_ids = list(
            dict.fromkeys(
                movie_ids
                + [
                    tmdb_to_movie[tmdb_id]
                    for tmdb_id in tmdb_ids
                    if tmdb_id in tmdb_to_movie
                ]
            )
        )
        # Every miss on the page scores in this one pass. The work is
        # local queries only. Thus, even a full 300-id batch stays quick.
        # The patches of the resolver make the next request free.

        misses = [
            movie_id
            for movie_id in ordered_ids
            if movie_id not in scores
            and (movie_id not in latest or latest[movie_id].rating is None)
            and movie_id not in flagged_ids
        ]
        for movie in Movie.query.filter(Movie.id.in_(misses or [0])):
            score = resolved_score(
                current_app.redis, current_user.id, movie, profile, scores=scores
            )
            if score is not None:
                scores[movie.id] = score

    # The tmdb-keyed lane: ids with no record at all (most of a
    # filmography page) still get an estimate. The estimate comes from
    # the overlay when the overlay holds them. Otherwise, they score
    # live from the enriched payloads. Missing payloads warm in PARALLEL
    # first (the enrichment pattern of the rail). Thus, one hydration
    # pass covers a whole career page in 2 seconds, instead of 20 films
    # per reload. The fetch cap bounds the burst that a single request
    # can aim at TMDB. The page itself never waits. The hydration is an
    # async fetch after the render.

    tmdb_estimates = {}
    unmatched = [tmdb_id for tmdb_id in tmdb_ids if tmdb_id not in tmdb_to_movie]
    if unmatched and profile:
        overlay = {
            int(tmdb_id): float(score)
            for tmdb_id, score in current_app.redis.hgetall(
                TMDB_PATCH_SCORES_KEY.format(user_id=int(current_user.id))
            ).items()
        }
        misses = [tmdb_id for tmdb_id in unmatched if tmdb_id not in overlay]
        scoreable = set()
        if misses:
            cached = {
                tmdb_id
                for tmdb_id, payload in zip(
                    misses,
                    current_app.redis.mget(
                        [ENRICHED_KEY.format(tmdb_id=tmdb_id) for tmdb_id in misses]
                    ),
                )
                if payload
            }
            to_fetch = [tmdb_id for tmdb_id in misses if tmdb_id not in cached][
                :MOVIE_STATES_TMDB_FETCHES
            ]
            if to_fetch and current_app.config["TMDB_API_KEY"]:
                flask_app = current_app._get_current_object()

                def warm(tmdb_id):
                    with flask_app.app_context():
                        enriched_movie(tmdb_id)

                with ThreadPoolExecutor(max_workers=10) as executor:
                    list(executor.map(warm, to_fetch))
            scoreable = cached | set(to_fetch)
        for tmdb_id in unmatched:
            score = overlay.get(tmdb_id)
            if score is None and tmdb_id in scoreable:
                score = resolved_tmdb_score(
                    current_app.redis, current_user.id, tmdb_id, profile
                )
            if score is not None:
                tmdb_estimates[tmdb_id] = estimated_rating(profile, score)

    def state_for(movie_id):
        if movie_id is None:
            return {
                "rating": None,
                "has_review": False,
                "flagged": False,
                "estimated": None,
                "on_watchlist": False,
                "fold_new": None,
                "fold_leaving": None,
            }
        row = latest.get(movie_id)
        flagged = movie_id in flagged_ids
        estimated = None
        # The estimate shows until the own stars of the user exist. A
        # bare watch (a Plex viewing, an unrated import) still shows it.
        if (row is None or row.rating is None) and not flagged:
            score = scores.get(movie_id)
            if score is not None:
                estimated = estimated_rating(profile, score)
        recent_label = (recent.get(movie_id) or {}).get("label")
        if movie_id in owned_fold_ids:
            # Ownership gates the folds. Only the arrival of the local
            # file itself goes green. The red fold never paints.
            fold_new = recent_label if recent_label == NEW_IN_LIBRARY_LABEL else None
            fold_leaving = None
        else:
            fold_new = recent_label or newly_added_fold(
                movie_tmdb.get(movie_id), fold_feeds
            )
            fold_leaving = (
                leaving_departure(movie_tmdb.get(movie_id))
                if criterion_member
                else None
            )
        return {
            "rating": (
                float(row.rating)
                if row is not None and row.rating is not None
                else None
            ),
            "has_review": row is not None,
            "flagged": flagged,
            "estimated": estimated,
            "on_watchlist": movie_id in listed_ids,
            "fold_new": fold_new,
            "fold_leaving": fold_leaving,
        }

    def tmdb_state_for(tmdb_id):
        state = state_for(tmdb_to_movie.get(tmdb_id))
        if state["estimated"] is None:
            state["estimated"] = tmdb_estimates.get(tmdb_id)
        if tmdb_to_movie.get(tmdb_id) in owned_fold_ids:
            return state
        if criterion_member and state["fold_leaving"] is None:
            state["fold_leaving"] = leaving_departure(tmdb_id)
        if state["fold_new"] is None:
            state["fold_new"] = newly_added_fold(tmdb_id, fold_feeds)
        return state

    return jsonify(
        {
            "movies": {str(movie_id): state_for(movie_id) for movie_id in movie_ids},
            "tmdb": {str(tmdb_id): tmdb_state_for(tmdb_id) for tmdb_id in tmdb_ids},
        }
    )


@bp.route("/leaving")
@login_required
def leaving():
    """Render the complete leaving-Criterion departure inventory.

    This is every film on the leaving page of the month, with the owned
    and seen films included."""

    return render_template(
        "leaving.html",
        title="Leaving the Criterion Channel",
        inventory=leaving_inventory(current_user),
    )


@bp.route("/newly-added")
@login_required
def newly_added():
    """Render the complete newly-added inventory (#246).

    This is every recent arrival on the feed of every provider, with the
    owned and seen films included. It is the "See more…" destination of
    the shelf. Each section links to the own page of the provider."""

    return render_template(
        "newly_added.html",
        title="Newly added to streaming",
        sections=newly_added_inventory(current_user),
    )


WATCHLIST_BUCKETS = ("all", "local", "services", "rent", "unavailable")


def watchlist_bucket(row):
    """Return the one exclusive availability bucket of a watchlist row.

    Owned outranks streaming. Streaming outranks renting. The result is
    None while the fetch of the availability of the film continues."""

    if row["owned"]:
        return "local"
    if row["streaming"]:
        return "services"
    if row["rentals"]:
        return "rent"
    if row["availability_pending"]:
        return None
    return "unavailable"


@bp.route("/watchlist", methods=["GET", "POST"])
@login_required
def watchlist():
    """Render the want-to-watch list of the user.

    This is the funnel stage before the shopping list. This page still
    warms the availability in a batch. Thus, the popover of each tile
    answers "how can I watch this" from a hot cache. The badges live in
    the popover since the revision of Glenn in 2026-08."""

    # The availability filter: the default is ALL. The user can narrow
    # it to one exclusive bucket of the list. The removal redirect keeps
    # the filter in place. The title and runtime filters (#216/#195) go
    # with the same query string. They survive the redirect the same way.

    availability_filter = request.args.get("availability", "all")
    if availability_filter not in WATCHLIST_BUCKETS:
        availability_filter = "all"
    q = (request.args.get("q") or "").strip()
    minutes = request.args.get("minutes", type=int)
    if minutes is not None and minutes < 1:
        minutes = None

    watchlist_form = WatchlistForm()
    if (
        watchlist_form.remove_watchlist_submit.data
        and watchlist_form.validate_on_submit()
        and watchlist_form.movie_id.data
    ):
        clear_watchlist(current_user.id, watchlist_form.movie_id.data)
        db.session.commit()
        # A background post from the tile (#187) wants the JSON state,
        # not a redirect. The client clears the tile itself.
        if _card_fetch():
            return jsonify({"on_watchlist": False})
        flash("Removed from your watchlist", "success")
        return redirect(
            url_for(
                "main.watchlist",
                availability=(
                    availability_filter if availability_filter != "all" else None
                ),
                q=q or None,
                minutes=minutes,
            )
        )

    # contains_eager goes with the join. Without it, every entry.movie
    # below lazy-loads its own row. That is 400 queries on a 400-film
    # list.

    entries = (
        UserWatchlist.query.filter_by(user_id=int(current_user.id))
        .join(Movie, Movie.id == UserWatchlist.movie_id)
        .options(contains_eager(UserWatchlist.movie))
        .order_by(UserWatchlist.date_added.desc())
        .all()
    )

    # The availability works like the other list surfaces. It is
    # cache-only, from the store that the nightly refresh keeps full.
    # Fitzflix warms an uncached film (a film added since last night) in
    # the background. It never fetches inline. 50 fetches under the rate
    # limiter cost this page 4 seconds before 2026-08.

    provider_ids = user_provider_ids(current_user)
    availability_by_id = {}
    if provider_ids:
        availability_by_id, deferred = batch_title_availability(
            (entry.movie.tmdb_id for entry in entries if entry.movie.tmdb_id),
            fetch_limit=0,
        )
        if deferred and current_app.redis.set(
            f"fitzflix:streaming:warm:watchlist:{int(current_user.id)}",
            "1",
            nx=True,
            ex=900,
        ):
            current_app.maintenance_queue.enqueue(
                "app.streaming.warm_title_availability",
                args=(deferred,),
                job_timeout="30m",
                description=(
                    f"Warming streaming availability for {len(deferred)} films"
                ),
            )

    # Owned films show the library mark. Thus, the user can separate
    # owned-wanted and unowned-wanted at a glance.

    owned_ids = {
        movie_id
        for (movie_id,) in db.session.query(Movie.id)
        .filter(Movie.id.in_([entry.movie_id for entry in entries] or [0]))
        .filter(Movie.files.any(File.feature_type_id.is_(None)))
    }

    # The ad-hoc Radarr hand-off: admins get request and withdraw
    # entries on the Find menus of unowned tiles, from the hour-cached
    # id set.

    radarr_ids = (
        radarr_tmdb_ids() if current_user.admin and radarr_configured() else set()
    )

    rows = []
    streaming_attribution = False
    for entry in entries:
        movie = entry.movie
        streaming = []
        rentals = []
        if provider_ids and movie.tmdb_id:
            availability = availability_by_id.get(movie.tmdb_id)
            # Owned rows skip the leaving and newly-added annotations.
            # The copy on the shelf does not go anywhere.
            streaming = streaming_matches(
                availability,
                provider_ids,
                tmdb_id=None if movie.id in owned_ids else movie.tmdb_id,
            )
            rentals = rental_matches(availability, provider_ids)
            if streaming or rentals:
                streaming_attribution = True
        rows.append(
            {
                "movie": movie,
                "date_added": entry.date_added,
                "owned": movie.id in owned_ids,
                "streaming": streaming,
                "rentals": rentals,
                # The warming state: Fitzflix cannot classify a film for
                # the streaming and rental filters before it fetches the
                # availability. It must report the film as pending. It
                # must never drop it silently. A film without a tmdb_id
                # is known-negative, not pending.
                "availability_pending": bool(
                    not (movie.id in owned_ids)
                    and provider_ids
                    and movie.tmdb_id
                    and availability_by_id.get(movie.tmdb_id) is None
                ),
                "in_radarr": movie.tmdb_id in radarr_ids if movie.tmdb_id else False,
            }
        )

    # The filter semantics (the revision of Glenn in 2026-08): the 4
    # buckets are exclusive. Every film goes into exactly one bucket, by
    # the best way to watch it. LOCAL = owned library files, whatever
    # else carries the film. ON MY SERVICES = unowned, on a subscribed
    # streaming service. An additional rental listing does not move it.
    # FOR RENT = unowned, rentable, and on no subscribed service.
    # UNAVAILABLE = none of the above, with the availability known. A
    # film that still warms has no bucket. Fitzflix reports it as
    # pending. It does not file it as unavailable.

    for row in rows:
        row["bucket"] = watchlist_bucket(row)

    # The title and runtime filters narrow the list before the buckets
    # count. Thus, the pills always add up in the current search. The
    # runtime semantics match the landing page: films that fit the
    # evening, with unknown runtimes hidden only from filtered views.

    total = len(rows)
    if q:
        needle = q.lower()
        rows = [
            row
            for row in rows
            if needle in (row["movie"].tmdb_title or "").lower()
            or needle in (row["movie"].title or "").lower()
        ]
    if minutes:
        rows = [
            row
            for row in rows
            if row["movie"].tmdb_runtime and row["movie"].tmdb_runtime <= minutes
        ]

    counts = {
        chosen: sum(1 for row in rows if chosen == "all" or row["bucket"] == chosen)
        for chosen in WATCHLIST_BUCKETS
    }
    pending = sum(1 for row in rows if row["availability_pending"])
    if availability_filter != "all":
        rows = [row for row in rows if row["bucket"] == availability_filter]

    return render_template(
        "watchlist.html",
        title="My Watchlist",
        rows=rows,
        availability=availability_filter,
        q=q,
        minutes=minutes,
        total=total,
        counts=counts,
        # The warming note matters only where unfetched films are hidden.
        # That is every view except ALL and IN LIBRARY.
        pending=(
            pending if availability_filter in ("services", "rent", "unavailable") else 0
        ),
        watchlist_form=watchlist_form,
        radarr_form=RadarrForm(),
        radarr_available=bool(current_user.admin and radarr_configured()),
        radarr_proxy_url=current_app.config["RADARR_PROXY_URL"],
        streaming_attribution=streaming_attribution,
    )


@bp.route("/radarr", methods=["POST"])
@login_required
@admin_required
def radarr_request():
    """Request one unowned film for download through Radarr.

    This is the ad-hoc Radarr hand-off. It is a per-film action that
    the user takes on purpose from the Find menu on the movie page or a
    watchlist tile. It is never automatic. An auto-sync of the whole
    watchlist would fill the volume. The withdraw branch has no UI
    since Glenn removed the Un-request entry (2026-08). The user
    removes films in Radarr itself. But the branch stays as the
    route-level counterpart. An `origin` query param carries where the
    visitor came from. Fitzflix validates it to a local path."""

    radarr_form = RadarrForm()
    origin = request.args.get("origin", "", type=str)
    if not origin.startswith("/") or origin.startswith("//"):
        origin = None
    if not radarr_form.validate_on_submit() or not radarr_form.movie_id.data:
        flash("That Radarr request was not valid.", "warning")
        return redirect(origin or url_for("main.index"))
    movie = Movie.query.filter_by(id=radarr_form.movie_id.data).first_or_404()
    dest = redirect(origin or url_for("main.movie", movie_id=movie.id))
    title = (
        f"{movie.tmdb_title if movie.tmdb_title else movie.title} "
        f"({movie.tmdb_release_date.strftime('%Y') if movie.tmdb_title and movie.tmdb_release_date else movie.year})"
    )

    if not radarr_configured():
        flash("Radarr is not configured. Fitzflix cannot request films.", "warning")
        return dest
    if not movie.tmdb_id:
        flash(f"'{title}' has no TMDB id. Radarr cannot find it.", "warning")
        return dest

    try:
        if radarr_form.radarr_request_submit.data:
            owned = (
                db.session.query(File.id)
                .filter(File.movie_id == movie.id, File.feature_type_id.is_(None))
                .first()
                is not None
            )
            if owned:
                flash(f"'{title}' is already in the library.", "warning")
                return dest
            request_movie(movie.tmdb_id)
            flash(
                f"Requested '{title}' through Radarr. When the download "
                f"arrives, Fitzflix imports it automatically.",
                "success",
            )
        elif radarr_form.radarr_remove_submit.data:
            withdraw_movie(movie.tmdb_id)
            flash(f"Removed '{title}' from Radarr.", "success")
    except RadarrError as e:
        flash(str(e), "warning")
    except Exception:
        current_app.logger.warning(traceback.format_exc())
        flash("Fitzflix could not reach Radarr. Try again in a moment.", "danger")
    return dest


@bp.route("/recommendations")
@login_required
def recommendations():
    """Render the Recommendations page (#235).

    The page shows shelves of unseen films keyed by shared criteria:
    genres, keywords, awards won, and people. Two films that the user
    showed interest in anchor each shelf (or sometimes 1 film, #249).
    Every reload draws a new set of shelves. An action on a suggestion
    replaces only that film, through the tile endpoint below. This page
    replaced the old /rate drive."""

    built = build_shelves(current_user)
    wanted = {
        movie_id
        for shelf in built
        for movie_id in shelf["anchor_ids"] + shelf["movie_ids"]
    }
    movies = {
        movie.id: movie
        for movie in Movie.query.filter(Movie.id.in_(list(wanted) or [0]))
    }

    # The genres that the anchors of a copref shelf share, named for its
    # subtitle (Glenn, 2026-08-30). One query resolves them across all
    # shelves.

    shared_ids = {
        genre_id for shelf in built for genre_id in shelf.get("shared_genre_ids", ())
    }
    genre_names = dict(
        db.session.query(TMDBGenre.id, TMDBGenre.name).filter(
            TMDBGenre.id.in_(list(shared_ids) or [0])
        )
    )

    shelves = []
    for shelf in built:
        anchors = [
            movies[movie_id] for movie_id in shelf["anchor_ids"] if movie_id in movies
        ]
        films = [
            movies[movie_id] for movie_id in shelf["movie_ids"] if movie_id in movies
        ]
        if len(anchors) != len(shelf["anchor_ids"]) or not anchors or not films:
            continue
        shelves.append(
            {
                "kind": shelf["kind"],
                "criteria_param": ",".join(key for key, _ in shelf["criteria"]),
                "labels": [label for _, label in shelf["criteria"]],
                "anchors": anchors,
                "movies": films,
                "shared_genres": sorted(
                    genre_names[genre_id]
                    for genre_id in shelf.get("shared_genre_ids", ())
                    if genre_id in genre_names
                )[:3],
            }
        )

    has_history = (
        db.session.query(UserMovieReview.id)
        .filter(UserMovieReview.user_id == int(current_user.id))
        .first()
        is not None
    )

    return render_template(
        "recommendations.html",
        title="Recommendations",
        shelves=shelves,
        has_history=has_history,
    )


@bp.route("/recommendations/tile")
@login_required
def recommendations_tile():
    """Render one replacement tile for a Recommendations shelf slot.

    The tile is the next eligible film that matches the criteria of the
    shelf and that is not already on the page. ?criteria= carries the
    comma-joined feature keys of the shelf. ?exclude= carries the movie
    ids that show now. A 204 means that the criteria set is exhausted.
    Then the slot must close."""

    criteria = parse_criteria(request.args.get("criteria"))
    if criteria is None:
        abort(400)
    exclude = [
        int(part)
        for part in (request.args.get("exclude") or "").split(",")
        if part.strip().isdigit()
    ]
    movie_id = replacement_film(current_user, criteria, exclude)
    if movie_id is None:
        return "", 204
    return render_template(
        "_recommendation_tile.html", movie=db.session.get(Movie, movie_id)
    )


@bp.route("/review/tmdb/<int:tmdb_id>", methods=["GET", "POST"])
@login_required
def review_tmdb(tmdb_id):
    """Review a film that is not in the library, looked up on TMDB.

    The review creates a review-only movie record. The standard TMDB
    refresh pipeline enriches it afterwards. Thus, the film shows in
    search and in filmographies like every other seen-but-unowned title.
    A film already in the library redirects to its movie page. That page
    has the same review form.
    """

    movie = Movie.query.filter_by(tmdb_id=tmdb_id).first()
    if movie:
        # A live ladder tap can aim here after the record appeared (the
        # first tap on the landing card creates the record). Fitzflix
        # forwards it with the method and body intact (307, not 302).
        # Thus, the full ladder handling of the movie route (re-rate,
        # toggle-off, ✕) takes over. Poster-card watchlist toggles (#45c)
        # forward the same way.

        if (_ladder_fetch() or _card_fetch()) and request.method == "POST":
            return redirect(url_for("main.movie", movie_id=movie.id), code=307)
        return redirect(url_for("main.movie", movie_id=movie.id))

    if not current_app.config["TMDB_API_KEY"]:
        flash("TMDB_API_KEY is not configured. Fitzflix cannot query TMDB.", "warning")
        return redirect(url_for("main.history"))

    try:
        r = tmdb_get(
            current_app.config["TMDB_API_URL"] + "/movie/" + str(tmdb_id),
            params={
                "api_key": current_app.config["TMDB_API_KEY"],
                "append_to_response": "credits,release_dates",
            },
            timeout=10,
        )
        r.raise_for_status()
    except Exception:
        current_app.logger.warning(traceback.format_exc())
        flash("Fitzflix could not reach TMDB. Try again in a moment.", "warning")
        return redirect(url_for("main.history"))

    details = r.json()

    # The runtime, genres, US certification, and top billing. This
    # mirrors what the movie page shows for library films.

    genres = [
        (g.get("id"), g.get("name"))
        for g in details.get("genres") or []
        if g.get("name")
    ]
    certification = None
    for country_release in (details.get("release_dates") or {}).get("results") or []:
        if country_release.get("iso_3166_1") == "US":
            for release in country_release.get("release_dates") or []:
                if release.get("certification"):
                    certification = release["certification"]
                    break
            break

    billed_cast = sorted(
        (details.get("credits") or {}).get("cast") or [],
        key=lambda person: person.get("order", 99),
    )

    # The filmography page serves every TMDB person id. Thus, every cast
    # member links, with or without local credit rows.

    cast = [
        {
            "id": person.get("id"),
            "name": person.get("name"),
            "profile_path": person.get("profile_path"),
            "character": person.get("character"),
        }
        for person in billed_cast
        if person.get("id") is not None
    ]

    # The (person id, name) pairs for the directed-by line. This matches
    # the movie page. The filmography route serves every TMDB person id.

    directors = []
    for person in (details.get("credits") or {}).get("crew") or []:
        if person.get("job") == "Director" and person.get("id") is not None:
            pair = (person["id"], person.get("name"))
            if pair not in directors:
                directors.append(pair)
    film_title = details.get("title")
    release_year = (details.get("release_date") or "")[:4]
    if not film_title or not release_year.isdigit():
        flash(
            "TMDB has no title or release year for that film yet. Fitzflix "
            "cannot review it.",
            "warning",
        )
        return redirect(url_for("main.history"))
    year = int(release_year)

    # The watchlist add creates the same review-only record that a log
    # creates. Thus, the film is enriched and first-class from the moment
    # the user wants it.

    watchlist_form = WatchlistForm()
    if watchlist_form.add_watchlist_submit.data and watchlist_form.validate_on_submit():
        movie, created = find_or_create_tmdb_movie(
            tmdb_id, film_title, year, details=details
        )
        listed = UserWatchlist.query.filter_by(
            user_id=int(current_user.id), movie_id=movie.id
        ).first()
        if listed is None:
            db.session.add(UserWatchlist(user_id=current_user.id, movie_id=movie.id))
        db.session.commit()
        if created:
            current_app.request_queue.enqueue(
                "app.videos.refresh_tmdb_info",
                args=("Movies", movie.id, tmdb_id),
                job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
                description=(
                    f"Refreshing TMDB data for '{movie.title} ({movie.year})'"
                ),
            )
        if _card_fetch():
            return jsonify({"on_watchlist": True})
        flash(f"Added '{film_title} ({year})' to your watchlist", "success")
        return redirect(url_for("main.movie", movie_id=movie.id))

    # The date starts blank, the same as on the movie page. The default
    # is a verdict without a date. The field is there when the user knows
    # the date.

    movie_review_form = MovieReviewForm()
    quick_present, quick_rating = _quick_rating()
    if (
        movie_review_form.review_submit.data or quick_present
    ) and movie_review_form.validate_on_submit():
        if quick_present and quick_rating is None:
            flash("That rating is not valid.", "warning")
            return redirect(url_for("main.review_tmdb", tmdb_id=tmdb_id))
        movie, created = find_or_create_tmdb_movie(
            tmdb_id, film_title, year, details=details
        )

        # The ✕ of the ladder is the not-interested flag, never a
        # review. This is the same flow as the dedicated button below.

        if quick_present and quick_rating == 0:
            if _mark_not_interested(current_user.id, movie.id):
                if created:
                    current_app.request_queue.enqueue(
                        "app.videos.refresh_tmdb_info",
                        args=("Movies", movie.id, tmdb_id),
                        job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
                        description=(
                            f"Refreshing TMDB data for '{movie.title} ({movie.year})'"
                        ),
                    )
                _enqueue_profile_recompute()
                if not _ladder_fetch():
                    flash(
                        f"Fitzflix will not recommend '{film_title} ({year})'.",
                        "info",
                    )
            elif not _ladder_fetch():
                flash(
                    f"You logged '{film_title} ({year})'. The lowest "
                    f"rating for a seen film is 1 star.",
                    "warning",
                )
            if _ladder_fetch():
                return _ladder_state(current_user.id, movie.id)
            return redirect(url_for("main.movie", movie_id=movie.id))

        rating = quick_rating
        # A bare submission (no rating or text) is a plain diary entry.
        # It is a watch, not a review. Thus, it carries no review date.
        # Fitzflix computes rewatch the way it does for Plex watches. An
        # earlier row for this user and film makes this a repeat viewing.

        is_review = bool(
            rating is not None or (movie_review_form.review.data or "").strip()
        )

        # A second star tap on a film already reviewed today corrects
        # that review in place. This is the same rule as on the movie page.

        edited = None
        if (
            quick_present
            and rating is not None
            and not (movie_review_form.review.data or "").strip()
            and movie_review_form.date_watched.data is None
        ):
            edited = _same_day_rerate(current_user.id, movie.id, rating)
        if edited is None:
            rewatch = (
                db.session.query(UserMovieReview.id)
                .filter_by(user_id=current_user.id, movie_id=movie.id)
                .first()
                is not None
            )
            db.session.add(
                UserMovieReview(
                    user_id=current_user.id,
                    movie_id=movie.id,
                    review=movie_review_form.review.data,
                    liked=rating is not None and rating >= 3,
                    date_watched=_watched_timestamp(
                        movie_review_form.date_watched.data
                    ),
                    date_reviewed=datetime.now() if is_review else None,
                    rewatch=rewatch,
                    **star_rating_fields(rating),
                )
            )
        clear_watchlist(current_user.id, movie.id)
        clear_not_interested(current_user.id, movie.id)
        db.session.commit()
        if rating is not None:
            # The redirect goes to the movie page. There, a positive
            # rating gets the "since you liked…" strip. A new record has
            # no features yet. Thus, its strip stays empty until the
            # enrichment arrives. This is harmless. A poster-card rating
            # (#45c) never moves the anchor of the drive.
            if not request.form.get("from_card"):
                set_last_response(
                    current_app.redis,
                    current_user.id,
                    movie.id,
                    "rated",
                    positive=rating >= 3,
                )
            _enqueue_profile_recompute()

        if created:
            current_app.request_queue.enqueue(
                "app.videos.refresh_tmdb_info",
                args=("Movies", movie.id, tmdb_id),
                job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
                description=(
                    f"Refreshing TMDB data for '{movie.title} ({movie.year})'"
                ),
            )

        if _ladder_fetch():
            return _ladder_state(current_user.id, movie.id)
        if is_review:
            flash(f"Logged review for '{film_title} ({year})'", "success")
        else:
            flash(f"Logged '{film_title} ({year})' in your history", "success")
        return redirect(url_for("main.movie", movie_id=movie.id))

    # The preview of the engine, as on the movie page. The TMDB-keyed
    # lane of the shared score source feeds the estimate of the ladder.
    # A film with no record cannot have the own stars of the user yet.
    # "Might interest you" keeps the coarse-scorer rule that the TMDB
    # search results use, minus the person term, because there is no
    # person context here.

    estimated = None
    might_interest = False
    profile = stored_profile(current_app.redis, current_user.id)
    if profile:
        score = resolved_tmdb_score(
            current_app.redis, current_user.id, tmdb_id, profile
        )
        if score is not None:
            estimated = estimated_rating(profile, score)
        coarse = coarse_interest_score(
            profile, [genre_id for genre_id, _ in genres if genre_id], str(year)
        )
        might_interest = coarse > marker_bar(profile)

    # A movie-shaped stand-in. Thus, the shared store-search dropdown and
    # the external-site links render for a film with no local record.

    store_lookup = SimpleNamespace(
        title=film_title,
        year=year,
        tmdb_title=None,
        tmdb_release_date=None,
        tmdb_id=tmdb_id,
        imdb_id=details.get("imdb_id"),
        criterion_spine_number=None,
        criterion_set_title=None,
        criterion_in_print=None,
        criterion_film_id=None,
    )

    return render_template(
        "review_tmdb.html",
        title=f'Review "{film_title} ({year})"',
        poster_fold=poster_fold(current_user, tmdb_id),
        film_title=film_title,
        year=year,
        overview=details.get("overview"),
        poster_path=details.get("poster_path"),
        runtime=details.get("runtime"),
        genres=genres,
        certification=certification,
        cast=cast,
        directors=directors,
        estimated_rating=estimated,
        might_interest=might_interest,
        movie_review_form=movie_review_form,
        movie=store_lookup,
        streaming=user_streaming(tmdb_id, current_user, negative=True),
        watchlist_form=watchlist_form,
        radarr_proxy_url=current_app.config["RADARR_PROXY_URL"],
    )
