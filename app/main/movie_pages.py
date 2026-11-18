"""Serve the movie library and the pages of 1 film."""

import time
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
from flask_wtf.csrf import validate_csrf
from wtforms import ValidationError
from markupsafe import Markup
from flask_login import current_user, login_required
from sqlalchemy.exc import IntegrityError
from app.tasks import enqueue_task
from app import db
from app.main.forms import (
    CriterionForm,
    LibrarySearchForm,
    MovieReviewForm,
    MovieShoppingExcludeForm,
    RadarrForm,
    QualityFilterForm,
    TMDBLookupForm,
    TMDBRemoveForm,
    WatchlistForm,
)
from app.models import (
    File,
    Movie,
    MovieAward,
    MovieCast,
    MovieCrew,
    RefFeatureType,
    RefQuality,
    TMDBCredit,
    TMDBGenre,
    UserMovieStatus,
    UserWatchlist,
    movie_genres,
    title_sort,
)
from app.main import bp
from app.media.file_ranking import MOVIE_BEST_ORDER, quality_anchors, ranked_movie_files
from app.availability.newly_added import poster_fold
from app.playback.infuse_player import infuse_only_formats
from app.playback.infuse_player import play_movie as infuse_play_movie
from app.playback.plex_player import play_movie, remote_playback_configured
from app.main.helpers import (
    _hold_verdict_lock,
    apply_verdict,
    verdict_response,
    admin_required,
    flash_form_errors,
    _card_fetch,
    _ladder_fetch,
    _latest_review_row,
    decimal_id,
    _verdict_post,
    refused_verdict,
    MOVIE_PAGE_POLICY,
    library_upgradable,
)
from app.discovery.recommendations import (
    estimated_rating,
    resolved_score,
    stored_profile,
)
from app.availability.streaming import user_streaming
from app.discovery.elicitation import suggestions_after_rating
from app.arr.radarr_push import radarr_configured, radarr_tmdb_ids
from app.criterion.criterion_catalog import record_set_title
from app.viewing.diary import clear_watchlist
from app.main import people_pages


