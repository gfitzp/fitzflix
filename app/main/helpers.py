"""Shared helpers for the route modules of the main blueprint.

This is slice f of the routes.py split. It holds the verdict and ladder
plumbing that every rating surface uses, the admin gate, and the read of
the quality threshold."""

from datetime import date, datetime, timedelta

from flask import (
    after_this_request,
    current_app,
    jsonify,
    flash,
    redirect,
    url_for,
    request,
)

# Flask 2.4 removed flask.Markup. Import it from its real home.
from flask_login import current_user

from app.tasks import enqueue_task
from app import db
from app.models import (
    File,
    Movie,
    RefQuality,
    TVSeries,
    UserMovieReview,
    UserMovieStatus,
    UserWatchlist,
)
from app.file_ranking import (
    MOVIE_BEST_ORDER,
    copy_upgradable,
    criterion_settled,
    tv_file_rank,
    upgrade_threshold,
)
from app.elicitation import set_last_response
from app.recommendations import (
    RECOMPUTE_QUEUED_KEY,
    RECOMPUTE_QUEUED_SECONDS,
    RECOMPUTE_TRAILING_KEY,
    estimated_rating,
    resolved_score,
    stored_profile,
)
from app.videos import (
    clear_not_interested,
    clear_watchlist,
    star_rating_fields,
)

from collections import namedtuple
from functools import wraps


def decimal_id(value):
    """Return the integer of a decimal string, or None.

    This is the one parser for an id or a count that comes from a form
    field, a query string or an API payload. The test is isdecimal, not
    isdigit: "²" passes isdigit(), and int() refuses it (#307). None, a
    blank value and a value with other characters give None."""

    text = str(value if value is not None else "").strip()
    return int(text) if text.isdecimal() else None


def decimal_ids(values):
    """Return the integer ids in a comma-separated string or a list.

    A part that is not a decimal string is skipped. The order of the
    input is kept."""

    if isinstance(values, str) or values is None:
        values = (values or "").split(",")
    ids = (decimal_id(value) for value in values)
    return [value for value in ids if value is not None]


def _enqueue_profile_recompute():
    """Enqueue a recompute that puts new ratings into the stored profile.

    The recompute occurs within minutes. The user does not wait for the
    1:45 AM run. The Redis key is set with NX. Thus, a rating session
    enqueues a maximum of 1 recompute per 5 minutes. A rating inside the
    5 minutes books 1 trailing recompute at their end. Without it, the
    later ratings of a session waited for the nightly run."""

    user_id = int(current_user.id)
    if current_app.redis.set(
        RECOMPUTE_QUEUED_KEY.format(user_id=user_id),
        "1",
        nx=True,
        ex=RECOMPUTE_QUEUED_SECONDS,
    ):
        enqueue_task(
            "app.recommendations.recompute_recommendations",
            description="Recomputing film recommendations",
        )
    elif current_app.redis.set(
        RECOMPUTE_TRAILING_KEY.format(user_id=user_id),
        "1",
        nx=True,
        ex=RECOMPUTE_QUEUED_SECONDS,
    ):
        enqueue_task(
            "app.recommendations.recompute_recommendations",
            delay=timedelta(seconds=300),
            description="Recomputing film recommendations",
        )


def _quick_rating():
    """Return (present, rating) from the submission of the quick-answer ladder.

    The result is (False, None) if the user pressed no ladder button. It
    is (True, None) if the value is not valid. Otherwise it is (True,
    0.0 to 5.0). A 0 is the ✕ button. It means "not interested, never
    saw it". The handlers route a 0 to the status-flag path, never to a
    review. The star scale starts at 1 ("Hated it"). It applies to seen
    films only.
    """

    value = (request.form.get("quick_rating") or "").strip()
    if not value:
        return False, None
    if value not in {"0", "1", "2", "3", "4", "5"}:
        return True, None
    return True, float(value)


