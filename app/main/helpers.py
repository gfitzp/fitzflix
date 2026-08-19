"""Shared helpers for the main blueprint's route modules (#17's
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
    RefQuality,
    UserMovieReview,
    UserMovieStatus,
    UserWatchlist,
)
from app.recommendations import (
    estimated_rating,
    stored_profile,
    stored_scores,
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
    status-flag path, never to a review (#51): the star scale itself
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
    harshest verdict is 1 star (#51)."""

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
    background fetch (#54) — it wants JSON state back instead of a
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
    whether the not-interested flag is set, and — for UNLOGGED,
    unflagged films — the engine's estimated rating, so removing a
    verdict repaints the row back to its estimate (#58)."""

    row = _latest_review_row(user_id, movie_id)
    flagged = (
        UserMovieStatus.query.filter_by(
            user_id=int(user_id), movie_id=int(movie_id), kind="not_interested"
        ).first()
        is not None
    )
    estimated = None
    if row is None and not flagged:
        score = stored_scores(current_app.redis, int(user_id)).get(int(movie_id))
        if score is not None:
            estimated = estimated_rating(
                stored_profile(current_app.redis, int(user_id)), score
            )
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