@bp.route("/library/movie", methods=["GET", "POST"])
@login_required
def movie_library():
    """Show the best quality version of each movie in the library.

    Possible user queries:
    - credit: the id of an actor. Fitzflix filters the movie list to the
              films that the actor starred in.
    - q     : a substring. Fitzflix filters the movie list to the films
              that contain it.
    - filter: "library" limits a credit view to the owned titles. Any
              other value, or no value, shows the full career.
    """

    page = request.args.get("page", 1, type=int)
    credit = request.args.get("credit", None, type=int)
    credit_filter = request.args.get("filter", "all", type=str)
    q = request.args.get("q", None, type=str)
    genre = request.args.get("genre", None, type=int)
    quality = request.args.get("quality", "0", type=str)

    # A stale or mistyped bookmark must not crash the page (#306).

    if not quality.isdecimal():
        quality = "0"

    if credit:
        return people_pages._filmography(
            credit, library_only=credit_filter == "library"
        )

    # This subquery gets the best movie files.

    ranked_files = ranked_movie_files()
    movies = (
        db.session.query(File, Movie, RefQuality)
        .join(Movie, (Movie.id == File.movie_id))
        .join(RefQuality, (RefQuality.id == File.quality_id))
        .join(ranked_files, (ranked_files.c.id == File.id))
    )

    # A search ignores the genre and the quality. The genre links on the
    # movie pages filter the library to the films that carry the TMDB
    # genre. The quality dropdown can combine with the genre.

    if q:
        title = f"Library movies matching '{q}'"
    elif genre:
        genre_row = db.session.get(TMDBGenre, int(genre))
        if genre_row is None:
            abort(404)
        title = f"{genre_row.name} Movies"
        movies = movies.join(
            movie_genres, (movie_genres.c.movie_id == Movie.id)
        ).filter(movie_genres.c.genre_id == int(genre))
    else:
        title = "Movie Library"
    movies = movies.filter(File.feature_type_id == None).filter(
        ranked_files.c.rank == 1
    )
    if q:
        like = q.replace(" ", "%")
        movies = movies.filter(
            db.or_(Movie.title.ilike(f"%{like}%"), Movie.tmdb_title.ilike(f"%{like}%"))
        )
    elif int(quality) > 0:
        movies = movies.filter(RefQuality.id == int(quality))
    movies = movies.order_by(
        title_sort(Movie).asc(),
        db.case(
            (Movie.tmdb_title != None, Movie.tmdb_release_date),
            else_=Movie.year,
        ).asc(),
        File.edition.asc(),
    ).paginate(page=page, per_page=120, error_out=False)

    # The arrows keep each filter of the page.

    filters = {
        "quality": quality,
        "genre": genre,
        "credit": credit,
        "q": request.args.get("q"),
    }
    next_url = (
        url_for("main.movie_library", page=movies.next_num, **filters)
        if movies.has_next
        else None
    )
    prev_url = (
        url_for("main.movie_library", page=movies.prev_num, **filters)
        if movies.has_prev
        else None
    )

    filter_form = QualityFilterForm()

    # Create the list of qualities for the dropdown filter

    qualities = (
        db.session.query(RefQuality.id, RefQuality.quality_title)
        .join(File, (File.quality_id == RefQuality.id))
        .distinct()
        .filter(File.movie_id != None)
        .filter(File.feature_type_id == None)
        .order_by(RefQuality.preference.asc())
        .all()
    )
    filter_form.quality.choices = [("0", "All")] + [
        (str(id), title) for (id, title) in qualities
    ]

    filter_form.quality.default = quality

    if filter_form.validate_on_submit():
        return redirect(
            url_for("main.movie_library", quality=filter_form.quality.data, genre=genre)
        )

    filter_form.process()

    # The form to search the movie library titles for a substring

    library_search_form = LibrarySearchForm()
    if library_search_form.validate_on_submit():
        return redirect(
            url_for("main.movie_library", q=library_search_form.search_query.data)
        )

    return render_template(
        "library_movie.html",
        title=title,
        movies=movies.items,
        next_url=next_url,
        prev_url=prev_url,
        pages=movies,
        filter_form=filter_form,
        library_search_form=library_search_form,
    )


def _flash_tmdb_refresh(result, title, reload_url):
    """Flash the state of an interactive TMDB refresh.

    The refresh job only fetches. The apply runs next on the sql queue.
    Thus, a fetched result does not say that the page has the new data.
    None is a job that still runs. False is a failed fetch (#514). Then
    nothing is queued, and a reload shows no change."""

    from app.metadata.tmdb_refresh import TMDB_NO_MATCH

    if result == TMDB_NO_MATCH:
        flash(f"TMDB has no data for '{title}' with that ID.", "warning")
    elif result is False:
        flash(
            f"Fitzflix could not get TMDB data for '{title}'. The log has "
            f"the reason. Try again later.",
            "danger",
        )
    elif result:
        flash(
            Markup(
                "Fetched TMDB data for '{}'. Fitzflix applies it next. "
                "<a href='{}'>Reload this page</a>"
            ).format(title, reload_url),
            "success",
        )
    else:
        flash(
            Markup(
                "Fitzflix is refreshing TMDB data for '{}'. "
                "<a href='{}'>Reload this page</a>"
            ).format(title, reload_url),
            "info",
        )


