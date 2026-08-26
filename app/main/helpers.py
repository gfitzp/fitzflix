"""Shared helpers for the main blueprint's route modules (the routes.py split's
slice f): the verdict/ladder plumbing every rating surface uses, the
admin gate, and the quality-threshold read."""

from datetime import date, datetime

from flask import (
    current_app,
    jsonify,
    flash,
    redirect,
    url_for,
    request,
)

# flask.Markup was removed in Flask 2.4; import from its actual home
from flask_login import current_user

from app import db
from app.models import (
    File,
    Movie,
    RefQuality,
    TVSeries,
    UserMovieReview,
    UserMovieStatus,
    UserWatchlist,
    tv_file_rank,
)
from app.recommendations import (
    estimated_rating,
    resolved_score,
    stored_profile,
)
from app.videos import (
    clear_watchlist,
    star_rating_fields,
)

from functools import wraps


def _enqueue_profile_recompute():
    """Fold fresh ratings into the stored profile within minutes,
    instead of waiting for the 1:45 AM run — NX-marked so a rating
    session enqueues at most one recompute per five minutes."""

    if current_app.redis.set(
        f"fitzflix:elicit:recompute:{int(current_user.id)}", "1", nx=True, ex=300
    ):
        current_app.maintenance_queue.enqueue(
            "app.recommendations.recompute_recommendations",
            job_timeout="1h",
            description="Recomputing film recommendations",
        )


def _quick_rating():
    """(present, rating) from the quick-answer ladder's submission.

    (False, None) when no ladder button was pressed, (True, None) when
    the value is nonsense, (True, 0.0–5.0) otherwise. A 0 is the ✕ —
    "not interested, never saw it" — which handlers route to the
    status-flag path, never to a review: the star scale itself
    starts at 1 ("Hated it") and belongs to seen films only.
    """

    value = (request.form.get("quick_rating") or "").strip()
    if not value:
        return False, None
    if value not in {"0", "1", "2", "3", "4", "5"}:
        return True, None
    return True, float(value)


def _mark_not_interested(user_id, movie_id):
    """Flag a film not-interested and clear any contradicting watchlist
    entry; commits. Returns False without writing when the user has a
    diary row for the film — ✕ means "never saw it", and a seen film's
    harshest verdict is 1 star."""

    if (
        db.session.query(UserMovieReview.id)
        .filter_by(user_id=int(user_id), movie_id=int(movie_id))
        .first()
        is not None
    ):
        return False
    exists = UserMovieStatus.query.filter_by(
        user_id=int(user_id), movie_id=int(movie_id), kind="not_interested"
    ).first()
    if exists is None:
        db.session.add(
            UserMovieStatus(
                user_id=int(user_id), movie_id=int(movie_id), kind="not_interested"
            )
        )
        clear_watchlist(int(user_id), int(movie_id))
    db.session.commit()
    return True


def _ladder_fetch():
    """True when the quick-rating post came from the star row's
    background fetch — it wants JSON state back instead of a
    redirect, and flash messages would only queue up unseen for some
    later page load."""

    return request.headers.get("X-Requested-With") == "ladder"


def _card_fetch():
    """True when the post came from the poster popover's card (#45c) —
    its watchlist toggle wants compact JSON state back, never a
    redirect, and flash messages would queue up unseen."""

    return request.headers.get("X-Requested-With") == "card"


def _latest_review_row(user_id, movie_id):
    """The diary row whose verdict the star widget shows: newest review
    first, bare watches last, newest id breaking ties — the same
    ordering the engine's latest_ratings() mirrors, so what the page
    displays is exactly what the profile scores."""

    return (
        UserMovieReview.query.filter_by(user_id=int(user_id), movie_id=int(movie_id))
        .order_by(UserMovieReview.date_reviewed.desc(), UserMovieReview.id.desc())
        .first()
    )


def _same_day_rerate(user_id, movie_id, rating):
    """A second star tap on the same calendar day corrects today's
    review in place — new stars, liked re-derived — instead of logging
    a rewatch; only the day rolling over makes the next tap a fresh
    diary entry (Glenn's rule, Aug 2026). Returns the edited row, or
    None when today has no review to edit."""

    row = _latest_review_row(user_id, movie_id)
    if (
        row is None
        or row.date_reviewed is None
        or row.date_reviewed.date() != date.today()
    ):
        return None
    for field, value in star_rating_fields(rating).items():
        setattr(row, field, value)
    row.liked = rating >= 3
    row.date_reviewed = datetime.now()
    return row


def _ladder_state(user_id, movie_id):
    """The star row's current verdict for a film as its JSON payload:
    the latest viewing's rating (the row the movie page displays),
    whether the not-interested flag is set, and — until the user's own
    STARS exist (unlogged films and bare unrated watches alike) — the
    engine's estimated rating, so removing a verdict repaints the row
    back to its estimate."""

    row = _latest_review_row(user_id, movie_id)
    flagged = (
        UserMovieStatus.query.filter_by(
            user_id=int(user_id), movie_id=int(movie_id), kind="not_interested"
        ).first()
        is not None
    )
    estimated = None
    if (row is None or row.rating is None) and not flagged:
        profile = stored_profile(current_app.redis, int(user_id))
        movie = db.session.get(Movie, int(movie_id))
        score = resolved_score(current_app.redis, int(user_id), movie, profile)
        if score is not None:
            estimated = estimated_rating(profile, score)
    return jsonify(
        {
            "rating": (
                float(row.rating)
                if row is not None and row.rating is not None
                else None
            ),
            "flagged": flagged,
            "estimated": estimated,
            # A rating or a ✕ clears the film's watchlist entry, so the
            # popover card (#45c) syncs its toggle from the same payload
            "on_watchlist": (
                UserWatchlist.query.filter_by(
                    user_id=int(user_id), movie_id=int(movie_id)
                ).first()
                is not None
            ),
        }
    )


