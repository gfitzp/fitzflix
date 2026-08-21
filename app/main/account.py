"""The user's own pages (#17's slice f): the viewing history with
its per-row editors, review editing, and the profile."""

import csv
import io
import json
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

from app import db
from app.main.forms import (
    EditProfileForm,
    MovieReviewForm,
    ReviewExportForm,
    ReviewUploadForm,
    LetterboxdUsernameForm,
    PlexUsernameForm,
    StreamingProvidersForm,
    UpdateAPIKeyForm,
)
from app.models import (
    Movie,
    User,
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

    # Feed-originated rows sync FROM Letterboxd (#61): while the entry
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
        # A logged viewing can't be "not interested" (#51) — the ladder
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
        # clears the stars instead (#54): the viewing itself stays, an
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
        # RODE IN THE POST — the history page's per-row forms (#58) and
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
            # engine's estimate (#58's rule, extended to bare watches)
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

    reviews = (
        UserMovieReview.query.join(Movie, (Movie.id == UserMovieReview.movie_id))
        .filter(UserMovieReview.user_id == int(current_user.id))
        .filter(UserMovieReview.date_watched.isnot(None))
        .order_by(
            UserMovieReview.date_watched.desc(),
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

    # Form to request an export of all of this user's movie reviews as a CSV file

    review_export_form = ReviewExportForm()
    if (
        review_export_form.export_submit.data
        and review_export_form.validate_on_submit()
    ):
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
            # to Letterboxd (#61) — they are already there, and the
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
            return redirect(url_for("main.history"))
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

        return redirect(url_for("main.history"))

    review_upload_form = ReviewUploadForm()
    if (
        review_upload_form.upload_submit.data
        and review_upload_form.validate_on_submit()
    ):
        upload = request.files["file"]
        data = upload.read()

        if data[:4] == b"PK\x03\x04" or (upload.filename or "").lower().endswith(
            ".zip"
        ):
            # A Letterboxd account export, imported as-is: diary, ratings,
            # reviews, and film likes. Parsing is local and fast; matching
            # unowned films needs TMDb, so that runs as a task

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
        return redirect(url_for("main.history"))

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
        review_export_form=review_export_form,
        review_upload_form=review_upload_form,
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

    return render_template(
        "profile.html",
        title="Profile",
        email_form=email_form,
        api_refresh_form=api_refresh_form,
        plex_form=plex_form,
        letterboxd_form=letterboxd_form,
        streaming_form=streaming_form,
        provider_logos={p["provider_id"]: p["logo_path"] for p in picker},
    )