@bp.route("/movie/<int:movie_id>", methods=["GET", "POST"])
@login_required
def movie(movie_id):
    """Show details for a particular movie."""

    movie = Movie.query.filter_by(id=movie_id).first_or_404()
    title = movie.display_title
    # Each credited actor in billing order, for the cast scroller

    cast = [
        {
            "id": role.starring.id,
            "name": role.starring.name,
            "profile_path": role.starring.tmdb_profile_path,
            "character": role.character,
        }
        for role in MovieCast.query.filter(MovieCast.movie_id == movie_id)
        .order_by(MovieCast.billing_order.asc())
        .all()
    ]
    # (credit id, name) pairs. Thus, the directed-by line links to the
    # filmography pages, like the featured card of the rating drive.
    directors = list(
        db.session.query(TMDBCredit.id, TMDBCredit.name)
        .join(MovieCrew, MovieCrew.credit_id == TMDBCredit.id)
        .filter(MovieCrew.movie_id == movie.id)
        .filter(MovieCrew.job == "Director")
        .distinct()
    )
    genres = [(genre.id, genre.name) for genre in movie.genres]
    awards = (
        MovieAward.query.filter_by(movie_id=movie.id)
        .order_by(
            MovieAward.win.desc(), MovieAward.year.asc(), MovieAward.award_name.asc()
        )
        .all()
    )
    review = _latest_review_row(current_user.id, movie.id)
    films, features = _films_and_features(movie_id)

    # The shopping list is shared by every user. Thus, only an admin can
    # add a film to it or remove a film from it.

    movie_shopping_exclude_form = MovieShoppingExcludeForm()
    if (
        movie_shopping_exclude_form.add_submit.data
        or movie_shopping_exclude_form.exclude_submit.data
    ) and not current_user.admin:
        flash("Only an admin user can change the shopping list.", "warning")
        return redirect(url_for("main.movie", movie_id=movie.id))

    if (
        movie_shopping_exclude_form.add_submit.data
        and movie_shopping_exclude_form.validate_on_submit()
    ):
        movie.shopping_list_exclude = 0
        db.session.commit()
        flash(f"Added '{title}' to the shopping list")
        return redirect(url_for("main.movie", movie_id=movie.id))

    elif (
        movie_shopping_exclude_form.exclude_submit.data
        and movie_shopping_exclude_form.validate_on_submit()
    ):
        movie.shopping_list_exclude = 1
        db.session.commit()
        flash(f"Removed '{title}' from the shopping list")
        return redirect(url_for("main.movie", movie_id=movie.id))

    # The form to review a movie. A user can review the same movie many
    # times (tastes change). Thus, this only adds one more review to the
    # UserMovieReview table for this film.

    # The date field starts BLANK. The default log has no date ("seen at
    # some time, unknown when"). Plex supplies real timestamps for the
    # watches that it sees. The field is there for the times when the
    # date is known.

    movie_review_form = MovieReviewForm()
    post = _verdict_post(movie_review_form)
    if post is not None:
        if post.invalid:
            flash("That rating is not valid.", "warning")
            return redirect(url_for("main.movie", movie_id=movie.id))
        # The ladder is the only rating input. Log Watch without a tap
        # is a bare diary entry. 3 or more stars set the liked flag. The
        # date and the review text submit as they are in both cases. A
        # tap on a SUGGESTION card carries the movie_id of that film and
        # rates it (without a date). The strip stays anchored to this
        # page.

        target = movie
        if post.quick_present:
            raw_movie_id = (request.form.get("movie_id") or "").strip()
            if raw_movie_id:
                form_movie_id = decimal_id(raw_movie_id)
                target = (
                    db.session.get(Movie, form_movie_id)
                    if form_movie_id is not None
                    else None
                )
                if target is None:
                    # An unknown id names no film. The post must not fall
                    # back on the film of this page.
                    if _ladder_fetch():
                        return jsonify({"error": "unknown film"}), 404
                    flash("That film is not in the library.", "warning")
                    return redirect(url_for("main.movie", movie_id=movie.id))
            _hold_verdict_lock(f"movie:{target.id}")

        # A poster-card post (#45c) and a tap on a suggestion card never
        # steer the drive. Only the own page of a film (and the featured
        # card) moves the last-response state.

        steer = target.id == movie.id and not request.form.get("from_card")
        outcome = apply_verdict(post, target, MOVIE_PAGE_POLICY(steer))
        return verdict_response(
            outcome,
            target,
            target.display_title,
            url_for("main.movie", movie_id=movie.id),
        )
    refused = refused_verdict(
        movie_review_form, url_for("main.movie", movie_id=movie.id)
    )
    if refused is not None:
        return refused

    # The watchlist toggle. An add is useful only for a film with no
    # local copy (the funnel stage before the shopping list). Removal is
    # offered whenever the film is on the list, also after the user got
    # the film.

    watchlist_form = WatchlistForm()
    on_watchlist = (
        UserWatchlist.query.filter_by(
            user_id=int(current_user.id), movie_id=movie.id
        ).first()
        is not None
    )
    if watchlist_form.add_watchlist_submit.data and watchlist_form.validate_on_submit():
        # A movie_id in the form banks a film from the suggestion strip.
        # Without one, the toggle adds THIS film. A bank does not touch
        # the last-response state. Thus, the strip stays anchored, and
        # the banked film only drops out of it.
        target_id = watchlist_form.movie_id.data or movie.id
        target = db.session.get(Movie, target_id)
        if target is None:
            if _card_fetch():
                return jsonify({"error": "unknown film"}), 404
            flash("That film is not in the library.", "warning")
            return redirect(url_for("main.movie", movie_id=movie.id))
        if not UserWatchlist.query.filter_by(
            user_id=int(current_user.id), movie_id=target.id
        ).first():
            db.session.add(UserWatchlist(user_id=current_user.id, movie_id=target.id))
            try:
                db.session.commit()
            except IntegrityError:
                # A second post in flight added the row first (#516). The
                # film is on the list. Thus, the answer is the same.
                db.session.rollback()
        if _card_fetch():
            return jsonify({"on_watchlist": True})
        target_title = target.display_title
        flash(f"Added '{target_title}' to your watchlist", "success")
        return redirect(url_for("main.movie", movie_id=movie.id))
    if (
        watchlist_form.remove_watchlist_submit.data
        and watchlist_form.validate_on_submit()
    ):
        clear_watchlist(current_user.id, movie.id)
        db.session.commit()
        if _card_fetch():
            return jsonify({"on_watchlist": False})
        flash(f"Removed '{title}' from your watchlist", "success")
        return redirect(url_for("main.movie", movie_id=movie.id))

    # The not-interested state (#45b). The standalone buttons are gone
    # per #184. The ✕ toggle of the ladder is the only writer now. The
    # quick-rating branch handles it. Fitzflix reads the state for the
    # flag of the ladder and for the note.

    refused = (
        UserMovieStatus.query.filter_by(
            user_id=int(current_user.id), movie_id=movie.id, kind="not_interested"
        ).first()
        is not None
    )

    # The Movie Data forms are admin tools (#186 follow-up). The section
    # renders only for admins. The forms post to movie_tmdb and
    # movie_criterion, which only admins can reach.

    tmdb_remove_form = TMDBRemoveForm()
    tmdb_lookup_form = TMDBLookupForm()
    criterion_form = _criterion_form(movie)
    criterion_form.process()

    # Streaming availability for the services of this user. It is quiet
    # for a user who selected none. An owned film starts with "In your
    # library" (with or without a streaming match). A film with no local
    # files says "not on your services". That is where the watch-or-buy
    # decision is.

    streaming = (
        user_streaming(
            movie.tmdb_id,
            current_user,
            negative=not films,
            local=bool(films),
            upgradable=library_upgradable(movie),
        )
        if movie.tmdb_id
        else None
    )

    # The "since you liked…" strip renders when the last positive rating
    # of the session was for THIS film. That is directly after the
    # redirect of the log arrives back here (review_tmdb logs arrive
    # here too). The page of another film computes nothing (#705).

    suggested_ids = suggestions_after_rating(current_user.id, movie.id)
    suggestions = []
    if suggested_ids:
        suggested_movies = {
            m.id: m for m in Movie.query.filter(Movie.id.in_(suggested_ids))
        }
        suggestions = [
            suggested_movies[movie_id]
            for movie_id in suggested_ids
            if movie_id in suggested_movies
        ]

    # The personal funnel badge state. "Seen" is any diary row of the
    # current user (review is their latest row). "Might interest you"
    # never shows on a seen film. Its watch already feeds the taste
    # profile. An owned film shows the badge when the nightly recompute
    # ranked it in the stored recommendations. An unowned record scores
    # through the coarse scorer against the profile-relative bar, like
    # the TMDB search results.

    # The estimated rating (#45a) comes from the shared score source.
    # That is the stored map. It live-scores a missing film with the
    # same recipe and writes the result back. Thus, each surface shows
    # 1 number (requested by Glenn: a low guess warns against a
    # watchlist add as usefully as a high guess invites it). The
    # calibration curve of the profile turns the score into "you might
    # rate this around ★★★★". Fitzflix shows it until the own STARS of
    # the user exist. Thus, a bare unrated watch still previews the
    # guess.

    estimated = None
    profile = stored_profile(current_app.redis, current_user.id)
    if (review is None or review.rating is None) and not refused:
        score = resolved_score(current_app.redis, current_user.id, movie, profile)
        if score is not None:
            estimated = estimated_rating(profile, score)

    # The ad-hoc Radarr hand-off. An admin can request an unowned film
    # for download. The badge reads from the hour-cached id set.

    in_radarr = bool(
        current_user.admin
        and not films
        and movie.tmdb_id
        and radarr_configured()
        and movie.tmdb_id in radarr_tmdb_ids()
    )

    # The refresh and the import pipeline share the title lock. A refresh
    # that found the lock busy waits in the scheduled registry. Without
    # this note, a match from the form looks like it did nothing.

    tmdb_refresh_state = None
    if current_user.admin:
        from app.metadata.tmdb_refresh import tmdb_refresh_status

        tmdb_refresh_state = tmdb_refresh_status("Movies", movie.id)

    return render_template(
        "movie.html",
        title=title,
        movie=movie,
        tmdb_refresh_state=tmdb_refresh_state,
        poster_fold=poster_fold(current_user, movie.tmdb_id, movie.id),
        cast=cast,
        directors=directors,
        genres=genres,
        # The US rating of the meta line. It is the same answer that the
        # popover card gives. Thus, the page and the popup read the same.
        certification=movie.us_certification,
        awards=awards,
        review=review,
        films=films,
        radarr_form=RadarrForm(),
        radarr_available=radarr_configured(),
        in_radarr=in_radarr,
        features=features,
        movie_shopping_exclude_form=movie_shopping_exclude_form,
        movie_review_form=movie_review_form,
        tmdb_lookup_form=tmdb_lookup_form,
        tmdb_remove_form=tmdb_remove_form,
        criterion_form=criterion_form,
        streaming=streaming,
        watchlist_form=watchlist_form,
        on_watchlist=on_watchlist,
        estimated_rating=estimated,
        refused=refused,
        suggestions=suggestions,
        radarr_proxy_url=current_app.config["RADARR_PROXY_URL"],
        plex_playable=(
            bool(films)
            and current_user.plex_player_configured
            and remote_playback_configured()
        ),
        infuse_playable=(
            bool(films)
            and bool(movie.tmdb_id)
            and current_user.infuse_player_configured
        ),
        default_player=current_user.preferred_player,
        infuse_reasons=infuse_only_formats(films),
    )