def _mark_not_interested(user_id, movie_id):
    """Flag a film as not-interested and remove its watchlist entry.

    This function commits the session. It returns False and writes
    nothing if the user has a diary row for the film. The ✕ button means
    "never saw it". The lowest verdict for a seen film is 1 star."""

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
    """Return True if the quick-rating post came from the background fetch.

    The background fetch of the star row wants JSON state back, not a
    redirect. A flash message would only wait unseen for a later page
    load."""

    return request.headers.get("X-Requested-With") == "ladder"


def _card_fetch():
    """Return True if the post came from the card of the poster popover (#45c).

    The watchlist toggle of the card wants compact JSON state back, never
    a redirect. A flash message would wait unseen."""

    return request.headers.get("X-Requested-With") == "card"


def _latest_review_row(user_id, movie_id):
    """Return the diary row whose verdict the star widget shows.

    The newest review comes first. Bare watches come last. The newest id
    breaks ties. The latest_ratings() function of the engine uses the
    same order. Thus, the page shows exactly what the profile scores."""

    return (
        UserMovieReview.query.filter_by(user_id=int(user_id), movie_id=int(movie_id))
        .order_by(UserMovieReview.date_reviewed.desc(), UserMovieReview.id.desc())
        .first()
    )


def _same_day_rerate(user_id, movie_id, rating):
    """Correct the review of today in place after a second star tap.

    A second star tap on the same calendar day does not log a rewatch.
    It writes the new stars and derives liked again. Only a new day
    makes the next tap a new diary entry (rule from Glenn, 2026-08).
    The edit stamps date_updated for the export. This function returns
    the edited row. It returns None if today has
    no review to edit."""

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

    # The incremental Letterboxd export selects edited rows by this time.
    # An export earlier today already sent the old stars.

    row.date_updated = row.date_reviewed
    return row


def _letterboxd_locked(user_id, movie_id, rating, removes, corrects):
    """Return the latest row if a star tap would change a Letterboxd row.

    A row from the feed sync is locked, as in the History editor. The
    next poll would put the Letterboxd verdict back. Outside the feed
    window, the local change would differ from Letterboxd forever. A tap
    on the current stars (removes) and a correction on the same day
    (corrects) change the row in place. Thus, they are refused. A
    rating on a later day is a new log, and it stays allowed. This
    returns None if the tap can go on."""

    if rating is None:
        return None
    row = _latest_review_row(user_id, movie_id)
    if row is None or not row.letterboxd_guid:
        return None
    if removes and row.rating is not None and float(row.rating) == rating:
        return row
    if (
        corrects
        and row.date_reviewed is not None
        and row.date_reviewed.date() == date.today()
    ):
        return row
    return None


# The longest time that one star-row post holds the verdict lock of a film,
# in seconds. A second post waits for half of this time at most.

VERDICT_LOCK_SECONDS = 10
VERDICT_LOCK_KEY = "fitzflix:verdict-lock:{user_id}:{name}"


def _hold_verdict_lock(name):
    """Make the star-row posts of 1 user and 1 film run 1 at a time (#337).

    2 posts that run at the same time both read the latest review row
    before either commits. Thus, a double tap could write 2 rows for 1
    gesture. The second post waits here until the first post sends its
    answer. Then it sees the committed row. The lock goes when the
    response goes. If Redis fails, the post runs without the lock."""

    lock = current_app.redis.lock(
        VERDICT_LOCK_KEY.format(user_id=int(current_user.id), name=name),
        timeout=VERDICT_LOCK_SECONDS,
        blocking_timeout=VERDICT_LOCK_SECONDS / 2,
    )
    try:
        if not lock.acquire():
            return False
    except Exception:
        current_app.logger.warning(f"Verdict lock {name} not available")
        return False

    @after_this_request
    def release(response):
        try:
            lock.release()
        except Exception:
            pass
        return response

    return True


# The keys of every star-row state. The page script reads them from the
# live ladder post, the batched hydration, the history row edit and the
# Criterion 24/7 card. One builder keeps the 4 payloads in step (#335).
LADDER_KEYS = frozenset(
    ["rating", "has_review", "flagged", "estimated", "on_watchlist"]
)


