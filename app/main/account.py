"""The user's own pages (the routes.py split): the viewing history with
its per-row editors, review editing, and the profile."""

import csv
import io
import json
import re
import secrets


from datetime import datetime

from flask import (
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
from sqlalchemy.orm import contains_eager

from app import db
from app.main.forms import (
    AvailabilityAlertsForm,
    DefaultPlayerForm,
    EditProfileForm,
    InfusePinForm,
    InfusePlayerForm,
    MovieReviewForm,
    ReviewExportForm,
    ReviewUploadForm,
    LetterboxdUsernameForm,
    PlexPlayerForm,
    PlexUsernameForm,
    ResetFrameScoresForm,
    StreamingProvidersForm,
    UpdateAPIKeyForm,
)
from app.infuse_player import (
    COMPANION_PORT,
    pairing_outcome,
    pairing_pending,
    start_pairing,
    submit_pin,
)
from app.plex_player import probe_player, remote_playback_configured
from app.models import (
    Movie,
    User,
    UserFrameScore,
    UserMovieReview,
    UserStreamingProvider,
)
from app.main import bp
from app.main.helpers import _ladder_fetch, _quick_rating, _watched_timestamp
from app.recommendations import (
    estimated_rating,
    resolved_score,
    stored_profile,
    stored_scores,
)
from app.email import send_email
from app.streaming import (
    provider_registry,
)
from app.videos import (
    parse_letterboxd_export,
    star_rating_fields,
)


@bp.route("/review/<int:review_id>/edit", methods=["GET", "POST"])
@login_required
def review_edit(review_id):
    """Add or edit the review on one logged viewing.

    Each viewing — a Letterboxd import row, a Plex watch, or a manual log
    from the movie page — is its own row; this edits that row in place,
    unlike the movie page's form, which always logs a new viewing.
    """

    user_review = UserMovieReview.query.filter_by(
        id=review_id, user_id=current_user.id
    ).first_or_404()
    movie = user_review.movie
    title = f"{movie.tmdb_title if movie.tmdb_title else movie.title} ({movie.tmdb_release_date.strftime('%Y') if movie.tmdb_title else movie.year})"

    # The history page is paginated; its per-row forms and Edit-date
    # links carry the page so every redirect lands back where the row
    # lives instead of on page 1

    page = request.args.get("page", None, type=int)

    # Feed-originated rows sync FROM Letterboxd: while the entry
    # stays in the feed window, every poll re-asserts Letterboxd's
    # rating, like, and text over local edits — so this editor refuses
    # guid rows outright rather than accepting changes that revert

    if user_review.letterboxd_guid:
        flash(
            f"'{title}' syncs from your Letterboxd account — edit it on "
            f"Letterboxd instead",
            "info",
        )
        return redirect(url_for("main.history", page=page))

    movie_review_form = MovieReviewForm()
    quick_present, quick_rating = _quick_rating()
    if (
        movie_review_form.review_submit.data or quick_present
    ) and movie_review_form.validate_on_submit():
        if quick_present and quick_rating is None:
            flash("That rating didn't make sense", "warning")
            return redirect(url_for("main.review_edit", review_id=review_id, page=page))
        # A logged viewing can't be "not interested" — the ladder
        # hides its ✕ here, and a stray 0 is refused rather than stored

        if quick_rating == 0:
            flash(
                f"You've logged '{title}' — the lowest rating for a "
                f"seen film is 1 star",
                "warning",
            )
            return redirect(url_for("main.review_edit", review_id=review_id, page=page))
        # Only a ladder tap changes the stars (and the liked flag that
        # follows them) — saving a text or date edit must never wipe
        # the viewing's existing rating. Tapping the CURRENT rating
        # clears the stars instead: the viewing itself stays, an
        # explicit diary entry being edited is never deleted here

        if quick_rating is not None:
            if user_review.rating is not None and float(user_review.rating) == float(
                quick_rating
            ):
                for field, value in star_rating_fields(None).items():
                    setattr(user_review, field, value)
                user_review.liked = False
            else:
                for field, value in star_rating_fields(quick_rating).items():
                    setattr(user_review, field, value)
                user_review.liked = quick_rating >= 3

        # The date and text only change when their fields actually
        # RODE IN THE POST — the history page's per-row forms and
        # star-only ladder taps carry no date field, and an absent
        # field must never read as "clear the watch date"

        if "date_watched" in request.form:
            # The date-only form field can't improve on a stored
            # timestamp (e.g. a Plex watch's actual clock time), so only
            # replace the value when the calendar date itself changed
            new_date = movie_review_form.date_watched.data
            if new_date is None:
                user_review.date_watched = None
            elif (
                user_review.date_watched is None
                or user_review.date_watched.date() != new_date
            ):
                user_review.date_watched = _watched_timestamp(new_date)

        # Text changes on a row that was already reviewed keep the original
        # review date and stamp date_updated instead; a first review (no
        # date_reviewed yet) sets the review date

        if "review" in request.form:
            new_text = movie_review_form.review.data or ""
            if new_text != (user_review.review or ""):
                user_review.review = new_text
                if user_review.date_reviewed:
                    user_review.date_updated = datetime.now()
                else:
                    user_review.date_reviewed = datetime.now()

        db.session.commit()
        if _ladder_fetch():
            # This page edits ONE viewing, so the row's state comes from
            # that row — not the latest-viewing lookup the movie page
            # uses. Clearing the stars repaints the row back to the
            # engine's estimate (the universal-star-row rule, extended to bare watches)
            estimated = None
            if user_review.rating is None:
                profile = stored_profile(current_app.redis, current_user.id)
                score = resolved_score(
                    current_app.redis, current_user.id, movie, profile
                )
                if score is not None:
                    estimated = estimated_rating(profile, score)
            return jsonify(
                {
                    "rating": (
                        float(user_review.rating)
                        if user_review.rating is not None
                        else None
                    ),
                    "flagged": False,
                    "estimated": estimated,
                }
            )
        flash(f"Updated your review of '{title}'", "success")
        return redirect(url_for("main.history", page=page))

    if request.method == "GET":
        movie_review_form = MovieReviewForm(
            review=user_review.review,
            date_watched=(
                user_review.date_watched.date() if user_review.date_watched else None
            ),
        )

    estimated = None
    if user_review.rating is None:
        profile = stored_profile(current_app.redis, current_user.id)
        score = resolved_score(current_app.redis, current_user.id, movie, profile)
        if score is not None:
            estimated = estimated_rating(profile, score)

    return render_template(
        "review_edit.html",
        title=f'Edit review for "{title}"',
        movie=movie,
        user_review=user_review,
        movie_review_form=movie_review_form,
        page=page,
        estimated=estimated,
    )


def _review_export_response(review_export_form):
    """The review-export POST: build the Letterboxd-format CSV and email
    it to the user. Lives on the Profile page since #215 (History held
    the form before that). Returns a response once the form has
    submitted, None otherwise."""

    if not (
        review_export_form.export_submit.data
        and review_export_form.validate_on_submit()
    ):
        return None

    # Create the header columns for the CSV, per the Letterboxd import
    # format (https://letterboxd.com/about/importing-data/)

    csv_export = [
        [
            "tmdbID",
            "imdbID",
            "Title",
            "Year",
            "Rating",
            "WatchedDate",
            "Rewatch",
            "Review",
        ]
    ]

    # Compile the list of this user's reviews for export. By default
    # only entries added or edited since the last export are included,
    # so each Letterboxd upload contains exactly the new rows; the
    # "Full export" checkbox exports everything. New rows are detected
    # by id rather than date_watched, which can be backdated past the
    # last export

    export_query = (
        UserMovieReview.query.join(
            Movie, (Movie.id == UserMovieReview.movie_id)
        ).filter(UserMovieReview.user_id == int(current_user.id))
        # Rows that came FROM the Letterboxd feed never export back
        # to Letterboxd — they are already there, and the
        # round-trip would duplicate them
        .filter(UserMovieReview.letterboxd_guid.is_(None))
    )

    last_exported_at = current_user.date_reviews_exported
    incremental = (
        not review_export_form.full_export.data and last_exported_at is not None
    )
    if incremental:
        export_query = export_query.filter(
            db.or_(
                UserMovieReview.id > (current_user.last_export_review_id or 0),
                UserMovieReview.date_updated > last_exported_at,
            )
        )

    review_export = export_query.order_by(
        UserMovieReview.date_watched.desc(),
        UserMovieReview.date_reviewed.desc(),
        UserMovieReview.rating.desc(),
    ).all()

    if not review_export:
        if incremental:
            flash("Nothing logged or updated since your last export", "info")
        else:
            flash("No entries to export", "info")
        return redirect(url_for("main.profile"))
    for r in review_export:
        # Letterboxd accepts ratings of 0.5-5 and calendar dates only,
        # so unrated reviews export a blank rating and watched
        # timestamps are truncated to YYYY-MM-DD

        rating = ""
        if r.modified_rating:
            rating = (
                int(r.modified_rating)
                if r.modified_rating == int(r.modified_rating)
                else r.modified_rating
            )
        # Rewatch per the Letterboxd spec: Yes/No, blank when unknown
        # (rows that predate the flag)

        rewatch = "" if r.rewatch is None else ("Yes" if r.rewatch else "No")
        csv_export.append(
            [
                r.movie.tmdb_id,
                r.movie.imdb_id,
                r.movie.title,
                r.movie.year,
                rating,
                r.date_watched.strftime("%Y-%m-%d") if r.date_watched else "",
                rewatch,
                r.review or "",
            ]
        )

    current_app.logger.debug(csv_export)

    # Write out the CSV file in memory, no need to write it out to disk

    f = io.StringIO()
    review_writer = csv.writer(f, quoting=csv.QUOTE_ALL)
    for review in csv_export:
        review_writer.writerow(review)

    # Send an email to the user with the CSV file as an attachment;
    # incremental files are named for their cutoff so exports since
    # different dates are distinguishable in the inbox

    if incremental:
        filename = f"reviews-since-{last_exported_at.strftime('%Y-%m-%d')}.csv"
    else:
        filename = "reviews.csv"

    send_email(
        "Fitzflix - Your movie reviews",
        sender=("Fitzflix", current_app.config["SERVER_EMAIL"]),
        recipients=[current_user.email],
        text_body=render_template("email/reviews.txt", user=current_user),
        html_body=render_template("email/reviews.html", user=current_user),
        # Attach as UTF-8 bytes: a str payload makes the email
        # package fall back to raw-unicode-escape, which mangles
        # curly quotes into literal \\u2019 sequences in the file
        attachments=[
            (filename, "text/csv; charset=utf-8", f.getvalue().encode("utf-8"))
        ],
    )

    # Advance the export bookkeeping: either mode leaves Letterboxd
    # current through this moment

    current_user.date_reviews_exported = datetime.now()
    current_user.last_export_review_id = (
        db.session.query(db.func.max(UserMovieReview.id))
        .filter(UserMovieReview.user_id == int(current_user.id))
        .scalar()
    )
    db.session.commit()

    if incremental:
        count = len(review_export)
        flash(
            f"Emailed {count} new or updated entr{'y' if count == 1 else 'ies'}"
            f" to {current_user.email}",
            "success",
        )
    else:
        flash(f"Emailed your reviews to {current_user.email}", "success")

    # Discard the in-memory CSV file

    f.close()

    return redirect(url_for("main.profile"))
    return redirect(url_for("main.profile"))


def _review_upload_response(review_upload_form):
    """The review-import POST: a Letterboxd account export zip, or the
    legacy JSON-lines ratings file. On Profile since #215, like the
    export. Returns a response once the form has submitted, None
    otherwise."""

    if not (
        review_upload_form.upload_submit.data
        and review_upload_form.validate_on_submit()
    ):
        return None

    upload = request.files["file"]
    data = upload.read()

    if data[:4] == b"PK\x03\x04" or (upload.filename or "").lower().endswith(".zip"):
        # A Letterboxd account export, imported as-is: diary, ratings,
        # reviews, and film likes. Parsing is local and fast; matching
        # unowned films needs TMDB, so that runs as a task

        films = parse_letterboxd_export(data)
        if films:
            current_app.request_queue.enqueue(
                "app.videos.letterboxd_import_task",
                args=(current_user.id, films),
                job_timeout=current_app.config["LOCALIZATION_TASK_TIMEOUT"],
                description=f"Matching {len(films)} Letterboxd film(s)",
            )
            flash(f"Importing Letterboxd data for {len(films)} films", "info")
        else:
            flash("No importable films found in that Letterboxd export", "warning")

    else:
        # Legacy JSON-lines ratings file, one film per line

        for rating in data.splitlines():
            if not rating.strip():
                continue
            movie_rating = json.loads(rating)
            if movie_rating["rating"] >= 0:
                current_app.sql_queue.enqueue(
                    "app.videos.review_task",
                    args=(
                        current_user.id,
                        movie_rating["name"],
                        movie_rating["rating"],
                    ),
                    job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
                    description=f"Reviewing {movie_rating['name']}",
                )
    return redirect(url_for("main.profile"))


@bp.route("/history", methods=["GET", "POST"])
@login_required
def history():
    """Display all of a user's viewings and reviews."""

    # Paginate a user's movie reviews, show 50 reviews per page

    page = request.args.get("page", 1, type=int)

    # Chronological by watch date, newest first — unreviewed viewings (Plex
    # watches) sort by recency like everything else. Dated rows only: this
    # page is the diary, and a dateless row is a preference signal (a
    # rating-ladder tap, ratings.csv), not a viewing. Those rows still
    # drive recommendations and the stats below, and stay editable from
    # their film's page.

    # Day, then time of day, then title (#196). A midnight date_watched
    # means "no time recorded" rather than "watched at 00:00" — only a
    # watch logged on the day it happened keeps a clock time, see
    # _watched_timestamp — so sorting on the raw timestamp sank every
    # date-only row below any timed row on the same day, however late it
    # was actually logged. Fall back to the time the row was written,
    # which is the best evidence left of when the viewing happened.

    watched_time = db.func.nullif(
        db.func.time(UserMovieReview.date_watched), "00:00:00"
    )
    time_of_day = db.func.coalesce(
        watched_time, db.func.time(UserMovieReview.date_reviewed)
    )

    reviews = (
        UserMovieReview.query.join(Movie, (Movie.id == UserMovieReview.movie_id))
        .options(contains_eager(UserMovieReview.movie))
        .filter(UserMovieReview.user_id == int(current_user.id))
        .filter(UserMovieReview.date_watched.isnot(None))
        .order_by(
            db.func.date(UserMovieReview.date_watched).desc(),
            time_of_day.desc(),
            UserMovieReview.date_reviewed.desc(),
            Movie.title.asc(),
        )
        .paginate(page=page, per_page=50, error_out=False)
    )
    next_url = (
        url_for("main.history", page=reviews.next_num) if reviews.has_next else None
    )
    prev_url = (
        url_for("main.history", page=reviews.prev_num) if reviews.has_prev else None
    )

    # The ratings distribution: five whole-star bins, each absorbing the
    # half-step below it (2.5 and 3.0 both bin as "about 3 stars") — most
    # ratings are whole stars, so ten half-star buckets rendered as
    # near-empty slivers. Only rated reviews count — Letterboxd-era
    # reviews can be unrated likes or text-only.

    rating_counts = dict(
        db.session.query(UserMovieReview.modified_rating, db.func.count())
        .filter(UserMovieReview.user_id == int(current_user.id))
        .filter(UserMovieReview.modified_rating.isnot(None))
        .group_by(UserMovieReview.modified_rating)
        .all()
    )
    star_bins = {star: 0 for star in range(1, 6)}
    for value, count in rating_counts.items():
        star_bins[min(5, max(1, int(value + 0.5)))] += count
    max_count = max(star_bins.values(), default=0)
    rating_distribution = [
        {
            "stars": star,
            "count": star_bins[star],
            "percent": round(star_bins[star] / max_count * 100) if max_count else 0,
        }
        for star in range(1, 6)
    ]
    rating_summary = (
        db.session.query(
            db.func.count(UserMovieReview.rating),
            db.func.avg(UserMovieReview.rating),
            db.func.sum(db.case((UserMovieReview.liked == True, 1), else_=0)),
        )
        .filter(UserMovieReview.user_id == int(current_user.id))
        .one()
    )
    rated_count, rating_average, liked_count = (
        rating_summary[0],
        float(rating_summary[1]) if rating_summary[1] is not None else None,
        int(rating_summary[2] or 0),
    )

    # Unrated viewings — Plex watches, unrated imports — preview the
    # engine's estimate in their ladder until Glenn's own stars land,
    # through the shared score source like every other surface

    estimates = {}
    profile = stored_profile(current_app.redis, current_user.id)
    if profile:
        scores = stored_scores(current_app.redis, current_user.id)
        for review in reviews.items:
            if review.rating is not None or review.movie_id in estimates:
                continue
            score = resolved_score(
                current_app.redis,
                current_user.id,
                review.movie,
                profile,
                scores=scores,
            )
            if score is not None:
                estimates[review.movie_id] = estimated_rating(profile, score)

    return render_template(
        "history.html",
        title="My History",
        reviews=reviews.items,
        estimates=estimates,
        next_url=next_url,
        prev_url=prev_url,
        pages=reviews,
        rating_distribution=rating_distribution,
        rated_count=rated_count,
        rating_average=rating_average,
        liked_count=liked_count,
    )


@bp.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    """User profile: email address and API key."""

    # Form to update the user's email address

    email_form = EditProfileForm(current_user.email)
    if email_form.submit.data and email_form.validate_on_submit():
        current_user.email = email_form.email.data
        db.session.commit()
        flash("Your email address has been changed.", "success")
        return redirect(url_for("main.profile"))

    # Form to generate a new API key

    api_refresh_form = UpdateAPIKeyForm()
    if (
        api_refresh_form.regenerate_key_submit.data
        and api_refresh_form.validate_on_submit()
    ):
        current_user.api_key = secrets.token_hex(16)
        db.session.commit()
        flash("Regenerated the API key.", "success")
        return redirect(url_for("main.profile"))

    # Button to wipe the user's Name that Frame standings (Glenn's
    # ask, Aug 27 2026) — the score rows go, the dealt-frames record
    # stays, so a fresh start doesn't replay frames just seen

    reset_frames_form = ResetFrameScoresForm()
    if (
        reset_frames_form.reset_frames_submit.data
        and reset_frames_form.validate_on_submit()
    ):
        UserFrameScore.query.filter_by(user_id=int(current_user.id)).delete()
        db.session.commit()
        flash("Your Name that Frame scores and statistics have been reset.", "success")
        return redirect(url_for("main.profile"))

    # Form to map this account to a Plex username, so Plex watches land in
    # this user's diary

    letterboxd_form = LetterboxdUsernameForm()
    if letterboxd_form.letterboxd_submit.data and letterboxd_form.validate_on_submit():
        username = (letterboxd_form.letterboxd_username.data or "").strip() or None
        current_user.letterboxd_username = username
        db.session.commit()
        if username:
            flash(
                f"Letterboxd diary entries by '{username}' now sync into "
                f"your history.",
                "success",
            )
        else:
            flash("Letterboxd sync disabled.", "info")
        return redirect(url_for("main.profile"))

    plex_form = PlexUsernameForm()
    if plex_form.plex_submit.data and plex_form.validate_on_submit():
        plex_username = (plex_form.plex_username.data or "").strip() or None
        taken = (
            User.query.filter(User.plex_username == plex_username)
            .filter(User.id != current_user.id)
            .first()
            if plex_username
            else None
        )
        if taken:
            flash(f"'{plex_username}' is already mapped to another user.", "danger")
        else:
            current_user.plex_username = plex_username
            db.session.commit()
            if plex_username:
                flash(
                    f"Plex watches by '{plex_username}' now count as yours.", "success"
                )
            else:
                flash("Removed your Plex username mapping.", "success")
        return redirect(url_for("main.profile"))

    # This user's playback device: the Plex player their play buttons
    # send films to. The user enters just an address (ip or hostname,
    # port optional — Companion's 32500 is assumed); Fitzflix probes it
    # and reads the machine id off the player itself, so a device is
    # only ever saved verified-reachable. Blank removes the device

    plex_player_form = PlexPlayerForm()
    if (
        plex_player_form.plex_player_submit.data
        and plex_player_form.validate_on_submit()
    ):
        address = (plex_player_form.plex_player_address.data or "").strip() or None
        if address is None:
            current_user.plex_player_address = None
            current_user.plex_player_id = None
            db.session.commit()
            flash("Removed your playback device.", "success")
        elif not re.fullmatch(r"[A-Za-z0-9.\-:\[\]]+", address):
            flash("That doesn't look like an ip:port or hostname:port.", "danger")
        else:
            if ":" not in address.strip("[]"):
                address = f"{address}:32500"
            player = probe_player(address)
            if player is None:
                flash(
                    f"No Plex player answered at {address}. Make sure the "
                    "Plex app is open on the device with 'Advertise as "
                    "Player' enabled, and that this address is reachable "
                    "from the Fitzflix server.",
                    "danger",
                )
            else:
                current_user.plex_player_address = address
                current_user.plex_player_id = player["machine_id"]
                db.session.commit()
                flash(
                    f"Play buttons now send films to '{player['name']}' "
                    f"at {address}.",
                    "success",
                )
        return redirect(url_for("main.profile"))

    # This user's Infuse target (#192): the same Apple TV, driven over
    # Apple's Companion protocol instead of Plex Companion. Saving an
    # address starts the one-time PIN pairing, which must live in a
    # single process across the PIN round-trip — so it runs as a
    # user-request queue task and the PIN crosses over through Redis;
    # the PIN form below only appears while a pairing is waiting

    infuse_form = InfusePlayerForm()
    if infuse_form.infuse_player_submit.data and infuse_form.validate_on_submit():
        address = (infuse_form.infuse_player_address.data or "").strip() or None
        if address is None:
            current_user.infuse_player_address = None
            current_user.infuse_player_credentials = None
            db.session.commit()
            flash("Removed your Infuse player.", "success")
        elif not re.fullmatch(r"[A-Za-z0-9.\-:\[\]]+", address):
            flash("That doesn't look like an ip:port or hostname:port.", "danger")
        else:
            if ":" not in address.strip("[]"):
                address = f"{address}:{COMPANION_PORT}"
            if start_pairing(current_user.id, address):
                flash(
                    "Look at the Apple TV — it should show a PIN within a "
                    "few seconds. Enter it below to finish pairing.",
                    "info",
                )
            else:
                flash(
                    "A pairing is already waiting for its PIN — enter that "
                    "below, or give it a couple of minutes to expire before "
                    "starting over.",
                    "warning",
                )
        return redirect(url_for("main.profile"))

    infuse_pin_form = InfusePinForm()
    if infuse_pin_form.infuse_pin_submit.data and infuse_pin_form.validate_on_submit():
        pin = (infuse_pin_form.infuse_pin.data or "").strip()
        if not pin.isdigit():
            flash("The PIN is the number shown on the Apple TV's screen.", "danger")
        else:
            submit_pin(current_user.id, pin)
            ok, message = pairing_outcome(current_user.id)
            flash(message, {True: "success", False: "danger", None: "info"}[ok])
        return redirect(url_for("main.profile"))

    # Which app plain play buttons target, asked only while both are
    # configured; with a single app there is no choice to make

    default_player_form = DefaultPlayerForm()
    if (
        default_player_form.default_player_submit.data
        and default_player_form.validate_on_submit()
    ):
        current_user.default_player = default_player_form.default_player.data
        db.session.commit()
        flash(
            f"Play buttons now default to "
            f"{'Infuse' if current_user.default_player == 'infuse' else 'Plex'}.",
            "success",
        )
        return redirect(url_for("main.profile"))
    if not default_player_form.default_player_submit.data:
        default_player_form.default_player.data = current_user.preferred_player

    # Form to pick the streaming services availability displays are
    # customized to — a per-user setting, never site-wide. The picker
    # offers every registry provider, alphabetically

    registry = provider_registry()
    subscribed = {row.provider_id: row for row in current_user.streaming_providers}
    picker = sorted(registry, key=lambda p: (p["provider_name"] or "").lower())
    streaming_form = StreamingProvidersForm()
    streaming_form.providers.choices = [
        (p["provider_id"], p["provider_name"]) for p in picker
    ]
    if streaming_form.providers_submit.data and streaming_form.validate_on_submit():
        chosen = set(streaming_form.providers.data or [])
        registry_by_id = {p["provider_id"]: p for p in registry}
        for provider_id, row in subscribed.items():
            if provider_id not in chosen:
                db.session.delete(row)
        for provider_id in chosen - set(subscribed):
            details = registry_by_id.get(provider_id) or {}
            db.session.add(
                UserStreamingProvider(
                    user_id=current_user.id,
                    provider_id=provider_id,
                    name=details.get("provider_name"),
                    logo_path=details.get("logo_path"),
                )
            )
        db.session.commit()
        flash("Updated your streaming services.", "success")
        return redirect(url_for("main.profile"))
    if not streaming_form.providers_submit.data:
        streaming_form.providers.data = list(subscribed)

    # The watchlist availability digest opt-ins (#156/#230): the
    # nightly email is off unless asked for, and rentals are a further
    # opt-in on top of it

    alerts_form = AvailabilityAlertsForm()
    if alerts_form.alerts_submit.data and alerts_form.validate_on_submit():
        current_user.notify_availability = bool(alerts_form.notify_availability.data)
        current_user.notify_rentals = bool(alerts_form.notify_rentals.data)
        db.session.commit()
        flash("Updated your watchlist alerts.", "success")
        return redirect(url_for("main.profile"))
    if not alerts_form.alerts_submit.data:
        alerts_form.notify_availability.data = current_user.notify_availability
        alerts_form.notify_rentals.data = current_user.notify_rentals

    # The review import / export forms, moved here from History (#215)
    # — account-level plumbing rather than something to pass on the way
    # to the diary

    review_export_form = ReviewExportForm()
    export_response = _review_export_response(review_export_form)
    if export_response is not None:
        return export_response
    review_upload_form = ReviewUploadForm()
    upload_response = _review_upload_response(review_upload_form)
    if upload_response is not None:
        return upload_response

    return render_template(
        "profile.html",
        title="Profile",
        review_export_form=review_export_form,
        review_upload_form=review_upload_form,
        reset_frames_form=reset_frames_form,
        email_form=email_form,
        api_refresh_form=api_refresh_form,
        plex_form=plex_form,
        letterboxd_form=letterboxd_form,
        plex_player_form=plex_player_form,
        remote_playback=remote_playback_configured(),
        infuse_form=infuse_form,
        infuse_pin_form=infuse_pin_form,
        infuse_pairing_pending=pairing_pending(current_user.id),
        default_player_form=default_player_form,
        streaming_form=streaming_form,
        alerts_form=alerts_form,
        provider_logos={p["provider_id"]: p["logo_path"] for p in picker},
    )