def _films_and_features(movie_id):
    """Return (films, features), the main files and the extras of a film."""

    films = (
        File.query.join(RefQuality, (RefQuality.id == File.quality_id))
        .filter(File.movie_id == movie_id)
        .filter(File.feature_type_id == None)
        .order_by(File.fullscreen.asc(), File.edition.asc(), *MOVIE_BEST_ORDER)
        .all()
    )
    features = (
        File.query.filter(File.movie_id == movie_id)
        .filter(File.feature_type_id != None)
        .order_by(File.basename.asc())
        .all()
    )
    return films, features


def _criterion_form(movie):
    """Return the Criterion form of a film, with its quality choices and default.

    The first choice is "Unknown". It carries the empty value, and the
    save stores no format for it (#685). Thus, a film with no recorded
    format keeps none after a save. The other choices are the physical
    DVD, 1080p, and 2160p tiers, worst first. The list validates a
    posted value against these choices."""

    criterion_form = CriterionForm()
    tiers = sorted(
        (tier.preference, tier.id, title)
        for title, tier in quality_anchors().items()
        if tier.physical_media
        and (title == "DVD" or title.endswith(("1080p", "2160p")))
    )
    criterion_form.quality.choices = [("", "Unknown")] + [
        (str(id), title) for (_, id, title) in tiers
    ]
    criterion_form.quality.default = (
        str(movie.criterion_quality_id) if movie.criterion_quality_id else ""
    )
    return criterion_form