def ladder_payload(row, flagged, estimated, on_watchlist, **extra):
    """Return the state of a star row as a dict.

    row is the diary row that the row shows, or None. The rating is the
    float of its stars, or None for a bare watch. The ✕ hides when the
    film has a diary row, so has_review follows the row (#335). The
    extra keys (folds, upgradable, movie_id) belong to one surface each
    and come after the shared keys."""

    return {
        "rating": (
            float(row.rating) if row is not None and row.rating is not None else None
        ),
        "has_review": row is not None,
        "flagged": bool(flagged),
        "estimated": estimated,
        # A rating or a ✕ removes the watchlist entry of the film. Thus,
        # the popover card (#45c) syncs its toggle from the same payload.
        "on_watchlist": bool(on_watchlist),
        **extra,
    }


def _ladder_state(user_id, movie_id):
    """Return the current verdict of the star row for a film as JSON.

    The payload holds the rating of the latest viewing (the row that the
    movie page shows) and the state of the not-interested flag. It also
    holds the estimated rating from the engine until the user has STARS
    of their own. This applies to unlogged films and to bare unrated
    watches. Thus, when the user removes a verdict, the row shows the
    estimate again."""

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
    on_watchlist = (
        UserWatchlist.query.filter_by(
            user_id=int(user_id), movie_id=int(movie_id)
        ).first()
        is not None
    )
    return jsonify(ladder_payload(row, flagged, estimated, on_watchlist))


# The result of apply_verdict. kind is "unflagged", "flagged",
# "flag_refused", "locked", "removed", "corrected" or "logged". rating is
# the rating of the post. is_review is True for a log with stars or text.

Outcome = namedtuple("Outcome", "kind rating is_review")


class VerdictPost(
    namedtuple("VerdictPost", "user_id rating review_text watched_date quick_present")
):
    """1 verdict post of the review form: the ladder tap, the text, the date.

    rating is None for a post without a tap, 0 for the ✕, or 1.0 to
    5.0. quick_present is True if the post has a ladder tap."""

    @property
    def invalid(self):
        """Return True if the post has a ladder tap that is not a rating."""

        return self.quick_present and self.rating is None


def _verdict_post(form):
    """Return the VerdictPost of a valid post of a review form, or None.

    The result is None if the user posted neither the form nor a ladder
    tap, or if the form does not validate. The movie page, the TMDB log
    page, and the review editor read their posts with this function."""

    quick_present, quick_rating = _quick_rating()
    if not (form.review_submit.data or quick_present) or not form.validate_on_submit():
        return None
    return VerdictPost(
        int(current_user.id),
        quick_rating,
        form.review.data,
        form.date_watched.data,
        quick_present,
    )


def refused_verdict(form, back_url):
    """Return the response to a review post that did not validate, or None.

    The result is None if the user posted neither the form nor a ladder
    tap. A background post gets a JSON error. Thus, the page falls back
    to a normal submission. A normal submission flashes each field
    error and goes back to the page. Without this, the page renders
    again with no row and no message, and the user thinks that the
    viewing is logged."""

    quick_present, _ = _quick_rating()
    if request.method != "POST" or not (form.review_submit.data or quick_present):
        return None
    if request.headers.get("X-Requested-With"):
        return jsonify({"error": "not valid", "fields": sorted(form.errors)}), 400
    if "csrf_token" in form.errors:
        flash("The page was out of date. Load it again and try again.", "warning")
    flash_form_errors(form)
    return redirect(back_url)


# The rules of a verdict on 1 page. allow_unflag lets a ✕ on a flagged
# film remove the flag. allow_remove lets a tap on the current rating
# remove it. steer and steer_flag say if a rating and a ✕ move the
# last-response state of the drive.

VerdictPolicy = namedtuple(
    "VerdictPolicy", "allow_unflag allow_remove steer steer_flag"
)


def MOVIE_PAGE_POLICY(steer):
    """Return the verdict rules of the movie page. Its ✕ steers like its stars."""

    return VerdictPolicy(
        allow_unflag=True, allow_remove=True, steer=steer, steer_flag=steer
    )


