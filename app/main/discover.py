"""The discovery surfaces (the routes.py split): the landing rails, the
Recommendations page, the TMDB log page, the poster popover cards (film
and series), the watchlist, and the Radarr hand-off."""

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

# flask.Markup was removed in Flask 2.4; import from its actual home
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
    estimated_rating,
    not_interested_movie_ids,
    resolved_score,
    resolved_tmdb_score,
    rotate_daily,
    rotate_partition,
    frozen_shelf,
    shuffle_daily,
    stored_profile,
    stored_recommendations,
    stored_scores,
    watch_again_shelf,
)
from app.availability_alerts import recent_availability
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
from app.streaming_rail import ENRICHED_KEY, enriched_movie, stored_rail
from app.videos import (
    clear_not_interested,
    clear_watchlist,
    find_or_create_tmdb_movie,
    star_rating_fields,
)
from rq.registry import ScheduledJobRegistry, StartedJobRegistry

# At most this many of a rail's 12 daily slots go to watchlist pins;
# bigger watchlists rotate through the pinned slots day by day, so the
# list always surfaces without ever crowding out discovery

WATCHLIST_PIN_LIMIT = 4


def _fits(movie, minutes):
    """True when the film fits the evening's runtime filter."""

    return bool(movie.tmdb_runtime and movie.tmdb_runtime <= minutes)