def _wait_for_refresh(refresh_job):
    """Wait up to 10 seconds for the result of an interactive TMDB refresh.

    If the refresh completes in that time, the redirect shows the
    updated page. If not, the page gives the user a link to reload."""

    from rq.job import JobStatus

    waited_seconds = 0
    while True:
        status = refresh_job.get_status(refresh=True)
        # A job that raised has no return value. It is a failure, not a
        # refresh that still runs (#514).
        if status in (JobStatus.FAILED, JobStatus.STOPPED, JobStatus.CANCELED):
            return False
        result = refresh_job.return_value(refresh=True)
        if result is not None or waited_seconds >= 10:
            return result
        time.sleep(1)
        waited_seconds = waited_seconds + 1


@bp.route("/movie/<int:movie_id>/tmdb", methods=["POST"])
@login_required
@admin_required
def movie_tmdb(movie_id):
    """Detach a film from TMDB, or refresh its TMDB data, for an admin."""

    movie = Movie.query.filter_by(id=movie_id).first_or_404()
    title = movie.display_title
    films, features = _films_and_features(movie.id)

    # The form to detach a movie from TMDB completely, for a film that
    # TMDB has no entry for (#207). A record with no local files can
    # never be such a film. Fitzflix created it FROM a TMDB id, and it
    # mirrors that entry. Thus, a detach would leave a husk that nothing
    # can enrich again.

    tmdb_remove_form = TMDBRemoveForm()
    if tmdb_remove_form.remove_submit.data and tmdb_remove_form.validate_on_submit():
        if not films and not features:
            flash(
                f"'{title}' has no local files. Its record mirrors TMDB "
                f"and cannot be detached",
                "warning",
            )
            return redirect(url_for("main.movie", movie_id=movie.id))
        movie.tmdb_movie_clear()
        db.session.commit()
        flash(
            f"Removed the TMDB ID from '{title}'. Fitzflix will not look it up "
            f"again until you enter one by hand",
            "success",
        )
        return redirect(url_for("main.movie", movie_id=movie.id))

    # The form to update the information of a movie with the latest TMDB data

    tmdb_lookup_form = TMDBLookupForm()
    if not (
        tmdb_lookup_form.lookup_submit.data and tmdb_lookup_form.validate_on_submit()
    ):
        flash_form_errors(tmdb_lookup_form)
        return redirect(url_for("main.movie", movie_id=movie.id))

    # A record with no local files IS its TMDB entry. The id came directly
    # from TMDB at creation, and the diary rows depend on it. Thus, the
    # only operation is a refresh of the stored id. Never point it to a
    # different id. Fitzflix ignores a smuggled id in the post.

    if not films and not features:
        if not movie.tmdb_id:
            flash(
                f"'{title}' is not matched to TMDB. There is nothing to refresh",
                "warning",
            )
            return redirect(url_for("main.movie", movie_id=movie.id))
        refresh_job = enqueue_task(
            "app.metadata.tmdb_refresh.refresh_tmdb_info",
            "Movies",
            movie.id,
            movie.tmdb_id,
            apply_at_front=True,
            # The page waits for this refresh. Thus, it goes to the front
            # of the sql queue, not behind the bulk refreshes of the
            # request queue.
            queue="sql",
            description=f"Refreshing TMDB data for '{movie.title} ({movie.year})'",
            at_front=True,
        )
        _flash_tmdb_refresh(
            _wait_for_refresh(refresh_job),
            f"{movie.title} ({movie.year})",
            url_for("main.movie", movie_id=movie.id),
        )
        return redirect(url_for("main.movie", movie_id=movie.id))

    # A blank id asks TMDB to search by title, and takes the first hit. On
    # a film that is already matched, this silently points the film at a
    # different movie. Offer that only for a film with nothing to lose. A
    # detached film has something to lose: the detachment itself. The
    # detachment promised "not until you enter one by hand".

    if tmdb_lookup_form.tmdb_id.data is None and (
        movie.tmdb_id is not None or movie.tmdb_ignored
    ):
        flash(
            "Enter a TMDB ID to refresh this movie, or use "
            "'Remove TMDB ID' to detach it from TMDB",
            "warning",
        )
        return redirect(url_for("main.movie", movie_id=movie.id))

    # An id entered by hand is an intentional new match. It undoes a
    # previous removal. Only an actual id clears the flag. A blank submit
    # cannot get here on a detached record, because of the guard. The
    # apply clears the flag when TMDB sends data for the id. A wrong id
    # leaves the record detached (#514).

    # Add a task to the fitzflix-sql queue to check TMDB and update the
    # database. Add it to the front of the queue, because the user added
    # it interactively.

    refresh_job = enqueue_task(
        "app.metadata.tmdb_refresh.refresh_tmdb_info",
        "Movies",
        movie.id,
        tmdb_lookup_form.tmdb_id.data,
        apply_at_front=True,
        rematch=tmdb_lookup_form.tmdb_id.data is not None,
        # The page waits for this refresh. Thus, it goes to the front of
        # the sql queue, not behind the bulk refreshes of the request
        # queue.
        queue="sql",
        description=f"Refreshing TMDB data for '{movie.title} ({movie.year})'",
        at_front=True,
    )

    # The requested TMDB ID can belong to a movie in the database. Then go
    # to the page of that movie. This movie gets its TMDB data. A blank ID
    # refreshes this movie. filter_by(tmdb_id=None)
    # would match any unmatched movie.

    existing_tmdb_movie = (
        Movie.query.filter_by(tmdb_id=tmdb_lookup_form.tmdb_id.data).first()
        if tmdb_lookup_form.tmdb_id.data is not None
        else None
    )
    target_id = existing_tmdb_movie.id if existing_tmdb_movie else movie.id
    _flash_tmdb_refresh(
        _wait_for_refresh(refresh_job),
        f"{movie.title} ({movie.year})",
        url_for("main.movie", movie_id=target_id),
    )
    return redirect(url_for("main.movie", movie_id=target_id))