def TMDB_PAGE_POLICY(steer):
    """Return the verdict rules of the TMDB log page.

    The page never removes a rating and never undoes a ✕. Its ✕ never
    steers the drive."""

    return VerdictPolicy(
        allow_unflag=False, allow_remove=False, steer=steer, steer_flag=False
    )


def apply_verdict(post, target, policy):
    """Apply 1 VerdictPost on a film to the diary under a VerdictPolicy. Return an Outcome.

    This is the database work of the movie page and of the TMDB log page.
    A rating of 0 is the ✕: the not-interested flag, never a review. A
    seen film cannot get the flag. With allow_unflag, a ✕ on a flagged
    film removes the flag. A Letterboxd row cannot lose its stars or
    change them in place. With allow_remove, a tap on the current rating
    removes it. A bare star tap on a day that already has a review
    corrects that review. Anything else logs a new viewing. The changes
    are committed."""

    user_id, rating, review_text, watched_date, quick_present = post
    allow_unflag, allow_remove, steer, steer_flag = policy
    text = (review_text or "").strip()
    if rating == 0:
        return _apply_flag(user_id, target, allow_unflag, steer_flag)

    bare_tap = (
        quick_present and rating is not None and not text and watched_date is None
    )
    if _letterboxd_locked(
        user_id, target.id, rating, removes=allow_remove, corrects=bare_tap
    ):
        return Outcome("locked", rating, False)

    # A tap on your current rating removes it. A bare drive-style row (no
    # watch date, no text) is deleted completely. A viewing with real
    # history only loses its stars.

    if allow_remove and rating is not None:
        current_row = _latest_review_row(user_id, target.id)
        if (
            current_row is not None
            and current_row.rating is not None
            and float(current_row.rating) == rating
        ):
            bare = (
                current_row.date_watched is None
                and not (current_row.review or "").strip()
            )
            if bare:
                db.session.delete(current_row)
            else:
                for field, value in star_rating_fields(None).items():
                    setattr(current_row, field, value)
                current_row.liked = False
                current_row.date_updated = datetime.now()

                # A watched row with no stars and no text is a bare watch
                # again, the same as on the History page. A review date
                # would make it outrank an older review.

                if not (current_row.review or "").strip():
                    current_row.date_reviewed = None
            db.session.commit()
            _enqueue_profile_recompute()
            return Outcome("removed", rating, False)

    # A different star on a day that you already reviewed corrects that
    # review in place. Tastes change, but not 2 times in a day. A form
    # with text or a watch date is a real new log. It skips this.

    if bare_tap:
        edited = _same_day_rerate(user_id, target.id, rating)
        if edited is not None:
            clear_watchlist(user_id, target.id)
            clear_not_interested(user_id, target.id)
            db.session.commit()
            _steer_and_recompute(user_id, target, rating, steer)
            return Outcome("corrected", rating, True)

    # A bare submission (no rating or text) is a plain diary entry. It is
    # a watch, not a review. Thus, it has no review date. The rewatch flag
    # computes in the same way as for Plex watches. Any earlier row for
    # this user and film makes this a repeat viewing.

    is_review = bool(rating is not None or text)
    rewatch = (
        db.session.query(UserMovieReview.id)
        .filter_by(user_id=user_id, movie_id=target.id)
        .first()
        is not None
    )
    db.session.add(
        UserMovieReview(
            user_id=user_id,
            movie_id=target.id,
            review=review_text,
            liked=rating is not None and rating >= 3,
            date_watched=_watched_timestamp(watched_date),
            date_reviewed=datetime.now() if is_review else None,
            rewatch=rewatch,
            **star_rating_fields(rating),
        )
    )
    clear_watchlist(user_id, target.id)
    clear_not_interested(user_id, target.id)
    db.session.commit()
    if rating is not None:
        _steer_and_recompute(user_id, target, rating, steer)
    return Outcome("logged", rating, is_review)