@bp.route("/")
@bp.route("/index")
@login_required
def index():
    """The landing page: what to watch tonight, recommended from the
    library by the user's own diary (GitHub #46/#61).

    ?minutes=N filters both rails at view time to films that fit the
    evening — the computed recommendations themselves never consider
    length, and films with unknown runtimes hide only from filtered
    views.

    The default view's shelves are frozen for the calendar day (#204):
    the first render snapshots each rail's cards and later renders
    replay them, with only a no-longer-eligible film's slot replaced —
    see frozen_shelf. The ?minutes= view stays a live pick, a
    transient planning lens rather than the shelf."""

    minutes = request.args.get("minutes", type=int)
    if minutes is not None and minutes < 1:
        minutes = None

    stored = stored_recommendations(current_app.redis, current_user.id)

    has_history = (
        db.session.query(UserMovieReview.id)
        .filter(UserMovieReview.user_id == int(current_user.id))
        .first()
        is not None
    )

    recs = []
    # Candidates that clear every other test, so an empty rail can tell
    # "nothing fits the filter" from "nothing left to recommend" (#198)
    recs_eligible = 0
    computed_at = None
    if stored:
        computed_at = stored.get("computed_at")

        # Films logged since the nightly recompute drop out immediately
        # rather than lingering as recommendations until tonight

        seen = {
            movie_id
            for (movie_id,) in db.session.query(UserMovieReview.movie_id)
            .filter(UserMovieReview.user_id == int(current_user.id))
            .filter(UserMovieReview.movie_id.isnot(None))
        }
        # Films waved off since the nightly run drop immediately too
        seen |= not_interested_movie_ids(current_user.id)
        movie_ids = [item["movie_id"] for item in stored.get("items", [])]
        movies = {
            movie.id: movie
            for movie in Movie.query.filter(Movie.id.in_(movie_ids or [0]))
        }
        # Watchlisted owned films pin ahead of the rotation regardless
        # of where (or whether) they sit in the stored ranking — the
        # library is big, but these are the ones specifically wanted.
        # Pins are capped so a long watchlist rotates through its slots
        # instead of freezing the rail — the discovery slots always
        # keep the majority

        because_by_id = {
            item["movie_id"]: item.get("because", [])[:3]
            for item in stored.get("items", [])
        }
        wanted_owned = (
            db.session.query(Movie)
            .join(UserWatchlist, UserWatchlist.movie_id == Movie.id)
            .filter(UserWatchlist.user_id == int(current_user.id))
            .filter(Movie.files.any(File.feature_type_id.is_(None)))
            .order_by(UserWatchlist.date_added.desc())
            .all()
        )
        watchlist_ids = {movie.id for movie in wanted_owned}
        recs_eligible = len(wanted_owned)
        pin_candidates = [
            {
                "movie": movie,
                "because": because_by_id.get(movie.id, []),
                "watchlisted": True,
            }
            for movie in wanted_owned
        ]

        rec_rows = []
        for item in stored.get("items", []):
            movie = movies.get(item["movie_id"])
            if movie is None or item["movie_id"] in seen:
                continue
            if item["movie_id"] in watchlist_ids:
                continue
            recs_eligible += 1
            rec_rows.append(
                {
                    "movie": movie,
                    "because": item.get("because", [])[:3],
                    "watchlisted": False,
                }
            )

        # A no-repeat daily partition through the deep stored ranking:
        # twelve films a day, one per quality tier, cycling the whole
        # set (400+ films, roughly monthly) before anything repeats.
        # The day's cards then shuffle so neither the amber pins nor
        # the quality tiers hold fixed positions (Glenn: slot one must
        # not always be a pin or a top-tier film). The default view
        # freezes the day's result (#204); the ?minutes= view stays a
        # live pick over the films that fit — it's a transient planning
        # lens, not the shelf

        def pick_recs(pins, rows):
            """The day's baseline card order for the library rail."""

            chosen = rotate_partition(
                pins, WATCHLIST_PIN_LIMIT, date.today().toordinal()
            )
            return shuffle_daily(
                chosen
                + rotate_partition(rows, 12 - len(chosen), date.today().toordinal()),
                f"mix:recs:{int(current_user.id)}:{date.today().isoformat()}",
            )

        if minutes:
            recs = pick_recs(
                [row for row in pin_candidates if _fits(row["movie"], minutes)],
                [row for row in rec_rows if _fits(row["movie"], minutes)],
            )
        else:
            rows_by_id = {row["movie"].id: row for row in pin_candidates + rec_rows}
            shown_ids = frozen_shelf(
                current_app.redis,
                current_user.id,
                day=date.today().isoformat(),
                shelf="recs",
                eligible_ids=list(rows_by_id),
                pick=lambda: [
                    row["movie"].id for row in pick_recs(pin_candidates, rec_rows)
                ],
            )
            recs = [rows_by_id[movie_id] for movie_id in shown_ids]
    elif has_history:
        # Diary rows but nothing stored yet (first deploy, or a brand-new
        # reviewer): compute once now instead of waiting for tonight; the
        # marker keeps repeat page loads from re-enqueueing

        if current_app.redis.set(
            f"fitzflix:recs:requested:{int(current_user.id)}", "1", nx=True, ex=3600
        ):
            current_app.maintenance_queue.enqueue(
                "app.recommendations.recompute_recommendations",
                job_timeout="1h",
                description="Computing film recommendations",
            )

    # The rewatch shelf: owned films the user liked whose last watch is
    # long past — old favorites otherwise have no surface, since the
    # engine's candidates exclude logged films. Watchlisted ones
    # (re-added = declared rewatch intent) pin first under the same cap
    # as the other rails; the rest rotates daily

    again_items = []
    again_eligible = 0
    again_ranked = watch_again_shelf(current_user.id)
    if again_ranked:
        again_movies = {
            m.id: m
            for m in Movie.query.filter(
                Movie.id.in_([item["movie_id"] for item in again_ranked])
            )
        }
        again_watchlisted = {
            movie_id
            for (movie_id,) in db.session.query(UserWatchlist.movie_id).filter(
                UserWatchlist.user_id == int(current_user.id)
            )
        }
        again_rows = []
        for item in again_ranked:
            again_movie = again_movies.get(item["movie_id"])
            if again_movie is None:
                continue
            again_eligible += 1
            again_rows.append(
                {
                    "movie": again_movie,
                    "last_watched": item["last_watched"],
                    "watchlisted": item["movie_id"] in again_watchlisted,
                }
            )

        def pick_again(rows):
            """The day's baseline card order for the rewatch shelf."""

            chosen = rotate_partition(
                [row for row in rows if row["watchlisted"]],
                WATCHLIST_PIN_LIMIT,
                date.today().toordinal(),
            )
            return shuffle_daily(
                chosen
                + rotate_daily(
                    [row for row in rows if not row["watchlisted"]],
                    12 - len(chosen),
                    f"again:{int(current_user.id)}:{date.today().isoformat()}",
                ),
                f"mix:again:{int(current_user.id)}:{date.today().isoformat()}",
            )

        if minutes:
            again_items = pick_again(
                [row for row in again_rows if _fits(row["movie"], minutes)]
            )
        else:
            again_by_id = {row["movie"].id: row for row in again_rows}
            shown_ids = frozen_shelf(
                current_app.redis,
                current_user.id,
                day=date.today().isoformat(),
                shelf="again",
                eligible_ids=[
                    row["movie"].id for row in again_rows if row["watchlisted"]
                ]
                + [row["movie"].id for row in again_rows if not row["watchlisted"]],
                pick=lambda: [row["movie"].id for row in pick_again(again_rows)],
            )
            again_items = [again_by_id[movie_id] for movie_id in shown_ids]

    # The second rail: films streaming on this user's services, from the
    # nightly discover-pool recompute. Films logged or acquired since
    # the run drop out immediately; a user with a profile and provider
    # picks but no stored rail gets a one-off compute enqueued

    rail = []
    rail_eligible = 0
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
        # A watchlisted film on the rail is the best kind of match —
        # wanted, and streaming on a service already paid for — so it
        # pins ahead of the daily rotation, capped like the library
        # rail so discovery keeps most of the slots

        watchlisted_now = set()
        if rail_ids:
            watchlisted_now = {
                tmdb_id
                for (tmdb_id,) in db.session.query(Movie.tmdb_id)
                .join(UserWatchlist, UserWatchlist.movie_id == Movie.id)
                .filter(Movie.tmdb_id.in_(rail_ids))
                .filter(UserWatchlist.user_id == int(current_user.id))
            }
        rail_rows = []
        for item in rail_payload.get("items", []):
            if item["tmdb_id"] in dropped:
                continue
            rail_eligible += 1
            item["watchlisted"] = item["tmdb_id"] in watchlisted_now
            rail_rows.append(item)

        def pick_rail(rows):
            """The day's baseline card order for the streaming rail."""

            chosen = rotate_partition(
                [item for item in rows if item["watchlisted"]],
                WATCHLIST_PIN_LIMIT,
                date.today().toordinal(),
            )
            return shuffle_daily(
                chosen
                + rotate_daily(
                    [item for item in rows if not item["watchlisted"]],
                    12 - len(chosen),
                    f"rail:{int(current_user.id)}:{date.today().isoformat()}",
                ),
                f"mix:rail:{int(current_user.id)}:{date.today().isoformat()}",
            )

        if minutes:
            rail = pick_rail(
                [
                    item
                    for item in rail_rows
                    if item.get("runtime") and item["runtime"] <= minutes
                ]
            )
        else:
            rail_by_id = {item["tmdb_id"]: item for item in rail_rows}
            shown_ids = frozen_shelf(
                current_app.redis,
                current_user.id,
                day=date.today().isoformat(),
                shelf="rail",
                eligible_ids=[
                    item["tmdb_id"] for item in rail_rows if item["watchlisted"]
                ]
                + [item["tmdb_id"] for item in rail_rows if not item["watchlisted"]],
                pick=lambda: [item["tmdb_id"] for item in pick_rail(rail_rows)],
            )
            rail = [rail_by_id[tmdb_id] for tmdb_id in shown_ids]
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

    # The departure shelf: what leaves the Criterion Channel at month's
    # end, taste-ranked, for Criterion subscribers. The runtime filter
    # applies like everywhere else

    shelf = leaving_shelf(current_user)
    shelf_items = []
    shelf_eligible = 0
    shelf_departs = None
    shelf_url = None
    if shelf:
        shelf_departs = shelf["departs"].strftime("%B %-d")
        shelf_url = shelf["url"]
        shelf_eligible = len(shelf["items"])

        # Watchlist urgencies stay pinned; the rest rotates daily so a
        # month-long departure set doesn't look frozen — but frozen
        # WITHIN the day like the other rails (#204). No shuffle here:
        # watchlist-first order is urgency, not discovery

        def pick_shelf(rows):
            """The day's baseline card order for the departure shelf."""

            chosen = [item for item in rows if item.get("watchlisted")][:12]
            return chosen + rotate_daily(
                [item for item in rows if not item.get("watchlisted")],
                12 - len(chosen),
                f"shelf:{int(current_user.id)}:{date.today().isoformat()}",
            )

        if minutes:
            shelf_items = pick_shelf(
                [
                    item
                    for item in shelf["items"]
                    if item.get("runtime") and item["runtime"] <= minutes
                ]
            )
        else:
            shelf_by_id = {item["tmdb_id"]: item for item in shelf["items"]}
            shown_ids = frozen_shelf(
                current_app.redis,
                current_user.id,
                day=date.today().isoformat(),
                shelf="leaving",
                eligible_ids=[
                    item["tmdb_id"]
                    for item in shelf["items"]
                    if item.get("watchlisted")
                ]
                + [
                    item["tmdb_id"]
                    for item in shelf["items"]
                    if not item.get("watchlisted")
                ],
                pick=lambda: [item["tmdb_id"] for item in pick_shelf(shelf["items"])],
            )
            shelf_items = [shelf_by_id[tmdb_id] for tmdb_id in shown_ids]

    # A rail the runtime filter emptied says so rather than vanishing —
    # silence reads as "there's nothing here" (#198). Each flag means
    # the rail had films and the filter took them all

    def filtered_out(shown, eligible):
        """True when a rail had films and the runtime filter took
        every one of them."""

        return bool(minutes) and not shown and eligible > 0

    return render_template(
        "index.html",
        title="Home",
        recs=recs,
        computed_at=computed_at,
        has_history=has_history,
        recs_stored=bool(stored),
        recs_filtered_out=filtered_out(recs, recs_eligible),
        again_filtered_out=filtered_out(again_items, again_eligible),
        rail_filtered_out=filtered_out(rail, rail_eligible),
        shelf_filtered_out=filtered_out(shelf_items, shelf_eligible),
        again=again_items,
        rail=rail,
        rail_computed_at=rail_computed_at,
        shelf=shelf_items,
        shelf_departs=shelf_departs,
        shelf_url=shelf_url,
        now_playing=criterion_now_card(current_user),
        criterion_subscriber=is_criterion_subscriber(current_user),
        review_form=MovieReviewForm(),
        minutes=minutes,
    )