@bp.route("/movie/<int:movie_id>/criterion", methods=["POST"])
@login_required
@admin_required
def movie_criterion(movie_id):
    """Update the Criterion Collection details of a film by hand, for an admin."""

    movie = Movie.query.filter_by(id=movie_id).first_or_404()
    criterion_form = _criterion_form(movie)
    if not (
        criterion_form.criterion_submit.data and criterion_form.validate_on_submit()
    ):
        flash_form_errors(criterion_form)
        return redirect(url_for("main.movie", movie_id=movie.id))
    movie.criterion_spine_number = criterion_form.spine_number.data
    record_set_title(movie, criterion_form.set_title.data)

    movie.criterion_in_print = criterion_form.in_print.data
    movie.criterion_disc_owned = criterion_form.owned.data
    movie.criterion_quality_id = (
        int(criterion_form.quality.data) if criterion_form.quality.data else None
    )

    db.session.commit()
    flash(f"Updated Criterion Collection details for '{movie.display_title}'")
    return redirect(url_for("main.movie", movie_id=movie.id))


@bp.route("/movie/<int:movie_id>/play", methods=["POST"])
@login_required
def movie_play(movie_id):
    """Start this movie on the playback device of the current user.

    The device comes from the Profile-page settings. It is the Plex app
    through Plex Companion, or Infuse through an Apple-Companion deep
    link (#192). The buttons of the movie page name their app in the
    "player" field. A plain post (the poster popovers) uses the default
    of the user. It falls back to the other app when the default cannot
    play this movie. A Plex play of a film whose formats only Infuse
    handles includes the recommendation in its status message. Thus,
    the popover plays get it too. A background post gets JSON. A plain
    form post falls back to flash-and-redirect.
    """

    # The play forms carry the csrf token. Each other mutating route
    # checks the token through its FlaskForm. This route reads the form
    # by hand. Thus, it validates the token by hand (security review,
    # 2026-09).
    try:
        validate_csrf(request.form.get("csrf_token"))
    except ValidationError:
        if request.headers.get("X-Requested-With") == "play":
            return (
                jsonify(
                    {
                        "ok": False,
                        "message": "This page is stale. Reload it and try again.",
                    }
                ),
                400,
            )
        flash("That page was out of date. Try again.", "warning")
        return redirect(url_for("main.movie", movie_id=movie_id))

    movie = Movie.query.filter_by(id=movie_id).first_or_404()
    infuse_possible = bool(movie.tmdb_id) and current_user.infuse_player_configured
    plex_possible = current_user.plex_player_configured and remote_playback_configured()
    player = request.form.get("player") or current_user.preferred_player or "plex"
    if player == "infuse" and not infuse_possible and plex_possible:
        player = "plex"
    elif player == "plex" and not plex_possible and infuse_possible:
        player = "infuse"

    if player == "infuse":
        ok, message = infuse_play_movie(movie, current_user)
    else:
        ok, message = play_movie(movie, current_user)
        if ok and infuse_possible:
            films = (
                File.query.filter(File.movie_id == movie.id)
                .filter(File.feature_type_id == None)
                .all()
            )
            reasons = infuse_only_formats(films)
            if reasons:
                message += (
                    f" Heads-up: this film's {' and '.join(reasons)} only "
                    "play correctly in Infuse."
                )
    if request.headers.get("X-Requested-With") == "play":
        return jsonify({"ok": ok, "message": message}), 200 if ok else 502
    flash(message)
    return redirect(url_for("main.movie", movie_id=movie.id))


@bp.route("/movie/<int:movie_id>/files")
@login_required
@admin_required
def movie_files(movie_id):
    """Show all files for a particular movie, regardless of ranking.

    This is an admin page (#186 follow-up). Each row links into the
    file management pages. Those pages are admin tools."""

    movie = Movie.query.filter_by(id=movie_id).first_or_404()
    title = f'Files for "{movie.display_title}"'

    # This subquery gets the ranking of each file of this movie.

    ranked_files = ranked_movie_files()

    files = (
        db.session.query(File, Movie, RefQuality, RefFeatureType, ranked_files.c.rank)
        .join(Movie, (Movie.id == File.movie_id))
        .join(RefQuality, (RefQuality.id == File.quality_id))
        .join(ranked_files, (ranked_files.c.id == File.id))
        .outerjoin(RefFeatureType, (RefFeatureType.id == File.feature_type_id))
        .filter(Movie.id == movie_id)
        .order_by(File.feature_type_id.asc(), File.plex_title.asc(), *MOVIE_BEST_ORDER)
        .all()
    )

    return render_template("movie_files.html", title=title, movie=movie, files=files)