def _apply_flag(user_id, target, allow_unflag, steer_flag):
    """Apply a ✕ post. Return an Outcome of "unflagged", "flagged", or "flag_refused"."""

    existing_flag = UserMovieStatus.query.filter_by(
        user_id=int(user_id), movie_id=target.id, kind="not_interested"
    ).first()
    if allow_unflag and existing_flag is not None:
        db.session.delete(existing_flag)
        db.session.commit()
        _enqueue_profile_recompute()
        return Outcome("unflagged", 0.0, False)
    if _mark_not_interested(user_id, target.id):
        if steer_flag:
            set_last_response(current_app.redis, user_id, target.id, "not_interested")
        _enqueue_profile_recompute()
        return Outcome("flagged", 0.0, False)
    return Outcome("flag_refused", 0.0, False)


def _steer_and_recompute(user_id, target, rating, steer):
    """Move the last-response state of the drive, if steer, and queue a recompute.

    This is the same state that the rating drive keeps. A positive
    rating shows the "since you liked…" strip on the movie page. It also
    steers the next card of the drive."""

    if steer:
        set_last_response(
            current_app.redis, user_id, target.id, "rated", positive=rating >= 3
        )
    _enqueue_profile_recompute()


# The flash of each verdict that is not a background ladder post. name
# is the title of the film.

VERDICT_MESSAGES = {
    "unflagged": ("'{name}' can be recommended again", "success"),
    "flagged": ("Fitzflix will not recommend '{name}'", "info"),
    "flag_refused": (
        "You logged '{name}'. The lowest rating for a seen film is 1 star",
        "warning",
    ),
    "locked": (
        "'{name}' syncs from your Letterboxd account. "
        "Change it on Letterboxd instead.",
        "info",
    ),
    "removed": ("Removed your rating of '{name}'", "success"),
    "rated": ("Rated '{name}' {rating:g} out of 5 stars", "success"),
    "reviewed": ("Logged review for '{name}'", "success"),
    "watched": ("Logged '{name}' in your history", "success"),
    "watched_undated": (
        "Marked '{name}' as seen. Add a date to show it in your history.",
        "success",
    ),
}


def verdict_response(outcome, target, name, fallback_url, messages=None):
    """Answer a verdict post. A ladder fetch gets the star-row state as JSON.

    Any other post gets a flash and a redirect to fallback_url. messages
    replaces entries of VERDICT_MESSAGES for 1 page."""

    if _ladder_fetch():
        return _ladder_state(current_user.id, target.id)
    key = outcome.kind
    if key in ("corrected", "logged"):
        if outcome.rating is not None:
            key = "rated"
        elif outcome.is_review:
            key = "reviewed"
        else:
            key = "watched"

            # A bare watch without a date is a preference signal. The
            # History page lists dated rows only. Thus, the message must
            # not say "in your history" (#421).

            if not (request.form.get("date_watched") or "").strip():
                key = "watched_undated"
    template, category = {**VERDICT_MESSAGES, **(messages or {})}[key]
    flash(template.format(name=name, rating=outcome.rating), category)
    return redirect(fallback_url)


