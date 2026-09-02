"""Shared helpers for the route modules of the main blueprint.

This is slice f of the routes.py split. It holds the verdict and ladder
plumbing that every rating surface uses, the admin gate, and the read of
the quality threshold."""

from datetime import date, datetime

from flask import (
    current_app,
    jsonify,
    flash,
    redirect,
    url_for,
    request,
)

# Flask 2.4 removed flask.Markup. Import it from its real home.
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
    """Enqueue a recompute that puts new ratings into the stored profile.

    The recompute occurs within minutes. The user does not wait for the
    1:45 AM run. The Redis key is set with NX. Thus, a rating session
    enqueues a maximum of 1 recompute per 5 minutes."""

    if current_app.redis.set(
        f"fitzflix:elicit:recompute:{int(current_user.id)}", "1", nx=True, ex=300
    ):
        current_app.maintenance_queue.enqueue(
            "app.recommendations.recompute_recommendations",
            job_timeout="1h",
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
    This function returns the edited row. It returns None if today has
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
    return row


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
    return jsonify(
        {
            "rating": (
                float(row.rating)
                if row is not None and row.rating is not None
                else None
            ),
            "flagged": flagged,
            "estimated": estimated,
            # A rating or a ✕ removes the watchlist entry of the film. Thus,
            # the popover card (#45c) syncs its toggle from the same payload.
            "on_watchlist": (
                UserWatchlist.query.filter_by(
                    user_id=int(user_id), movie_id=int(movie_id)
                ).first()
                is not None
            ),
        }
    )


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

    return (
        db.session.query(RefQuality.preference)
        .filter(RefQuality.quality_title == "Bluray-1080p")
        .scalar()
        or 0
    )


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
        season_upgradable = not physical and preference < threshold
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