def _watched_timestamp(watched_date):
    """A full DateTime for a date-only form value.

    Logging today's watch keeps the clock time so same-day viewings order
    correctly on the history page; past dates carry no time information
    and store midnight.
    """

    if watched_date is None:
        return None
    now = datetime.now()
    if watched_date == now.date():
        return now
    return datetime.combine(watched_date, datetime.min.time())


def admin_required(view):
    """Allow only admin users through; everyone else bounces to the home
    page. Stack under @login_required so anonymous visitors still get the
    login redirect."""

    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if not current_user.admin:
            flash("Need to be an admin user to view this page!", "danger")
            return redirect(url_for("main.index"))
        return view(*args, **kwargs)

    return wrapped_view


def _upgrade_threshold():
    """The quality preference below which a copy counts as upgradable."""

    return (
        db.session.query(RefQuality.preference)
        .filter(RefQuality.quality_title == "Bluray-1080p")
        .scalar()
        or 0
    )


def library_upgradable(movie, criterion=False):
    """Whether the film's best owned copy is worth upgrading — the
    shopping list's answer, which colors the In-library badge (amber
    = upgradable, green = settled) on the movie page and the poster
    popover; None when no main-feature file exists.

    The generic rule: a full-screen copy, or one below the app-wide
    threshold, is upgradable unless the film is excluded from the
    shopping list. criterion=True swaps in the Criterion catalog's
    settled rule (#77a, mirroring criterion_collection): the disc
    must be owned AND the copy must meet the release's own format,
    capped at the threshold — an owned disc with a Bluray-1080p file
    is settled even if Criterion re-released in 2160p.
    """

    best = (
        db.session.query(File, RefQuality)
        .join(RefQuality, RefQuality.id == File.quality_id)
        .filter(File.movie_id == movie.id)
        .filter(File.feature_type_id == None)
        .order_by(File.fullscreen.asc(), RefQuality.preference.desc())
        .first()
    )
    if best is None:
        return None
    file, quality = best
    threshold = _upgrade_threshold()
    if criterion:
        criterion_pref = (
            db.session.query(RefQuality.preference)
            .filter(RefQuality.id == movie.criterion_quality_id)
            .scalar()
            if movie.criterion_quality_id
            else None
        )
        target = min(criterion_pref or threshold, threshold)
        upgradable = bool(file.fullscreen) or quality.preference < target
        return not (bool(movie.criterion_disc_owned) and not upgradable)
    if movie.shopping_list_exclude:
        return False
    return bool(file.fullscreen) or quality.preference < threshold


def series_upgradable(series_ids):
    """Whether each TV series' library copy is worth upgrading — the
    series-shaped answer behind the In-library badge (#191): amber
    when any season still has an episode worth upgrading, green once
    every season is settled.

    Returns a dict keyed by series id; a series with no files is left
    out entirely, the way library_upgradable answers None for a film
    with no copy. The season rule is the TV library page's: rank each
    episode's copies, keep the best, and judge the season by its worst
    — physical-media seasons (DVD, SD/720p Blu-ray) are often the only
    release that will ever exist, so they never count as upgradable.
    """

    series_ids = [series_id for series_id in (series_ids or []) if series_id]
    if not series_ids:
        return {}

    # Each episode's best copy, with its quality's own preference and
    # physical flag on the same row — the season's worst is then picked
    # in Python off that row, rather than re-resolving a min(preference)
    # back to a RefQuality by value (#238: preference carries no unique
    # constraint, so a value join fans out if two tiers ever share one)

    ranked_files = (
        db.session.query(
            File.series_id.label("series_id"),
            File.season.label("season"),
            RefQuality.preference.label("preference"),
            RefQuality.physical_media.label("physical_media"),
            tv_file_rank(),
        )
        .join(TVSeries, (TVSeries.id == File.series_id))
        .join(RefQuality, (RefQuality.id == File.quality_id))
        .filter(File.series_id.in_(series_ids))
        .subquery()
    )

    worst = {}
    for series_id, season, preference, physical in db.session.query(
        ranked_files.c.series_id,
        ranked_files.c.season,
        ranked_files.c.preference,
        ranked_files.c.physical_media,
    ).filter(ranked_files.c.rank == 1):
        key = (series_id, season)
        held = worst.get(key)
        # Ties break toward the non-physical copy: if two episodes tie
        # at the season's worst and one of them CAN be upgraded, the
        # season still has an episode worth upgrading
        if (
            held is None
            or preference < held[0]
            or (preference == held[0] and not physical)
        ):
            worst[key] = (preference, physical)

    threshold = _upgrade_threshold()
    upgradable = {}
    for (series_id, _season), (preference, physical) in worst.items():
        season_upgradable = not physical and preference < threshold
        upgradable[series_id] = upgradable.get(series_id, False) or season_upgradable

    return upgradable


def tv_meta_line(first_year, last_year, seasons, episodes, genres):
    """The TV series meta line — run of years, size of the run, genres
    — shared by the series page and the popover card so both read the
    same. TMDB only fills the season and episode counts once a show has
    ended, so a running series simply shows fewer bits.
    """

    bits = []
    if first_year:
        years = str(first_year)
        if last_year and last_year != first_year:
            years += f"–{last_year}"
        bits.append(years)
    if seasons:
        bits.append(
            f"{seasons} season{'s' if seasons != 1 else ''}, "
            f"{episodes} episode{'s' if episodes != 1 else ''}"
        )
    if genres:
        bits.append(", ".join(genres))
    return " · ".join(bits)