def apply_row_edit(row, quick_rating, review_text, watched_date, posted):
    """Edit 1 logged viewing in place and commit. This is the database work of review_edit.

    The verdict posts of the movie page and the TMDB log page log new
    viewings through apply_verdict. This edits the given row only.
    posted holds the names of the fields that arrived in the post. A
    field that did not arrive stays as it is."""

    # Only a ladder tap changes the stars and the liked flag that
    # follows them. A saved text or date edit must never delete the
    # existing rating of the viewing. A tap on the CURRENT rating
    # clears the stars instead. The viewing itself stays. This never
    # deletes an explicit diary entry that the user edits.

    # Each change stamps date_updated. The incremental Letterboxd
    # export selects the changed rows by it. The first stars or the
    # first text of a bare watch also set date_reviewed. The movie
    # page and the engine select the current verdict by that date.

    changed = False
    if quick_rating is not None:
        changed = True
        if row.rating is not None and float(row.rating) == float(quick_rating):
            for field, value in star_rating_fields(None).items():
                setattr(row, field, value)
            row.liked = False

            # A row with no stars and no text is a bare watch again.
            # A review date would make it outrank an older review.

            if not (row.review or "").strip():
                row.date_reviewed = None
        else:
            for field, value in star_rating_fields(quick_rating).items():
                setattr(row, field, value)
            row.liked = quick_rating >= 3
            if row.date_reviewed is None:
                row.date_reviewed = datetime.now()

    # The date and the text change only if their fields ARRIVED IN
    # THE POST. The per-row forms of the history page and the
    # star-only ladder taps carry no date field. An absent field
    # must never read as "clear the watch date".

    if "date_watched" in posted:
        # The date-only form field cannot improve a stored timestamp
        # (for example, the actual clock time of a Plex watch).
        # Thus, replace the value only if the calendar date changed.
        new_date = watched_date
        if new_date is None:
            changed = changed or row.date_watched is not None
            row.date_watched = None
        elif row.date_watched is None or row.date_watched.date() != new_date:
            row.date_watched = _watched_timestamp(new_date)
            changed = True

    # A text change on a row that already has a review keeps the
    # original review date. A first review (no date_reviewed yet)
    # sets the review date.

    if "review" in posted:
        new_text = review_text or ""
        if new_text != (row.review or ""):
            row.review = new_text
            changed = True
            if not row.date_reviewed:
                row.date_reviewed = datetime.now()

    # A star change is a new verdict, as on the movie page. Thus, it
    # queues the profile recompute. Without the recompute, the old stars
    # stay in each estimate until the nightly run. An edit of the latest
    # viewing also removes a stray not-interested flag, and it completes
    # a watchlist entry from before that viewing. An edit of an older
    # viewing, or an entry added after the viewing, is a wish to watch
    # the film again. That entry stays.

    if quick_rating is not None and _is_latest_viewing(row):
        clear_not_interested(row.user_id, row.movie_id)
        if row.date_watched is not None:
            UserWatchlist.query.filter(
                UserWatchlist.user_id == row.user_id,
                UserWatchlist.movie_id == row.movie_id,
                UserWatchlist.date_added <= row.date_watched,
            ).delete()
    if changed:
        row.date_updated = datetime.now()
    db.session.commit()
    if quick_rating is not None:
        _enqueue_profile_recompute()


def _is_latest_viewing(row):
    """Return True if no other diary row of the film has a later watch date.

    A row without a date is the latest only if the film has no dated
    row."""

    later = UserMovieReview.query.filter(
        UserMovieReview.user_id == row.user_id,
        UserMovieReview.movie_id == row.movie_id,
        UserMovieReview.id != row.id,
    )
    if row.date_watched is None:
        later = later.filter(UserMovieReview.date_watched.isnot(None))
    else:
        later = later.filter(UserMovieReview.date_watched > row.date_watched)
    return later.first() is None


def _watched_timestamp(watched_date):
    """Return a full DateTime for a date-only form value.

    A watch logged for today keeps the clock time. Thus, the viewings of
    the same day sort correctly on the history page. A past date carries
    no time information and stores midnight.
    """

    if watched_date is None:
        return None
    now = datetime.now()
    if watched_date == now.date():
        return now
    return datetime.combine(watched_date, datetime.min.time())


def flash_form_errors(form):
    """Flash the first error of each field of a form that failed validation.

    The pages do not show the field errors of these forms. Thus, a
    refused field gets a message. A csrf error stays quiet, as a form
    that did not validate always did."""

    for field, messages in form.errors.items():
        if field != "csrf_token":
            flash(f"{getattr(form, field).label.text}: {messages[0]}", "danger")


def admin_required(view):
    """Allow only admin users through the view.

    All other users go to the home page. Stack this decorator under
    @login_required. Then an anonymous visitor still gets the login
    redirect."""

    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if not current_user.admin:
            flash("You must be an admin user to view this page.", "danger")
            return redirect(url_for("main.index"))
        return view(*args, **kwargs)

    return wrapped_view


def _upgrade_threshold():
    """Return the quality preference below which a copy is upgradable."""

    return upgrade_threshold()