@bp.route("/criterion-now")
@login_required
def criterion_now():
    """The Criterion24/7 card fragment, re-fetched by the home
    page once a minute while it's visible so an open tab follows the
    feed — the film swaps when the poller stores the next one, and the
    "minutes in" / "next film" line keeps time in between. Empty (200)
    when there's nothing to show: not a subscriber, no stored film, or
    one gone stale — the page's container empties and the card
    disappears just as a reload would drop it."""

    return render_template(
        "_criterion_now_card.html",
        now_playing=criterion_now_card(current_user),
        review_form=MovieReviewForm(),
    )


@bp.route("/file-activity")
@login_required
def file_activity():
    """The File Activity dashboard: the old Recently Added page and the
    Pipeline Activity trails merged (Glenn, Aug 2026). Landed files
    render as full cards here, each with its trail chips filled by
    base.html's queue poll; files still in flight show as chips on the
    queue page's job rows, and the poll adds a file's card here via
    /file-activity/card once cataloging lands."""

    page = request.args.get("page", 1, type=int)

    # Show only files added or updated in the last 7 days — the page's
    # recency horizon, matching pipeline.TRAIL_TTL_SECONDS so a card
    # keeps its trail chips for as long as it stays on the page. (This
    # window once meant "still in S3 Standard, re-downloadable without a
    # Glacier thaw", but the live lifecycle rule has transitioned
    # untouched/ to Deep Archive at 0 days for some time — verified
    # against the bucket Aug 2026 — so files here are usually already
    # frozen after their first day.)

    # The cards read each file's tracks, film, and series: selectin
    # loads fetch those per page instead of per file (291 queries for
    # 100 cards before Aug 2026)

    recently_added = (
        File.query.outerjoin(FileAudioTrack, (FileAudioTrack.file_id == File.id))
        .distinct()  # need .distinct() in order to get the result numbers per page correct
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

    # Import pipeline activity, assembled from live state: running and queued
    # imports, deferred retries, and whatever sits in the rejects folder

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

        # A file can accumulate several scheduled retries; show each file
        # once, with its soonest retry time

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
    """The File row a pipeline trail belongs to, or None.

    A trail's basename can be the original import filename (the
    localization stages), that name with its extension swapped to .mkv
    (container conversion), or the File row's own basename (every
    file_id-keyed stage) — so match exactly against both stored names
    first, then fall back to the stem, newest file first.
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
    """One file's card fragment for the File Activity dashboard,
    fetched by the queue poll when a trail with no card yet finishes
    cataloging — the chips blossom into the full card (poster, quality
    badge, tracks, links) without a reload. Keyed by the trail's
    basename; 404 means the File row isn't visible yet and the poll
    simply tries again."""

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
    """The poster popover's card fragment (#45c): one film's title,
    credits, synopsis, availability, and at-a-glance badges — keyed
    by movie_id for library records, or tmdb_id for films with no
    local row (the streaming rail and the leaving shelf). Purely
    informational since Glenn's Aug 2026 revision: the star ladder
    and watchlist toggle live on the gallery tiles, so the card
    carries the In-library badge in its shopping colors (amber =
    worth upgrading, green = settled) and the watchlist badge
    instead. ?context=criterion (#77a) recolors that badge with the
    Criterion page's settled rule — green only when the disc is owned
    AND the copy matches the release's format — instead of the
    generic shopping answer. Fetched only when a poster is hovered or
    tapped, so gallery pages stay light."""

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

        # The In-library badge wears the shopping list's answer (Glenn's
        # Aug 2026 revision, replacing the quality-tier badge): amber
        # when the copy is worth upgrading, green when it's settled.
        # ?context=criterion swaps in the Criterion catalog's rule

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

    # No local record: the card renders from TMDB directly

    if not current_app.config["TMDB_API_KEY"]:
        abort(404)
    try:
        r = tmdb_get(
            current_app.config["TMDB_API_URL"] + "/movie/" + str(tmdb_id),
            params={
                "api_key": current_app.config["TMDB_API_KEY"],
                "append_to_response": "credits",
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

    return render_template(
        "_movie_card.html",
        display_title=f"{film_title} ({release_year})",
        href=url_for("main.review_tmdb", tmdb_id=tmdb_id),
        runtime=details.get("runtime"),
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
    """The poster popover's card fragment for one TV series — the film
    card's shape in TV terms: title, the run's meta line, synopsis,
    billed cast, the In-library badge in its shopping colors, and how
    much of the run is on the shelf. Keyed by series_id for library
    records, or tmdb_id for a series with no local row (a person's
    unowned television credits). Fetched only when a poster is hovered
    or tapped, so the TV Library and filmography pages stay light.

    No streaming strip, watchlist badge, or play button: watch
    providers, the watchlist, and the Apple TV hand-off are all
    film-keyed here, so the card carries only what it can answer."""

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

        # What's on the shelf, counted the way the TV Library page
        # counts it: seasons that have files, and the distinct episode
        # numbers within them. Season 0 is called Specials there rather
        # than counted as a season, and the card says it the same way —
        # otherwise a show with specials reads as owning more seasons
        # than TMDB says exist. A series record with no files at all is
        # a leftover, and badges nothing

        season_counts = (
            db.session.query(File.season, db.func.count(db.func.distinct(File.episode)))
            .filter(File.series_id == series.id)
            .group_by(File.season)
            .all()
        )
        owned_seasons = sum(1 for season, _ in season_counts if season)
        return render_template(
            "_tv_card.html",
            # No year appended: every other TV surface titles a series
            # by its TMDB name alone, and the meta line opens with the
            # run of years anyway
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
            overview=series.tmdb_overview,
            top_cast=top_cast,
            in_library=bool(season_counts),
            upgradable=series_upgradable([series.id]).get(series.id, False),
            owned_seasons=owned_seasons,
            owned_specials=any(not season for season, _ in season_counts),
            owned_episodes=sum(count for _, count in season_counts),
        )

    # No local record: the card renders from TMDB directly

    if not current_app.config["TMDB_API_KEY"]:
        abort(404)
    try:
        r = tmdb_get(
            current_app.config["TMDB_API_URL"] + "/tv/" + str(tmdb_id),
            params={
                "api_key": current_app.config["TMDB_API_KEY"],
                "append_to_response": "aggregate_credits",
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

    # aggregate_credits bills the whole run, matching the TVCast rows
    # a local record would have

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
        overview=details.get("overview"),
        top_cast=top_cast,
        in_library=False,
        upgradable=None,
        owned_seasons=0,
        owned_specials=False,
        owned_episodes=0,
    )


# One request may carry at most this many films; every gallery page
# shows a bounded set (rails of 12, paginated walls), so the cap only
# guards against abuse
MOVIE_STATES_LIMIT = 300

# How many enriched payloads one state batch may FETCH from TMDB for
# the tmdb-keyed lane — sized to cover a whole filmography page in a
# single hydration pass (fetches run 10 abreast, so ~100 completes in
# a couple of seconds) while bounding the burst one request can aim
# at TMDB; cached payloads and overlay hits cost nothing and are
# never capped

MOVIE_STATES_TMDB_FETCHES = 100


@bp.route("/movie_states")
@login_required
def movie_states():
    """Batch ladder-and-watchlist state for the poster tiles (#45c,
    Aug 2026 revision): one fetch per gallery page hydrates every
    tile's star row and watchlist toggle, so no gallery route has to
    compute per-film verdicts itself. ?movie_ids= and ?tmdb_ids= are
    comma-separated; tmdb ids are answered under their own key (mapped
    through a local record when one exists, empty state otherwise).
    Estimates come from the shared score source: the stored map,
    live-scoring a bounded number of missing films through the
    resolver that patches the map — the same source every other
    estimate surface reads."""

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

    # The newest verdict per film — the row the movie page displays —
    # gathered in one query and reduced here (newest review first,
    # bare watches last, id breaking ties, like _latest_review_row)

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

    # Poster folds (#197, plus #156/#230's badge): the per-user corner
    # overlays every gallery tile paints through this one batched
    # fetch — green for a watchlist film that recently became
    # available (the nightly alert diff's record), red for a film in
    # the leaving-Criterion set, subscribers only. Movie-keyed tiles
    # need their tmdb ids for the leaving lookup; one query covers
    # them, and leaving_departure parses the stored set once per
    # request on flask.g

    recent = recent_availability(current_user)
    criterion_member = CRITERION_PROVIDER_ID in user_provider_ids(current_user)
    movie_tmdb = {}
    if all_ids and criterion_member:
        movie_tmdb = dict(
            db.session.query(Movie.id, Movie.tmdb_id).filter(Movie.id.in_(all_ids))
        )

    profile = stored_profile(current_app.redis, current_user.id)
    scores = stored_scores(current_app.redis, current_user.id)

    # Estimate-eligible films the map doesn't cover — records created
    # since the last nightly recompute — score live through the shared
    # resolver, whose patch makes the number permanent for every other
    # surface; request order decides who makes the cap

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
        # Every miss on the page scores in this one pass — the work is
        # local queries only, so even a full 300-id batch stays quick —
        # and the resolver's patches make the next request free

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

    # The tmdb-keyed lane: ids with no record at all — most of a
    # filmography page — still estimate, from the overlay when it
    # holds them and otherwise scored live from enriched payloads.
    # Missing payloads warm in PARALLEL first (the rail's enrichment
    # pattern), so one hydration pass covers a whole career page in a
    # couple of seconds instead of twenty films per reload; the fetch
    # cap bounds the burst a single request can aim at TMDB, and the
    # page itself never waits — hydration is an async fetch after
    # render

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
        # The estimate previews until the user's own stars exist — a
        # bare watch (a Plex viewing, an unrated import) still shows it
        if (row is None or row.rating is None) and not flagged:
            score = scores.get(movie_id)
            if score is not None:
                estimated = estimated_rating(profile, score)
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
            "fold_new": (recent.get(movie_id) or {}).get("label"),
            "fold_leaving": (
                leaving_departure(movie_tmdb.get(movie_id))
                if criterion_member
                else None
            ),
        }

    def tmdb_state_for(tmdb_id):
        state = state_for(tmdb_to_movie.get(tmdb_id))
        if state["estimated"] is None:
            state["estimated"] = tmdb_estimates.get(tmdb_id)
        if criterion_member and state["fold_leaving"] is None:
            state["fold_leaving"] = leaving_departure(tmdb_id)
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
    """The complete leaving-Criterion departure inventory: every film
    on the month's leaving page, owned and seen included."""

    return render_template(
        "leaving.html",
        title="Leaving the Criterion Channel",
        inventory=leaving_inventory(current_user),
    )


WATCHLIST_BUCKETS = ("all", "local", "services", "rent", "unavailable")


def watchlist_bucket(row):
    """The one exclusive availability bucket a watchlist row files
    under — owned beats streaming beats renting — or None while the
    film's availability is still being fetched."""

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
    """The user's want-to-watch list: the funnel stage before the
    shopping list. Availability is still batch-warmed here so each
    tile's popover — where the badges live since Glenn's Aug 2026
    revision — answers "how can I watch this" from a hot cache."""

    # The availability filter: default ALL, narrowable to one
    # exclusive bucket of the list — the removal redirect keeps it in
    # place. The title and runtime filters (#216/#195) ride the same
    # query string and survive the redirect the same way

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
        # A background post from the tile (#187) wants JSON state, not
        # a redirect — the client clears the tile itself
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

    # contains_eager rides the join: without it every entry.movie below
    # lazy-loads its own row — 400 queries on a 400-film list

    entries = (
        UserWatchlist.query.filter_by(user_id=int(current_user.id))
        .join(Movie, Movie.id == UserWatchlist.movie_id)
        .options(contains_eager(UserWatchlist.movie))
        .order_by(UserWatchlist.date_added.desc())
        .all()
    )

    # Availability like the other list surfaces: cache-only, from the
    # store the nightly refresh keeps full; anything uncached (a film
    # added since last night) is warmed in the background, never
    # fetched inline — fifty fetches under the rate limiter cost this
    # page four seconds before Aug 2026

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

    # Owned films badge with the library mark, so owned-wanted and
    # unowned-wanted read at a glance

    owned_ids = {
        movie_id
        for (movie_id,) in db.session.query(Movie.id)
        .filter(Movie.id.in_([entry.movie_id for entry in entries] or [0]))
        .filter(Movie.files.any(File.feature_type_id.is_(None)))
    }

    # The ad-hoc Radarr hand-off: admins get request/withdraw
    # entries on unowned tiles' Find menus, from the hour-cached id set

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
            streaming = streaming_matches(
                availability, provider_ids, tmdb_id=movie.tmdb_id
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
                # Warming state: a film whose availability hasn't
                # been fetched yet can't be classified for the
                # streaming/rental filters — it must be reported as
                # pending, never silently dropped. Films without a
                # tmdb_id are known-negative, not pending
                "availability_pending": bool(
                    not (movie.id in owned_ids)
                    and provider_ids
                    and movie.tmdb_id
                    and availability_by_id.get(movie.tmdb_id) is None
                ),
                "in_radarr": movie.tmdb_id in radarr_ids if movie.tmdb_id else False,
            }
        )

    # The filter semantics (Glenn's Aug 2026 revision): the four
    # buckets are exclusive — every film lands in exactly one, by the
    # best way to watch it. LOCAL = owned library files, whatever else
    # carries the film; ON MY SERVICES = unowned, on a subscribed
    # streaming service (a rental listing too doesn't move it); FOR
    # RENT = unowned, rentable, and on no subscribed service;
    # UNAVAILABLE = none of the above with the availability actually
    # known. A film still warming has no bucket: it's reported as
    # pending rather than filed as unavailable

    for row in rows:
        row["bucket"] = watchlist_bucket(row)

    # Title and runtime narrow the list before the buckets count, so
    # the pills always add up within the current search. Runtime
    # semantics match the landing page: films that fit the evening,
    # with unknown runtimes hidden only from filtered views

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
        # The warming note only matters where unfetched films are
        # actually hidden — every view but ALL and IN LIBRARY
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
    """The ad-hoc Radarr hand-off: request one unowned film for
    download — a deliberate per-film action from the Find menu on the
    movie page or a watchlist tile, never automatic (an auto-sync of
    the whole watchlist would fill the volume). The withdraw branch
    has no UI since Glenn dropped the Un-request entry (Aug 2026) —
    films are removed in Radarr itself — but stays as the route-level
    counterpart. An `origin` query param carries where the visitor
    came from, validated to a local path."""

    radarr_form = RadarrForm()
    origin = request.args.get("origin", "", type=str)
    if not origin.startswith("/") or origin.startswith("//"):
        origin = None
    if not radarr_form.validate_on_submit() or not radarr_form.movie_id.data:
        flash("That Radarr request didn't make sense", "warning")
        return redirect(origin or url_for("main.index"))
    movie = Movie.query.filter_by(id=radarr_form.movie_id.data).first_or_404()
    dest = redirect(origin or url_for("main.movie", movie_id=movie.id))
    title = (
        f"{movie.tmdb_title if movie.tmdb_title else movie.title} "
        f"({movie.tmdb_release_date.strftime('%Y') if movie.tmdb_title and movie.tmdb_release_date else movie.year})"
    )

    if not radarr_configured():
        flash("Radarr isn't configured, so films can't be requested.", "warning")
        return dest
    if not movie.tmdb_id:
        flash(f"'{title}' has no TMDB id, so Radarr can't look it up.", "warning")
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
                f"Requested '{title}' via Radarr — when a download lands "
                f"it imports automatically.",
                "success",
            )
        elif radarr_form.radarr_remove_submit.data:
            withdraw_movie(movie.tmdb_id)
            flash(f"Removed '{title}' from Radarr.", "success")
    except RadarrError as e:
        flash(str(e), "warning")
    except Exception:
        current_app.logger.warning(traceback.format_exc())
        flash("Radarr couldn't be reached; try again in a moment.", "danger")
    return dest