def library_upgradable(movie, criterion=False):
    """Return True if the best owned copy of the film is worth an upgrade.

    This is the answer of the shopping list. It colors the In-library
    badge on the movie page and the poster popover (amber = upgradable,
    green = settled). The result is None if no main-feature file exists.

    The generic rule: a full-screen copy, or a copy below the app-wide
    threshold, is upgradable unless the shopping list excludes the film.
    criterion=True uses the settled rule of the Criterion catalog (#77a,
    the same as criterion_collection). The disc must be owned AND the
    copy must satisfy the format of the release, capped at the threshold.
    An owned disc with a Bluray-1080p file is settled even if Criterion
    released the film again in 2160p.
    """

    best = (
        db.session.query(File, RefQuality)
        .join(RefQuality, RefQuality.id == File.quality_id)
        .filter(File.movie_id == movie.id)
        .filter(File.feature_type_id == None)
        .order_by(*MOVIE_BEST_ORDER)
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
        return not criterion_settled(
            file, quality, criterion_pref, threshold, movie.criterion_disc_owned
        )
    if movie.shopping_list_exclude:
        return False
    return copy_upgradable(file.fullscreen, quality.preference, threshold)


def series_upgradable(series_ids):
    """Return, for each TV series, if its library copy is worth an upgrade.

    This is the series-shaped answer behind the In-library badge (#191).
    The badge is amber if a season still has an episode worth an upgrade.
    It is green when every season is settled.

    The result is a dict keyed by series id. A series with no files is
    not in the dict. library_upgradable does the same. It answers None
    for a film with no copy. The season rule is the rule of
    the TV library page. Rank the copies of each episode, keep the best
    copy, and judge the season by its worst episode. A physical-media
    season (DVD, SD/720p Blu-ray) is often the only release that will
    exist. Thus, it never counts as upgradable.
    """

    series_ids = [series_id for series_id in (series_ids or []) if series_id]
    if not series_ids:
        return {}

    # Get the best copy of each episode. The preference and the physical
    # flag of its quality are on the same row. Python then selects the
    # worst episode of the season from that row. It does not resolve a
    # min(preference) back to a RefQuality by value (#238: preference has
    # no unique constraint. Thus, a value join fans out if 2 tiers share
    # one value).

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
        # A tie goes to the non-physical copy. 2 episodes can tie as the
        # worst of the season. If one of them CAN be upgraded, the season
        # still has an episode worth an upgrade.
        if (
            held is None
            or preference < held[0]
            or (preference == held[0] and not physical)
        ):
            worst[key] = (preference, physical)

    threshold = _upgrade_threshold()
    upgradable = {}
    for (series_id, _season), (preference, physical) in worst.items():
        season_upgradable = copy_upgradable(
            False, preference, threshold, physical=physical
        )
        upgradable[series_id] = upgradable.get(series_id, False) or season_upgradable

    return upgradable


def tv_meta_line(first_year, last_year, seasons, episodes, genres):
    """Return the meta line of a TV series.

    The line holds the run of years, the size of the run, and the genres.
    The series page and the popover card share it. Thus, both read the
    same. TMDB fills the season and episode counts only after a show has
    ended. Thus, a running series shows fewer parts.
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


# A span longer than this is a damaged episode number, not a real
# multi-episode file. It counts as its first episode only. The TV shopping
# list uses the same rule.

MAX_EPISODE_SPAN = 500


def season_episode_counts(series_ids=None):
    """Return {(series_id, season): number of owned episodes} (#330).

    The count is the number of distinct episode numbers that the files of
    the season hold. A multi-episode file (S04E17-E22) counts each episode
    in its span. A count of distinct File.episode values counts it as one.
    series_ids limits the query to those series."""

    query = db.session.query(
        File.series_id, File.season, File.episode, File.last_episode
    ).filter(File.series_id.isnot(None))
    if series_ids is not None:
        query = query.filter(File.series_id.in_(list(series_ids)))

    # A file with no episode number puts its season in the result with
    # no episodes

    numbers = {}
    for series_id, season, episode, last_episode in query.all():
        held = numbers.setdefault((series_id, season), set())
        if episode is None:
            continue
        last = last_episode if last_episode is not None else episode
        if last < episode or last - episode > MAX_EPISODE_SPAN:
            last = episode
        held.update(range(episode, last + 1))
    return {key: len(value) for key, value in numbers.items()}