@bp.route("/recommendations")
@login_required
def recommendations():
    """The Recommendations page (#235): shelves of unseen films keyed
    by shared criteria — genres, keywords, awards won, people — each
    anchored by two films the user has expressed interest in. Every
    reload draws a fresh set of shelves; acting on a suggestion swaps
    just that film through the tile endpoint below. This page replaced
    the old /rate drive."""

    shelves = []
    for shelf in build_shelves(current_user):
        wanted = shelf["anchor_ids"] + shelf["movie_ids"]
        movies = {
            movie.id: movie for movie in Movie.query.filter(Movie.id.in_(wanted or [0]))
        }
        anchors = [
            movies[movie_id] for movie_id in shelf["anchor_ids"] if movie_id in movies
        ]
        films = [
            movies[movie_id] for movie_id in shelf["movie_ids"] if movie_id in movies
        ]
        if len(anchors) < 2 or not films:
            continue
        shelves.append(
            {
                "kind": shelf["kind"],
                "criteria_param": ",".join(key for key, _ in shelf["criteria"]),
                "labels": [label for _, label in shelf["criteria"]],
                "anchors": anchors,
                "movies": films,
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
    """One replacement tile for a Recommendations shelf slot: the next
    eligible film matching the shelf's criteria that isn't already on
    the page. ?criteria= carries the shelf's comma-joined feature keys
    and ?exclude= the movie ids currently showing; 204 means the
    criteria set is exhausted and the slot should close up."""

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
    """Review a film that isn't in the library, looked up on TMDB.

    Reviewing creates a review-only movie record — enriched afterwards
    through the standard TMDB refresh pipeline — so the film shows up in
    search and filmographies like any other seen-but-unowned title. Films
    already in the library redirect to their movie page, which has the
    same review form.
    """

    movie = Movie.query.filter_by(tmdb_id=tmdb_id).first()
    if movie:
        # A live ladder tap aimed here after the record appeared (the
        # landing card's first tap creates it) forwards with the method
        # and body intact — 307, not 302 — so the movie route's full
        # ladder handling (re-rate, toggle-off, ✕) takes over; poster-
        # card watchlist toggles (#45c) forward the same way

        if (_ladder_fetch() or _card_fetch()) and request.method == "POST":
            return redirect(url_for("main.movie", movie_id=movie.id), code=307)
        return redirect(url_for("main.movie", movie_id=movie.id))

    if not current_app.config["TMDB_API_KEY"]:
        flash("TMDB_API_KEY is not configured, so TMDB can't be queried.", "warning")
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
        flash("TMDB could not be reached; try again in a moment.", "warning")
        return redirect(url_for("main.history"))

    details = r.json()

    # Runtime, genres, US certification, and top billing, mirroring what
    # the movie page shows for library films

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

    # The filmography page serves any TMDB person id, so every cast member
    # links whether or not they have local credit rows

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
    film_title = details.get("title")
    release_year = (details.get("release_date") or "")[:4]
    if not film_title or not release_year.isdigit():
        flash(
            "TMDB has no title or release year for that film yet, so it "
            "can't be reviewed.",
            "warning",
        )
        return redirect(url_for("main.history"))
    year = int(release_year)

    # Watchlist add: creates the same review-only record a log would, so
    # the film is enriched and first-class from the moment it's wanted

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

    # Like the movie page, the date starts blank — a date-less verdict
    # by default, with the field there when the date is actually known

    movie_review_form = MovieReviewForm()
    quick_present, quick_rating = _quick_rating()
    if (
        movie_review_form.review_submit.data or quick_present
    ) and movie_review_form.validate_on_submit():
        if quick_present and quick_rating is None:
            flash("That rating didn't make sense", "warning")
            return redirect(url_for("main.review_tmdb", tmdb_id=tmdb_id))
        movie, created = find_or_create_tmdb_movie(
            tmdb_id, film_title, year, details=details
        )

        # The ladder's ✕ is the not-interested flag, never a
        # review — same flow as the dedicated button below

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
                        f"Got it — '{film_title} ({year})' won't be recommended",
                        "info",
                    )
            elif not _ladder_fetch():
                flash(
                    f"You've logged '{film_title} ({year})' — the lowest "
                    f"rating for a seen film is 1 star",
                    "warning",
                )
            if _ladder_fetch():
                return _ladder_state(current_user.id, movie.id)
            return redirect(url_for("main.movie", movie_id=movie.id))

        rating = quick_rating
        # A bare submission (no rating or text) is a plain diary
        # entry — a watch, not a review — so it carries no review date.
        # Rewatch is computed the way Plex watches compute it: any earlier
        # row for this user and film makes this a repeat viewing.

        is_review = bool(
            rating is not None or (movie_review_form.review.data or "").strip()
        )

        # A repeat star tap on a film already reviewed today corrects
        # that review in place — same rule as the movie page

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
            # The redirect lands on the movie page, where a positive
            # rating earns the "since you liked…" strip (a just-created
            # record has no features yet, so its strip stays empty
            # until the enrichment lands — harmless). A poster-card
            # rating (#45c) never moves the drive's anchor
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

    # A movie-shaped stand-in so the shared store-search dropdown and the
    # external-site links render for a film with no local record

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
        film_title=film_title,
        year=year,
        overview=details.get("overview"),
        poster_path=details.get("poster_path"),
        runtime=details.get("runtime"),
        genres=genres,
        certification=certification,
        cast=cast,
        movie_review_form=movie_review_form,
        movie=store_lookup,
        streaming=user_streaming(tmdb_id, current_user, negative=True),
        watchlist_form=watchlist_form,
        radarr_proxy_url=current_app.config["RADARR_PROXY_URL"],
    )
