import csv
import io
import json
import math
import os
import secrets

from datetime import datetime

import boto3
import botocore

from botocore.client import Config
from rq.job import Job
from rq.registry import StartedJobRegistry, ScheduledJobRegistry

from flask import (
    render_template,
    flash,
    jsonify,
    redirect,
    url_for,
    request,
    send_from_directory,
    Markup,
)
from flask_login import current_user, login_user, logout_user, login_required
from werkzeug.urls import url_parse

from app import db
from app.main.forms import (
    CriterionFilterForm,
    CriterionForm,
    CriterionRefreshForm,
    EditProfileForm,
    FileDeleteForm,
    ImportForm,
    LibrarySearchForm,
    MKVMergeForm,
    MKVPropEditForm,
    MovieReviewForm,
    MovieShoppingExcludeForm,
    MovieShoppingFilterForm,
    SyncAWSStorageForm,
    QualityFilterForm,
    ReviewExportForm,
    ReviewUploadForm,
    S3DownloadForm,
    S3UploadForm,
    TMDBLookupForm,
    TMDBRefreshForm,
    TrackMetadataScanForm,
    TranscodeForm,
    TVShoppingFilterForm,
    UpdateAPIKeyForm,
)
from app.models import (
    File,
    FileAudioTrack,
    FileSubtitleTrack,
    Movie,
    MovieCast,
    RefFeatureType,
    RefQuality,
    TMDBCredit,
    TVSeries,
    User,
    UserMovieReview,
)
from app.main import bp
from app.email import send_email
from app.videos import *


@bp.route("/browserconfig.xml")
def browserconfigXml():
    return send_from_directory(
        os.path.join(current_app.root_path, "static"),
        "browserconfig.xml",
        mimetype="image/png",
    )


@bp.route("/mstile-150x150.png")
def mstilePng():
    return send_from_directory(
        os.path.join(current_app.root_path, "static"),
        "mstile-150x150.png",
        mimetype="image/png",
    )


@bp.route("/apple-touch-icon-precomposed.png")
@bp.route("/apple-touch-icon.png")
def androidPng():
    return send_from_directory(
        os.path.join(current_app.root_path, "static"),
        "apple-touch-icon.png",
        mimetype="image/png",
    )


@bp.route("/favicon.ico")
def favicon():
    return send_from_directory(
        os.path.join(current_app.root_path, "static"),
        "favicon.ico",
        mimetype="image/vnd.microsoft.icon",
    )


@bp.route("/", methods=["GET", "POST"])
@bp.route("/index", methods=["GET", "POST"])
@bp.route("/recently-added", methods=["GET", "POST"])
@login_required
def index():
    """Show the ten most recently added files."""

    page = request.args.get("page", 1, type=int)

    last_week = (
        File.query.join(FileAudioTrack, (FileAudioTrack.file_id == File.id))
        .outerjoin(Movie, (Movie.id == File.movie_id))
        .outerjoin(TVSeries, (TVSeries.id == File.series_id))
        .filter(
            db.func.coalesce(File.date_updated, File.date_added)
            >= db.func.adddate(db.func.now(), -7)
        )
        .order_by(db.func.coalesce(File.date_updated, File.date_added).desc())
    )

    last_ten = (
        File.query.join(FileAudioTrack, (FileAudioTrack.file_id == File.id))
        .outerjoin(Movie, (Movie.id == File.movie_id))
        .order_by(db.func.coalesce(File.date_updated, File.date_added).desc())
        .limit(10)
    )

    recently_added = (
        last_week.union(last_ten)
        .order_by(db.func.coalesce(File.date_updated, File.date_added).desc())
        .paginate(page, 100, False)
    )

    next_url = (
        url_for("main.index", page=recently_added.next_num)
        if recently_added.has_next
        else None
    )
    prev_url = (
        url_for("main.index", page=recently_added.prev_num)
        if recently_added.has_prev
        else None
    )

    form = ImportForm()
    if form.validate_on_submit():
        file = import_video()
        flash(f"Importing '{file}'...")
        return redirect(url_for("main.index"))

    return render_template(
        "recently_added.html",
        title="Recently Added",
        recently_added=recently_added.items,
        native_language=[current_app.config["NATIVE_LANGUAGE"], "und", "zxx"],
        form=form,
        next_url=next_url,
        prev_url=prev_url,
        pages=recently_added,
    )


@bp.route("/library/movie", methods=["GET", "POST"])
@login_required
def movie_library():
    """Show the best quality version of each movie in the library.

    Possible user queries:
    - credit: get the id of an actor and filter the movie list for only the films they
              starred in
    - q     : filter the movie list for only the films that contain this substring
    """

    page = request.args.get("page", 1, type=int)
    credit = request.args.get("credit", None, type=int)
    q = request.args.get("q", None, type=str)
    quality = request.args.get("quality", "0", type=str)

    # Subquery to get the best movie files

    ranked_files = (
        db.session.query(
            File.id,
            db.func.row_number()
            .over(
                partition_by=(Movie.id, File.plex_title, File.version),
                order_by=(File.fullscreen.asc(), RefQuality.preference.desc()),
            )
            .label("rank"),
        )
        .join(Movie, (Movie.id == File.movie_id))
        .join(RefQuality, (RefQuality.id == File.quality_id))
        .subquery()
    )

    if credit:
        person = TMDBCredit.query.filter_by(id=int(credit)).first_or_404()
        title = f"Movies starring {person.name}"
        movies = (
            db.session.query(File, Movie, RefQuality)
            .join(Movie, (Movie.id == File.movie_id))
            .join(RefQuality, (RefQuality.id == File.quality_id))
            .join(ranked_files, (ranked_files.c.id == File.id))
            .join(MovieCast, (MovieCast.movie_id == Movie.id))
            .filter(File.feature_type_id == None)
            .filter(ranked_files.c.rank == 1)
            .filter(MovieCast.credit_id == int(credit))
            .order_by(
                db.case(
                    [(Movie.tmdb_title != None, Movie.tmdb_release_date)],
                    else_=Movie.year,
                ).asc(),
                db.case(
                    [(Movie.tmdb_title != None, Movie.tmdb_title)], else_=Movie.title
                ).asc(),
                File.version.asc(),
            )
            .paginate(page, 120, False)
        )

    elif q:
        title = f"Movies matching '{q}'"
        q = q.replace(" ", "%")
        movies = (
            db.session.query(File, Movie, RefQuality)
            .join(Movie, (Movie.id == File.movie_id))
            .join(RefQuality, (RefQuality.id == File.quality_id))
            .join(ranked_files, (ranked_files.c.id == File.id))
            .filter(File.feature_type_id == None)
            .filter(ranked_files.c.rank == 1)
            .filter(
                db.or_(Movie.title.ilike(f"%{q}%"), Movie.tmdb_title.ilike(f"%{q}%"))
            )
            .order_by(
                db.func.regexp_replace(
                    db.case(
                        [(Movie.tmdb_title != None, Movie.tmdb_title)],
                        else_=Movie.title,
                    ),
                    "^(The|A|An)\s",
                    "",
                ).asc(),
                db.case(
                    [(Movie.tmdb_title != None, Movie.tmdb_release_date)],
                    else_=Movie.year,
                ).asc(),
                File.version.asc(),
            )
            .paginate(page, 120, False)
        )

    elif int(quality) > 0:
        title = "Movie Library"
        movies = (
            db.session.query(File, Movie, RefQuality)
            .join(Movie, (Movie.id == File.movie_id))
            .join(RefQuality, (RefQuality.id == File.quality_id))
            .join(ranked_files, (ranked_files.c.id == File.id))
            .filter(File.feature_type_id == None)
            .filter(ranked_files.c.rank == 1)
            .filter(RefQuality.id == int(quality))
            .order_by(
                db.func.regexp_replace(
                    db.case(
                        [(Movie.tmdb_title != None, Movie.tmdb_title)],
                        else_=Movie.title,
                    ),
                    "^(The|A|An)\s",
                    "",
                ).asc(),
                db.case(
                    [(Movie.tmdb_title != None, Movie.tmdb_release_date)],
                    else_=Movie.year,
                ).asc(),
                File.version.asc(),
            )
            .paginate(page, 120, False)
        )

    else:
        title = "Movie Library"
        movies = (
            db.session.query(File, Movie, RefQuality)
            .join(Movie, (Movie.id == File.movie_id))
            .join(RefQuality, (RefQuality.id == File.quality_id))
            .join(ranked_files, (ranked_files.c.id == File.id))
            .filter(File.feature_type_id == None)
            .filter(ranked_files.c.rank == 1)
            .order_by(
                db.func.regexp_replace(
                    db.case(
                        [(Movie.tmdb_title != None, Movie.tmdb_title)],
                        else_=Movie.title,
                    ),
                    "^(The|A|An)\s",
                    "",
                ).asc(),
                db.case(
                    [(Movie.tmdb_title != None, Movie.tmdb_release_date)],
                    else_=Movie.year,
                ).asc(),
                File.version.asc(),
            )
            .paginate(page, 120, False)
        )

    next_url = (
        url_for("main.movie_library", page=movies.next_num, quality=quality)
        if movies.has_next
        else None
    )
    prev_url = (
        url_for("main.movie_library", page=movies.prev_num, quality=quality)
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
        return redirect(url_for("main.movie_library", quality=filter_form.quality.data))

    filter_form.process()

    # Form to search the movie library titles for a specific substring

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


@bp.route("/library/criterion-collection", methods=["GET", "POST"])
@login_required
def criterion_collection():
    """Show all films in the library that have a Criterion Collection release."""

    # Get the page filter status
    # - all  : show all movies that have a Criterion Collection release, whether or not
    #          I own the disc
    # - owned: show only those movies where I actually own the Criterion Collection disc

    filter_status = request.form.get("filter_status", "all")

    # Form to filter the Criterion Collection listing

    filter_form = CriterionFilterForm()

    # Set the filter status default to what was last submitted
    filter_form.filter_status.default = filter_status
    filter_form.process()

    if filter_form.validate_on_submit():
        return redirect(url_for("main.criterion_collection"))

    if filter_status == "owned":
        owned = True

    else:
        owned = False

    # Subquery to get the best movie files

    ranked_files = (
        db.session.query(
            File.id,
            db.func.row_number()
            .over(
                partition_by=(Movie.id, File.plex_title, File.version),
                order_by=(File.fullscreen.asc(), RefQuality.preference.desc()),
            )
            .label("rank"),
        )
        .join(Movie, (Movie.id == File.movie_id))
        .join(RefQuality, (RefQuality.id == File.quality_id))
        .subquery()
    )

    movies = (
        db.session.query(File, Movie, RefQuality)
        .join(Movie, (Movie.id == File.movie_id))
        .join(RefQuality, (RefQuality.id == File.quality_id))
        .join(ranked_files, (ranked_files.c.id == File.id))
        .filter(File.feature_type_id == None)
        .filter(ranked_files.c.rank == 1)
        .filter(
            db.or_(
                Movie.criterion_spine_number != None, Movie.criterion_set_title != None
            )
        )
        .filter(
            db.or_(
                Movie.criterion_disc_owned == True, Movie.criterion_disc_owned == owned
            )
        )
        .order_by(
            Movie.criterion_spine_number.asc(),
            Movie.criterion_set_title.asc(),
            db.case(
                [(Movie.tmdb_title != None, Movie.tmdb_release_date)], else_=Movie.year
            ).asc(),
            db.func.regexp_replace(
                db.case(
                    [(Movie.tmdb_title != None, Movie.tmdb_title)], else_=Movie.title
                ),
                "^(The|A|An)\s",
                "",
            ).asc(),
            File.version.asc(),
        )
        .all()
    )

    return render_template(
        "library_criterion.html",
        title="Criterion Collection films",
        movies=movies,
        filter_form=filter_form,
    )


@bp.route("/movie/<movie_id>", methods=["GET", "POST"])
@login_required
def movie(movie_id):
    """Show details for a particular movie."""

    movie = Movie.query.filter_by(id=int(movie_id)).first_or_404()
    title = f"{movie.tmdb_title if movie.tmdb_title else movie.title} ({movie.tmdb_release_date.strftime('%Y') if movie.tmdb_title else movie.year})"
    starring_roles = (
        MovieCast.query.filter(MovieCast.movie_id == int(movie_id))
        .filter(MovieCast.billing_order <= 2)
        .order_by(MovieCast.billing_order.asc())
        .all()
    )
    genres = [genre.name for genre in movie.genres]
    rating = [
        certification.certification
        for certification in movie.certifications
        if certification.country == "US"
    ]
    review = (
        UserMovieReview.query.filter_by(user_id=int(current_user.id), movie_id=movie.id)
        .order_by(UserMovieReview.date_reviewed.desc())
        .first()
    )
    films = (
        File.query.join(RefQuality, (RefQuality.id == File.quality_id))
        .filter(File.movie_id == int(movie_id))
        .filter(File.feature_type_id == None)
        .order_by(
            File.fullscreen.asc(), File.version.asc(), RefQuality.preference.desc()
        )
        .all()
    )
    features = (
        File.query.filter(File.movie_id == int(movie_id))
        .filter(File.feature_type_id != None)
        .order_by(File.basename.asc())
        .all()
    )

    movie_shopping_exclude_form = MovieShoppingExcludeForm()
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

    # Form to review a movie. A user can review the same movie multiple times
    # (tastes change!), so this just adds an additional review to the UserMovieReview
    # table for this film.

    movie_review_form = MovieReviewForm(date_watched=datetime.now())
    if movie_review_form.review_submit.data and movie_review_form.validate_on_submit():
        # Users can rate a movie from 0-5. While a user can use decimals, we can only
        # able to display their review with whole or half stars, so here round the rating
        # to the nearest 0.5, and use that to determine the number of whole stars to
        # display, and the number of half stars to display.

        modified_rating = round(movie_review_form.rating.data * 2) / 2
        whole_stars = math.floor(modified_rating)
        if modified_rating % 1 == 0:
            half_stars = 0
        else:
            half_stars = 1

        review = UserMovieReview(
            user_id=current_user.id,
            movie_id=movie.id,
            rating=movie_review_form.rating.data,
            modified_rating=modified_rating,
            whole_stars=whole_stars,
            half_stars=half_stars,
            review=movie_review_form.review.data,
            date_watched=movie_review_form.date_watched.data,
            date_reviewed=datetime.utcnow(),
        )
        db.session.add(review)
        db.session.commit()
        flash(
            f"Rated '{title}' {movie_review_form.rating.data} out of 5 stars", "success"
        )
        return redirect(url_for("main.movie", movie_id=movie.id))

    # Form to request all the files for this movie to be transcoded

    transcode_form = TranscodeForm()
    # TODO: Get the best files for this movie and pass them to the transcoding task
    # if transcode_form.validate_on_submit():
    #         current_app.transcode_queue.enqueue(
    #             "app.videos.transcode_task",
    #             args=(file.id,),
    #             job_timeout=current_app.config["TRANSCODE_TASK_TIMEOUT"],
    #             description=f"'{file.plex_title}'",
    #         )
    #         flash(f"Added '{file.plex_title}' to transcoding queue", "success")
    #         return redirect(url_for("main.file", file_id=file.id))

    # Form to update a movie's information with the latest TMDb data

    tmdb_lookup_form = TMDBLookupForm()
    if tmdb_lookup_form.lookup_submit.data and tmdb_lookup_form.validate_on_submit():
        # Add a task to the fitzflix-sql queue to check TMDb and update the database;
        # add it to the front of the queue since it's interactively added by the user

        refresh_job = current_app.sql_queue.enqueue(
            "app.videos.refresh_tmdb_info",
            args=("Movies", movie.id, tmdb_lookup_form.tmdb_id.data),
            job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
            description=f"Refreshing TMDB data for '{movie.title} ({movie.year})'",
            at_front=True,
        )

        # See if the requested TMDb ID already exists in the database;
        # if so, since we're updating this movie with that movie's TMDb data,
        # redirect to that movie's info page

        existing_tmdb_movie = Movie.query.filter_by(
            tmdb_id=tmdb_lookup_form.tmdb_id.data
        ).first()
        if existing_tmdb_movie:
            movie_id = existing_tmdb_movie.id

        else:
            movie_id = movie.id

        # Check the status of the refresh job every second. If the TMDb refresh process
        # completed within 10 seconds, redirect to the updated page, otherwise redirect
        # to the existing page and give the user a link to reload the page.

        waited_seconds = 0
        while refresh_job.result == None and waited_seconds < 10:
            time.sleep(1)
            waited_seconds = waited_seconds + 1

        if refresh_job.result:
            flash(f"Refreshed TMDb data for '{movie.title} ({movie.year})'", "success")

        else:
            flash(
                Markup(
                    "Refreshing TMDb data for '{}' ({}) – <a href='{}'>Reload this page</a>"
                ).format(
                    movie.title, movie.year, url_for("main.movie", movie_id=movie_id)
                ),
                "info",
            )

        return redirect(url_for("main.movie", movie_id=movie_id))

    # Form to manually update a movie's Criterion Collection information

    criterion_form = CriterionForm()
    if criterion_form.criterion_submit.data and criterion_form.validate_on_submit():
        movie.criterion_spine_number = criterion_form.spine_number.data
        if criterion_form.set_title.data:
            movie.criterion_set_title = criterion_form.set_title.data

        else:
            movie.criterion_set_title = None

        movie.criterion_in_print = criterion_form.in_print.data
        movie.criterion_bluray = criterion_form.bluray_release.data
        movie.criterion_disc_owned = criterion_form.owned.data

        db.session.commit()
        flash(f"Updated Criterion Collection details for '{title}'")
        return redirect(url_for("main.movie", movie_id=movie.id))

    return render_template(
        "movie.html",
        title=title,
        movie=movie,
        starring_roles=starring_roles,
        genres=genres,
        review=review,
        films=films,
        features=features,
        movie_shopping_exclude_form=movie_shopping_exclude_form,
        movie_review_form=movie_review_form,
        transcode_form=transcode_form,
        tmdb_lookup_form=tmdb_lookup_form,
        criterion_form=criterion_form,
        radarr_proxy_url=current_app.config["RADARR_PROXY_URL"],
    )


@bp.route("/movie/<movie_id>/files")
@login_required
def movie_files(movie_id):
    """Show all files for a particular movie, regardless of ranking."""

    movie = Movie.query.filter_by(id=int(movie_id)).first_or_404()
    title = f"Files for \"{movie.tmdb_title if movie.tmdb_title else movie.title} ({movie.tmdb_release_date.strftime('%Y') if movie.tmdb_title else movie.year})\""

    # Subquery to get the ranking for each of this movie's files

    ranked_files = (
        db.session.query(
            File.id,
            db.func.row_number()
            .over(
                partition_by=(Movie.id, File.plex_title, File.version),
                order_by=(File.fullscreen.asc(), RefQuality.preference.desc()),
            )
            .label("rank"),
        )
        .join(Movie, (Movie.id == File.movie_id))
        .join(RefQuality, (RefQuality.id == File.quality_id))
        .subquery()
    )

    files = (
        db.session.query(File, Movie, RefQuality, RefFeatureType, ranked_files.c.rank)
        .join(Movie, (Movie.id == File.movie_id))
        .join(RefQuality, (RefQuality.id == File.quality_id))
        .join(ranked_files, (ranked_files.c.id == File.id))
        .outerjoin(RefFeatureType, (RefFeatureType.id == File.feature_type_id))
        .filter(Movie.id == int(movie_id))
        .order_by(
            File.feature_type_id.asc(),
            File.plex_title.asc(),
            RefQuality.preference.desc(),
        )
        .all()
    )

    return render_template("movie_files.html", title=title, movie=movie, files=files)


@bp.route("/library/tv")
@login_required
def tv_library():
    """Show the worst quality in each season for each TV show in the library."""

    # Subquery to get the number of episodes we have for in each season,
    # and the worst quality for each season

    subquery = (
        db.session.query(
            File.series_id,
            File.season,
            db.func.count(db.func.distinct(File.episode)).label("episodes"),
            db.func.min(RefQuality.preference).label("preference"),
        )
        .group_by(File.series_id, File.season)
        .join(RefQuality, (RefQuality.id == File.quality_id))
        .subquery()
    )

    tv = []
    for series in TVSeries.query.order_by(
        db.func.regexp_replace(TVSeries.title, "^(The|A|An)\s", "").asc()
    ).all():
        seasons = []
        s = (
            db.session.query(
                subquery.c.season, subquery.c.episodes, RefQuality.quality_title
            )
            .join(RefQuality, (RefQuality.preference == subquery.c.preference))
            .filter(subquery.c.series_id == series.id)
            .order_by(
                db.case([(subquery.c.season == 0, 1)], else_=0).asc(),
                subquery.c.season.asc(),
            )
            .all()
        )
        for season, num_episodes, min_quality in s:
            seasons.append(
                {
                    "season": season,
                    "episode_count": num_episodes,
                    "min_quality": min_quality,
                }
            )

        tv.append(
            {
                "id": series.id,
                "title": series.title,
                "tmdb_id": series.tmdb_id,
                "tmdb_name": series.tmdb_name,
                "tmdb_poster_path": series.tmdb_poster_path,
                "seasons": seasons,
            }
        )

    return render_template("library_tv.html", title="TV Library", series=tv)


@bp.route("/tv/<series_id>", methods=["GET", "POST"])
@login_required
def tv(series_id):
    """Show details for a particular TV series."""

    tv = TVSeries.query.filter_by(id=int(series_id)).first_or_404()
    title = f"{tv.tmdb_name if tv.tmdb_name else tv.title}"
    seasons = []
    for file in tv.files:
        seasons.append(file.season)

    seasons.sort()
    seasons = list(set(seasons))

    # Form to request all the files for this TV series to be transcoded

    transcode_form = TranscodeForm()
    if transcode_form.transcode_all.data and transcode_form.validate_on_submit():
        # Subquery to get the best files for this TV series

        ranked_files = (
            db.session.query(
                File.id,
                db.func.row_number()
                .over(
                    partition_by=(TVSeries.id, File.season, File.episode, File.version),
                    order_by=(File.fullscreen.asc(), RefQuality.preference.desc()),
                )
                .label("rank"),
            )
            .join(TVSeries, (TVSeries.id == File.series_id))
            .join(RefQuality, (RefQuality.id == File.quality_id))
            .subquery()
        )

        # Get details for all the best files for this TV series

        files = (
            File.query.join(ranked_files, (ranked_files.c.id == File.id))
            .filter(File.series_id == int(series_id))
            .filter(ranked_files.c.rank == 1)
            .order_by(File.season.asc(), File.episode.asc())
            .all()
        )

        # Enqueue a transcode task for each best file for this TV show

        for file in files:
            current_app.transcode_queue.enqueue(
                "app.videos.transcode_task",
                args=(file.id,),
                job_timeout=current_app.config["TRANSCODE_TASK_TIMEOUT"],
                description=f"'{file.plex_title}'",
                job_id=file.plex_title,
            )

        flash(f"Added all files for '{title}' to transcoding queue", "success")
        return redirect(url_for("main.tv", series_id=tv.id))

    # Form to update a TV series' information with the latest TMDb data

    tmdb_lookup_form = TMDBLookupForm()
    if tmdb_lookup_form.lookup_submit.data and tmdb_lookup_form.validate_on_submit():
        # Add a task to the fitzflix-sql queue to check TMDb and update the database;
        # add it to the front of the queue since it's interactively added by the user

        refresh_job = current_app.sql_queue.enqueue(
            "app.videos.refresh_tmdb_info",
            args=("TV Shows", tv.id, tmdb_lookup_form.tmdb_id.data),
            job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
            description=f"Refreshing TMDB data for '{tv.title}'",
            at_front=True,
        )

        # See if the requested TMDb ID already exists in the database;
        # if so, since we're updating this TV series with that show's TMDb data,
        # redirect to that show's info page

        existing_tmdb_tv = TVSeries.query.filter_by(
            tmdb_id=tmdb_lookup_form.tmdb_id.data
        ).first()
        if existing_tmdb_tv:
            tv_id = existing_tmdb_tv.id

        else:
            tv_id = tv.id

        # Check the status of the refresh job every second. If the TMDb refresh process
        # completed within 10 seconds, redirect to the updated page, otherwise redirect
        # to the existing page and give the user a link to reload the page.

        waited_seconds = 0
        while refresh_job.result == None and waited_seconds < 10:
            time.sleep(1)
            waited_seconds = waited_seconds + 1

        if refresh_job.result:
            flash(f"Refreshed TMDb data for '{tv.title}'", "success")

        else:
            flash(
                Markup(
                    "Refreshing TMDb data for '{}' – <a href='{}'>Reload this page</a>"
                ).format(tv.title, url_for("main.tv", series_id=tv.id)),
                "info",
            )

        return redirect(url_for("main.tv", series_id=tv.id))

    return render_template(
        "tv.html",
        title=title,
        tv=tv,
        seasons=seasons,
        transcode_form=transcode_form,
        tmdb_lookup_form=tmdb_lookup_form,
    )


@bp.route("/tv/<series_id>/<season>")
@login_required
def season(series_id, season):
    """Show all files for a TV show's season, regardless of ranking."""

    tv = TVSeries.query.filter_by(id=int(series_id)).first_or_404()

    if season == 0:
        title = (
            f'Files for "{tv.tmdb_name if tv.tmdb_name else tv.title}" special episodes'
        )

    else:
        title = (
            f'Files for "{tv.tmdb_name if tv.tmdb_name else tv.title}", season {season}'
        )

    # Subquery to get the ranking for each of this season's files

    ranked_files = (
        db.session.query(
            File.id,
            db.func.row_number()
            .over(
                partition_by=(TVSeries.id, File.season, File.episode, File.version),
                order_by=(File.fullscreen.asc(), RefQuality.preference.desc()),
            )
            .label("rank"),
        )
        .join(TVSeries, (TVSeries.id == File.series_id))
        .join(RefQuality, (RefQuality.id == File.quality_id))
        .subquery()
    )

    # Query to get all of the files for this season

    files = (
        db.session.query(File, TVSeries, RefQuality, ranked_files.c.rank)
        .join(TVSeries, (TVSeries.id == File.series_id))
        .join(RefQuality, (RefQuality.id == File.quality_id))
        .join(ranked_files, (ranked_files.c.id == File.id))
        .filter(TVSeries.id == int(series_id))
        .filter(File.season == int(season))
        .order_by(
            File.episode.asc(), RefQuality.preference.desc(), File.last_episode.desc()
        )
        .all()
    )

    # Create a list of all of the episodes for this season,
    # and all of the files for each episode

    episode_numbers = []
    for file in files:
        episode_numbers.append(file.File.episode)

    episode_numbers.sort()
    episode_numbers = list(set(episode_numbers))
    episodes = []
    for ep_num in episode_numbers:
        this_episode = {"episode": ep_num, "files": []}
        for file in files:
            if file.File.episode == ep_num:
                this_episode["files"].append(
                    {
                        "id": file.File.id,
                        "basename": file.File.basename,
                        "quality_title": file.RefQuality.quality_title,
                        "quality_preference": file.RefQuality.preference,
                        "last_episode": file.File.last_episode,
                        "version": file.File.version,
                        "rank": file.rank,
                    }
                )

        episodes.append(this_episode)

    return render_template(
        "season.html", title=title, tv=tv, season=season, files=files
    )


@bp.route("/file/<file_id>", methods=["GET", "POST"])
@login_required
def file(file_id):
    """Show the details for a particular video file."""

    # if request.form:
    #         forced_subtitle_tracks = []
    #
    #     for key in request.form:
    #         current_app.logger.info(f"{key}: {request.form.getlist(key)}")

    #         if form_field == "forced_subtitles":
    #             forced_subtitle_tracks.append(form_value)

    #     current_app.logger.info(f"Forced subtitle tracks from the form: {forced_subtitle_tracks}")

    file = File.query.filter_by(id=int(file_id)).first_or_404()
    title = file.basename

    # Since the video file can be for either a movie or a tv show, determine which
    # it belongs to based off whether it has a movie_id or a series_id, get the
    # associated movie or tv series information

    if file.movie_id:
        movie = Movie.query.filter_by(id=int(file.movie_id)).first_or_404()
        tv = None
        file_rank = (
            db.session.query(
                File.id,
                db.func.row_number()
                .over(
                    partition_by=(Movie.id, File.plex_title, File.version),
                    order_by=(File.fullscreen.asc(), RefQuality.preference.desc()),
                )
                .label("rank"),
            )
            .join(Movie, (Movie.id == File.movie_id))
            .join(RefQuality, (RefQuality.id == File.quality_id))
            .subquery()
        )
        best_file = (
            db.session.query(
                File,
                db.case([(file_rank.c.rank == 1, 1)], else_=0).label("rank"),
            )
            .join(file_rank, (file_rank.c.id == File.id))
            .filter(File.id == int(file_id))
            .filter(file_rank.c.rank == 1)
            .first()
        )

    elif file.series_id:
        movie = None
        tv = TVSeries.query.filter_by(id=int(file.series_id)).first_or_404()
        file_rank = (
            db.session.query(
                File.id,
                db.func.row_number()
                .over(
                    partition_by=(TVSeries.id, File.season, File.episode, File.version),
                    order_by=(File.fullscreen.asc(), RefQuality.preference.desc()),
                )
                .label("rank"),
            )
            .join(TVSeries, (TVSeries.id == File.series_id))
            .join(RefQuality, (RefQuality.id == File.quality_id))
            .subquery()
        )
        best_file = (
            db.session.query(
                File,
                db.case([(file_rank.c.rank == 1, 1)], else_=0).label("rank"),
            )
            .join(file_rank, (file_rank.c.id == File.id))
            .filter(File.id == int(file_id))
            .filter(file_rank.c.rank == 1)
            .first()
        )

    # Get the details of each of the audio and subtitle tracks for this file

    audio_tracks = FileAudioTrack.query.filter_by(file_id=file.id).all()
    subtitle_tracks = FileSubtitleTrack.query.filter_by(file_id=file.id).all()

    # Form to rescan the file's metadata

    metadata_scan_form = TrackMetadataScanForm()

    if metadata_scan_form.scan_submit.data and metadata_scan_form.validate_on_submit():
        track_metadata_scan(file.id)
        # Enqueue a scan task for this file

        #         current_app.file_queue.enqueue(
        #             "app.videos.track_metadata_scan_task",
        #             args=(file.id,),
        #             job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
        #         )
        flash(f"Rescanned track metadata for '{file.basename}'", "info")
        return redirect(url_for("main.file", file_id=file.id))

    # Form to edit the file's attributes

    mkvpropedit_form = MKVPropEditForm()

    default_audio_choices = []
    default_audio_track_number = 1
    for audio_track in audio_tracks:
        if audio_track.compression_mode == "Lossless" and audio_track.bit_depth and audio_track.sampling_rate_khz:
            default_audio_choices.append(
                (
                    audio_track.track,
                    f"{audio_track.language_name}: {audio_track.codec} {audio_track.channels} ({audio_track.bit_depth}-bit {audio_track.sampling_rate_khz} khz)",
                )
            )
        elif audio_track.bitrate_kbps:
            default_audio_choices.append(
                (
                    audio_track.track,
                    f"{audio_track.language_name}: {audio_track.codec} {audio_track.channels} ({audio_track.bitrate_kbps} kbps)",
                )
            )
        else:
            default_audio_choices.append(
                (
                    audio_track.track,
                    f"{audio_track.language_name}: {audio_track.codec} {audio_track.channels}",
                )
            )

        if audio_track.default == True:
            default_audio_track_number = audio_track.track

    mkvpropedit_form.default_audio.choices = default_audio_choices
    mkvpropedit_form.default_audio.default = default_audio_track_number

    default_subtitle_choices = [(0, "None")]
    default_subtitle_track_number = 0

    forced_subtitle_choices = []
    default_forced_subtitles = []

    for subtitle_track in subtitle_tracks:
        default_subtitle_choices.append(
            (
                subtitle_track.track,
                f"{subtitle_track.elements}-element {subtitle_track.language_name}",
            )
        )
        if subtitle_track.default == True:
            default_subtitle_track_number = subtitle_track.track

        forced_subtitle_choices.append(
            (
                subtitle_track.track,
                f"{subtitle_track.elements}-element {subtitle_track.language_name}",
            )
        )
        if subtitle_track.forced == True:
            default_forced_subtitles.append(subtitle_track.track)

    mkvpropedit_form.default_subtitle.choices = default_subtitle_choices
    mkvpropedit_form.default_subtitle.default = default_subtitle_track_number

    mkvpropedit_form.forced_subtitles.choices = forced_subtitle_choices
    mkvpropedit_form.forced_subtitles.default = default_forced_subtitles

    if mkvpropedit_form.mkvpropedit_submit.data:
        current_app.logger.debug(
            f"Default audio: {mkvpropedit_form.default_audio.data}"
        )
        current_app.logger.debug(
            f"Default subtitle: {mkvpropedit_form.default_subtitle.data}"
        )
        current_app.logger.debug(
            f"Forced subtitles: {mkvpropedit_form.forced_subtitles.data}"
        )

        if file.container == "Matroska":
            mkvpropedit_job = current_app.file_queue.enqueue(
                "app.videos.mkvpropedit_task",
                args=(
                    file.id,
                    mkvpropedit_form.default_audio.data,
                    mkvpropedit_form.default_subtitle.data,
                    mkvpropedit_form.forced_subtitles.data,
                ),
                job_timeout=current_app.config["MKVPROPEDIT_TASK_TIMEOUT"],
                description=f"'{file.basename}'",
            )
            if mkvpropedit_job:
                current_app.logger.info(
                    f"Queued '{file.basename}' for MKV property edits"
                )

            flash(f"Updating MKV properties for '{file.basename}'", "info")

        else:
            flash(
                f"Unable to update MKV properties for '{file.basename}' since it is not an MKV file!",
                "danger",
            )

        return redirect(url_for("main.file", file_id=file.id))

    mkvpropedit_form.process()

    # Form to remux the file minus certain tracks

    mkvmerge_form = MKVMergeForm()

    audio_track_choices = []
    default_audio_tracks = []

    subtitle_track_choices = []
    default_subtitle_tracks = []

    for audio_track in audio_tracks:
        if audio_track.compression_mode == "Lossless" and audio_track.bit_depth and audio_track.sampling_rate_khz:
            audio_track_choices.append(
                (
                    audio_track.track,
                    f"{audio_track.language_name}: {audio_track.codec} {audio_track.channels} ({audio_track.bit_depth}-bit {audio_track.sampling_rate_khz} khz)",
                )
            )
        elif audio_track.bitrate_kbps:
            audio_track_choices.append(
                (
                    audio_track.track,
                    f"{audio_track.language_name}: {audio_track.codec} {audio_track.channels} ({audio_track.bitrate_kbps} kbps)",
                )
            )
        else:
            audio_track_choices.append(
                (
                    audio_track.track,
                    f"{audio_track.language_name}: {audio_track.codec} {audio_track.channels}",
                )
            )
        default_audio_tracks.append(audio_track.track)

    for subtitle_track in subtitle_tracks:
        subtitle_track_choices.append(
            (
                subtitle_track.track,
                f"{subtitle_track.elements}-element {subtitle_track.language_name}",
            )
        )
        default_subtitle_tracks.append(subtitle_track.track)

    mkvmerge_form.audio_tracks.choices = audio_track_choices
    mkvmerge_form.audio_tracks.default = default_audio_tracks

    mkvmerge_form.subtitle_tracks.choices = subtitle_track_choices
    mkvmerge_form.subtitle_tracks.default = default_subtitle_tracks

    if mkvmerge_form.mkvmerge_submit.data:
        current_app.logger.info(f"Audio tracks: {mkvmerge_form.audio_tracks.data}")
        current_app.logger.info(
            f"Subtitle tracks: {mkvmerge_form.subtitle_tracks.data}"
        )

        if file.container == "Matroska":
            mkvmerge_job = current_app.import_queue.enqueue(
                "app.videos.mkvmerge_task",
                args=(
                    file.id,
                    mkvmerge_form.audio_tracks.data,
                    mkvmerge_form.subtitle_tracks.data,
                ),
                job_timeout=current_app.config["LOCALIZATION_TASK_TIMEOUT"],
                description=f"'{file.basename}'",
                at_front=True,
            )
            if mkvmerge_job:
                current_app.logger.info(f"Queued '{file.basename}' for MKV remuxing")
            flash(f"Remuxing MKV file '{file.basename}'", "info")

        else:
            flash(
                f"Unable to remux '{file.basename}' since it is not an MKV file!",
                "danger",
            )

        return redirect(url_for("main.file", file_id=file.id))

    mkvmerge_form.process()

    # Form to request this file to be transcoded

    transcode_form = TranscodeForm()
    if transcode_form.transcode_submit.data and transcode_form.validate_on_submit():
        # Enqueue a transcode task for this file

        current_app.transcode_queue.enqueue(
            "app.videos.transcode_task",
            args=(file.id,),
            job_timeout=current_app.config["TRANSCODE_TASK_TIMEOUT"],
            description=f"'{file.plex_title}'",
            job_id=file.plex_title,
        )
        flash(f"Added '{file.plex_title}' to transcoding queue", "success")
        return redirect(url_for("main.file", file_id=file.id))

    # Form to request this file be uploaded to AWS S3 storage

    upload_form = S3UploadForm()
    if upload_form.s3_upload_submit.data and upload_form.validate_on_submit():
        # Enqueue an upload task for this file

        current_app.file_queue.enqueue(
            "app.videos.upload_task",
            args=(
                file.id,
                current_app.config["AWS_UNTOUCHED_PREFIX"],
                True,
            ),
            job_timeout=current_app.config["UPLOAD_TASK_TIMEOUT"],
            description=f"'{file.basename}'",
            at_front=True,
        )
        flash(f"Uploading '{file.basename}' to AWS S3 storage", "info")
        return redirect(url_for("main.file", file_id=file.id))

    download_form = S3DownloadForm()
    if download_form.s3_download_submit.data and download_form.validate_on_submit():
        # Enqueue a restore task for this file

        current_app.request_queue.enqueue(
            "app.videos.aws_restore",
            args=(file.aws_untouched_key,),
            job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
        )
        flash(
            f"Requesting '{file.untouched_basename}' to be restored from AWS Glacier",
            "info",
        )
        return redirect(url_for("main.file", file_id=file.id))

    # Form to delete and purge the file from the database

    delete_form = FileDeleteForm()
    if delete_form.delete_submit.data and delete_form.validate_on_submit():
        try:
            # TODO: Delete the archived version from S3
            # (For some reason, I can call the function in app.videos, but it silently fails)
            # For now, we just delete the local file and remove from the database;
            # the AWS file can be removed later as an unreferenced file
            # in the Admin page with the "Sync AWS Storage" button.

            # file.aws_untouched_date_deleted = aws_delete(file.aws_untouched_key)

            file.delete_local_file(delete_directory_tree=True)
            db.session.delete(file)

        except:
            db.session.rollback()
            flash(f"Unable to delete '{file.basename}'!", "danger")
            return redirect(url_for("main.file", file_id=file.id))

        db.session.commit()

        flash(f"Deleted '{file.basename}' and removed from database.", "success")

        if file.movie_id:
            return redirect(url_for("main.movie_files", movie_id=file.movie_id))

        elif file.series_id and file.season:
            return redirect(
                url_for("main.season", series_id=file.series_id, season=file.season)
            )

        else:
            return redirect(url_for("main.index"))

    return render_template(
        "file.html",
        file=file,
        title=title,
        movie=movie,
        tv=tv,
        audio_tracks=audio_tracks,
        subtitle_tracks=subtitle_tracks,
        metadata_scan_form=metadata_scan_form,
        mkvpropedit_form=mkvpropedit_form,
        mkvmerge_form=mkvmerge_form,
        transcode_form=transcode_form,
        upload_form=upload_form,
        download_form=download_form,
        delete_form=delete_form,
        best_file=best_file,
    )


@bp.route("/reviews", methods=["GET", "POST"])
@login_required
def reviews():
    """Display all of a user's movie reviews."""

    # Paginate a user's movie reviews, show 50 reviews per page

    page = request.args.get("page", 1, type=int)
    reviews = (
        UserMovieReview.query.join(Movie, (Movie.id == UserMovieReview.movie_id))
        .filter(UserMovieReview.user_id == int(current_user.id))
        .order_by(
            UserMovieReview.date_reviewed.desc(),
            UserMovieReview.rating.desc(),
            Movie.title.asc(),
        )
        .paginate(page, 50, False)
    )
    next_url = (
        url_for("main.reviews", page=reviews.next_num) if reviews.has_next else None
    )
    prev_url = (
        url_for("main.reviews", page=reviews.prev_num) if reviews.has_prev else None
    )

    all_reviews = (
        UserMovieReview.query.join(Movie, (Movie.id == UserMovieReview.movie_id))
        .filter(UserMovieReview.user_id == int(current_user.id))
        .order_by(UserMovieReview.date_reviewed.desc())
        .order_by(UserMovieReview.date_watched.desc())
        .all()
    )

    # Form to request an export of all of this user's movie reviews as a CSV file

    review_export_form = ReviewExportForm()
    if (
        review_export_form.export_submit.data
        and review_export_form.validate_on_submit()
    ):
        # Create the header columns for the CSV

        csv_export = [
            ["tmdbID", "imdbID", "Title", "Year", "Rating", "WatchedDate", "Review"]
        ]

        # Compile the list of this user's reviews for export

        review_export = (
            UserMovieReview.query.join(Movie, (Movie.id == UserMovieReview.movie_id))
            .filter(UserMovieReview.user_id == int(current_user.id))
            .order_by(
                UserMovieReview.date_watched.desc(),
                UserMovieReview.date_reviewed.desc(),
                UserMovieReview.rating.desc(),
            )
            .all()
        )
        for r in review_export:
            csv_export.append(
                [
                    r.movie.tmdb_id,
                    r.movie.imdb_id,
                    r.movie.title,
                    r.movie.year,
                    r.modified_rating,
                    r.date_watched,
                    r.review,
                ]
            )

        current_app.logger.debug(csv_export)

        # Write out the CSV file in memory, no need to write it out to disk

        f = io.StringIO()
        review_writer = csv.writer(f, quoting=csv.QUOTE_ALL)
        for review in csv_export:
            review_writer.writerow(review)

        # Send an email to the user with the CSV file as an attachment

        send_email(
            "Fitzflix - Your movie reviews",
            sender=("Fitzflix", current_app.config["SERVER_EMAIL"]),
            recipients=[current_user.email],
            text_body=render_template("email/reviews.txt", user=current_user),
            html_body=render_template("email/reviews.html", user=current_user),
            attachments=[("reviews.csv", "text/csv", f.getvalue())],
        )
        flash(f"Emailed your reviews to {current_user.email}", "success")

        # Discard the in-memory CSV file

        f.close()

        return redirect(url_for("main.reviews"))

    review_upload_form = ReviewUploadForm()
    if (
        review_upload_form.upload_submit.data
        and review_upload_form.validate_on_submit()
    ):
        ratings = request.files["file"].readlines()
        for rating in ratings:
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
        return redirect(url_for("main.reviews"))

    return render_template(
        "reviews.html",
        title="My Movie Reviews",
        review_export_form=review_export_form,
        review_upload_form=review_upload_form,
        reviews=reviews.items,
        next_url=next_url,
        prev_url=prev_url,
        pages=reviews,
        all_reviews=all_reviews,
    )


@bp.route("/admin", methods=["GET", "POST"])
@login_required
def admin():
    """User's adminstration page."""

    # Form to update the user's email address

    email_form = EditProfileForm(current_user.email)
    if email_form.submit.data and email_form.validate_on_submit():
        current_user.email = email_form.email.data
        db.session.commit()
        flash("Your email address has been changed.", "success")
        return redirect(url_for("main.admin"))

    # Form to generate a new API key

    api_refresh_form = UpdateAPIKeyForm()
    if (
        api_refresh_form.regenerate_key_submit.data
        and api_refresh_form.validate_on_submit()
    ):
        current_user.api_key = secrets.token_hex(16)
        db.session.commit()
        flash("Regenerated the API key.", "success")
        return redirect(url_for("main.admin"))

    # Form to update the Criterion Collection information for the entire movie library

    criterion_refresh_form = CriterionRefreshForm()
    if (
        criterion_refresh_form.criterion_refresh.data
        and criterion_refresh_form.validate_on_submit()
    ):
        current_app.sql_queue.enqueue(
            "app.videos.refresh_criterion_collection_info",
            args=None,
            job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
            description="Refreshing Criterion Collection information for all movies in library",
            at_front=True,
        )
        flash(
            f"Refreshing Criterion Collection information for all movies in library",
            "info",
        )
        return redirect(url_for("main.admin"))

    # Form to update the TMDb data for the entire library, both movies and TV shows

    tmdb_refresh_form = TMDBRefreshForm()
    if tmdb_refresh_form.tmdb_refresh.data and tmdb_refresh_form.validate_on_submit():
        movies = Movie.query.order_by(Movie.title.asc(), Movie.year.asc()).all()
        tv_shows = TVSeries.query.order_by(TVSeries.title.asc()).all()

        for movie in movies:
            refresh_job = current_app.sql_queue.enqueue(
                "app.videos.refresh_tmdb_info",
                args=("Movies", movie.id, movie.tmdb_id),
                job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
                description=f"Refreshing TMDB data for '{movie.title} ({movie.year})'",
            )

        for tv in tv_shows:
            refresh_job = current_app.sql_queue.enqueue(
                "app.videos.refresh_tmdb_info",
                args=("TV Shows", tv.id, tv.tmdb_id),
                job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
                description=f"Refreshing TMDB data for '{tv.title}'",
            )

        flash(f"Refreshing TMDb information for entire library", "info")
        return redirect(url_for("main.admin"))

    sync_form = SyncAWSStorageForm()
    if sync_form.sync_submit.data and sync_form.validate_on_submit():
        if not current_user.admin:
            flash(f"Need to be an admin user for this task!", "danger")

        elif current_user.check_password(sync_form.password.data):
            current_app.sql_queue.enqueue(
                "app.videos.sync_aws_s3_storage_task",
                args=None,
                job_timeout="24h",
                description=f"Syncing files from AWS S3 storage",
                at_front=True,
            )
            flash(f"Syncing files with AWS S3 storage", "info")

        else:
            flash(f"Incorrect password provided", "danger")

        return redirect(url_for("main.admin"))

    # Form to rescan metadata for all the files

    metadata_scan_form = TrackMetadataScanForm()

    if metadata_scan_form.scan_submit.data and metadata_scan_form.validate_on_submit():
        current_app.sql_queue.enqueue(
            "app.videos.track_metadata_scan_library",
            args=(),
            job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
            description=f"Scanning track metadata for all files in the library",
        )
        flash(f"Scanning track metadata for all files in the library", "info")
        return redirect(url_for("main.admin"))

    import_form = ImportForm()
    if import_form.submit.data and import_form.validate_on_submit():
        current_app.request_queue.enqueue(
            "app.videos.manual_import_task",
            args=(),
            job_timeout="1h",
            description="Manually scanning import directory for files",
            at_front=True,
        )
        flash(f"Added files in import directory to import queue", "info")
        return redirect(url_for("main.admin"))

    return render_template(
        "admin.html",
        title="Admin",
        email_form=email_form,
        api_refresh_form=api_refresh_form,
        criterion_refresh_form=criterion_refresh_form,
        tmdb_refresh_form=tmdb_refresh_form,
        sync_form=sync_form,
        metadata_scan_form=metadata_scan_form,
        import_form=import_form,
    )


@bp.route("/about")
def about():
    """Show general information about the Fitzflix application."""

    return render_template("about.html")


@bp.route("/shopping-list/movie", methods=["GET", "POST"])
@login_required
def movie_shopping():
    """Show instructions on how to improve the quality of each movie in the library.

    Possible user queries:
    - q          : filter the movie list for only the films that contain this substring
    - min_quality: show all movies where the best quality is at least this good
                   (defaults to "Unknown")
    - max_quality: show all movies where the best quality is *below* this threshold
                   (defaults to "Bluray-2160p Remux")
    """

    page = request.args.get("page", 1, type=int)
    q = request.args.get("q", None, type=str)
    library = request.args.get("library", None, type=str)
    media = request.args.get("media", None, type=str)
    min_quality = request.args.get("min_quality", 0, type=str)
    max_quality = request.args.get(
        "max_quality",
        db.session.query(RefQuality.id)
        .filter(RefQuality.quality_title == "Bluray-2160p Remux")
        .scalar(),
        type=str,
    )

    # Dynamically set the page title, so we can create different pages that all link to
    # the same shopping list, but using different filter presets

    title = request.args.get("title", "Movies to upgrade", type=str)

    # Form to filter the shopping list by Criterion release or quality

    filter_form = MovieShoppingFilterForm()
    if library == "criterion":
        criterion_release = True
        criterion_owned_false = False
        filter_form.filter_status.default = "criterion"

    else:
        criterion_release = None
        criterion_owned_false = None
        filter_form.filter_status.default = "all"

    if media == "digital":
        filter_form.media.default = "digital"

    else:
        filter_form.media.default = "all"

    # Create the list of qualities for the dropdown filter

    qualities = (
        db.session.query(RefQuality.id, RefQuality.quality_title)
        .order_by(RefQuality.preference.asc())
        .all()
    )
    filter_form.min_quality.choices = [(str(id), title) for (id, title) in qualities]
    filter_form.max_quality.choices = [(str(id), title) for (id, title) in qualities]

    # If the min_quality ID doesn't exist in our RefQuality table, default to "Unknown"

    if not RefQuality.query.filter_by(id=int(min_quality)).first():
        min_quality = int(
            db.session.query(RefQuality.id)
            .filter(RefQuality.quality_title == "Unknown")
            .scalar()
        )

    # If the max_quality ID doesn't exist in our RefQuality table, default to "Bluray-1080p"

    if not RefQuality.query.filter_by(id=int(max_quality)).first():
        max_quality = int(
            db.session.query(RefQuality.id)
            .filter(RefQuality.quality_title == "Bluray-1080p")
            .filter(RefQuality.physical_media == True)
            .scalar()
        )

    # If the minimum quality is greater than the maximum quality, set them to be the same

    if int(min_quality) > int(max_quality):
        max_quality = int(min_quality)

    # Find the preference associated with the quality ID, and set as the dropdown default

    min_preference = (
        db.session.query(RefQuality.preference).filter_by(id=int(min_quality)).scalar()
    )
    max_preference = (
        db.session.query(RefQuality.preference).filter_by(id=int(max_quality)).scalar()
    )
    filter_form.min_quality.default = min_quality
    filter_form.max_quality.default = max_quality

    # Form to filter the shopping list by a particular substring

    library_search_form = LibrarySearchForm()
    if filter_form.validate_on_submit():
        return redirect(
            url_for(
                "main.movie_shopping",
                title=title,
                library=filter_form.filter_status.data,
                media=filter_form.media.data,
                min_quality=filter_form.min_quality.data,
                max_quality=filter_form.max_quality.data,
                q=q,
            )
        )

    # Apply the changes to the filter form
    # (not sure why this has to go at this point in the code, but putting it elsewhere
    #  didn't work **shrug emoji**)

    filter_form.process()

    if (
        library_search_form.search_submit.data
        and library_search_form.validate_on_submit()
    ):
        return redirect(
            url_for(
                "main.movie_shopping",
                title=title,
                library=library,
                media=media,
                min_quality=min_quality,
                max_quality=max_quality,
                q=library_search_form.search_query.data,
            )
        )

    # Subquery to get the best movie titles

    ranked_files = (
        db.session.query(
            File.id.label("file_id"),
            Movie.id.label("movie_id"),
            Movie.title,
            File.version,
            RefQuality.quality_title,
            db.func.row_number()
            .over(
                partition_by=(Movie.id, File.plex_title, File.version),
                order_by=(File.fullscreen.asc(), RefQuality.preference.desc()),
            )
            .label("rank"),
        )
        .join(Movie, (Movie.id == File.movie_id))
        .join(RefQuality, (RefQuality.id == File.quality_id))
        .subquery()
    )

    # Subquery to get only physical-media movies

    physical_media = (
        db.session.query(Movie.id)
        .join(File, (File.movie_id == Movie.id))
        .join(RefQuality, (RefQuality.id == File.quality_id))
        .filter(RefQuality.physical_media == True)
        .filter(File.feature_type_id == None)
        .subquery()
    )

    file_count = (
        db.session.query(
            Movie.id,
            File.version,
            db.func.min(RefQuality.preference).label("min_preference"),
            db.func.count(File.id).label("file_count"),
        )
        .join(File, (File.movie_id == Movie.id))
        .join(RefQuality, (RefQuality.id == File.quality_id))
        .filter(File.feature_type_id == None)
        .group_by(Movie.id, File.version)
        .subquery()
    )

    # Subquery to get the current user's average ratings for each movie
    # The math on modified_rating, whole_stars, and half_stars is done when creating
    # the review, but we have to do it dynamically here because we need it to be
    # translated for drawing the *average* review stars on the shopping page.

    rating = (
        db.session.query(
            UserMovieReview.user_id,
            UserMovieReview.movie_id,
            db.func.avg(UserMovieReview.rating).label("rating"),
            (db.func.round(db.func.avg(UserMovieReview.rating) * 2) / 2).label(
                "modified_rating"
            ),
            db.func.floor(
                db.func.round(db.func.avg(UserMovieReview.rating) * 2) / 2
            ).label("whole_stars"),
            db.case(
                [
                    (
                        db.func.mod(
                            (
                                db.func.round(db.func.avg(UserMovieReview.rating) * 2)
                                / 2
                            ),
                            1,
                        )
                        == 0,
                        0,
                    ),
                ],
                else_=(1),
            ).label("half_stars"),
        )
        .group_by(UserMovieReview.user_id, UserMovieReview.movie_id)
        .subquery()
    )

    # Subqueries to get the preference associated with different quality thresholds

    dvd_quality = (
        db.session.query(RefQuality.preference)
        .filter(RefQuality.quality_title == "DVD")
        .scalar()
    )
    bluray_quality = (
        db.session.query(db.func.min(RefQuality.preference))
        .filter(RefQuality.quality_title.like("Bluray-1080%"))
        .filter(RefQuality.physical_media == True)
        .scalar()
    )

    if q:
        title = f"Movies to upgrade matching '{q}'"
        movies = (
            db.session.query(
                File,
                Movie,
                RefQuality,
                rating.c.rating,
                rating.c.modified_rating,
                rating.c.whole_stars,
                rating.c.half_stars,
                db.case(
                    [
                        (
                            db.and_(
                                RefQuality.preference == dvd_quality,
                                Movie.criterion_disc_owned == 1,
                                Movie.criterion_bluray == 0,
                            ),
                            "Already owned",
                        ),
                        (
                            db.and_(
                                RefQuality.preference <= bluray_quality,
                                Movie.criterion_disc_owned == 0,
                                Movie.criterion_in_print == 1,
                                Movie.criterion_bluray == 0,
                            ),
                            "Buy Criterion edition on DVD",
                        ),
                        (
                            db.and_(
                                RefQuality.preference <= bluray_quality,
                                Movie.criterion_disc_owned == 0,
                                Movie.criterion_in_print == 1,
                                Movie.criterion_bluray == 1,
                            ),
                            "Buy Criterion edition on Blu-Ray",
                        ),
                        (File.fullscreen == True, "Buy any non-fullscreen release"),
                        (RefQuality.preference < dvd_quality, "Buy on DVD or Blu-Ray"),
                        (RefQuality.preference < bluray_quality, "Buy on Blu-Ray"),
                    ],
                    else_=("Already owned"),
                ).label("instruction"),
            )
            .join(Movie, (Movie.id == File.movie_id))
            .join(RefQuality, (RefQuality.id == File.quality_id))
            .outerjoin(
                rating,
                (rating.c.movie_id == Movie.id) & (rating.c.user_id == current_user.id),
            )
            .join(ranked_files, (ranked_files.c.file_id == File.id))
            .filter(File.feature_type_id == None)
            .filter(ranked_files.c.rank == 1)
            .filter(RefQuality.preference >= min_preference)
            .filter(RefQuality.preference <= max_preference)
            .filter(
                db.or_(Movie.title.ilike(f"%{q}%"), Movie.tmdb_title.ilike(f"%{q}%"))
            )
            .filter(
                db.or_(
                    Movie.shopping_list_exclude == None,
                    Movie.shopping_list_exclude == False,
                )
            )
            .order_by(
                db.func.regexp_replace(Movie.title, "^(The|A|An)\s", "").asc(),
                Movie.year.asc(),
                File.version.asc(),
                RefQuality.preference.asc(),
                File.date_added.asc(),
            )
            .paginate(page, 100, False)
        )

    elif media == "digital":
        physical_media = (
            db.session.query(
                File.movie_id,
            )
            .join(Movie, (Movie.id == File.movie_id))
            .join(RefQuality, (RefQuality.id == File.quality_id))
            .filter(
                db.or_(
                    RefQuality.physical_media == True,
                    RefQuality.quality_title == "SDTV",
                    RefQuality.quality_title.ilike("HDTV-%"),
                )
            )
            .subquery()
        )

        movies = (
            db.session.query(
                File,
                Movie,
                RefQuality,
                rating.c.rating,
                rating.c.modified_rating,
                rating.c.whole_stars,
                rating.c.half_stars,
                db.case(
                    [
                        (
                            db.and_(
                                RefQuality.preference == dvd_quality,
                                Movie.criterion_disc_owned == 1,
                                Movie.criterion_bluray == 0,
                            ),
                            "Already owned",
                        ),
                        (
                            db.and_(
                                RefQuality.preference <= bluray_quality,
                                Movie.criterion_disc_owned == 0,
                                Movie.criterion_in_print == 1,
                                Movie.criterion_bluray == 0,
                            ),
                            "Buy Criterion edition on DVD",
                        ),
                        (
                            db.and_(
                                RefQuality.preference <= bluray_quality,
                                Movie.criterion_disc_owned == 0,
                                Movie.criterion_in_print == 1,
                                Movie.criterion_bluray == 1,
                            ),
                            "Buy Criterion edition on Blu-Ray",
                        ),
                        (File.fullscreen == True, "Buy any non-fullscreen release"),
                        (RefQuality.preference < dvd_quality, "Buy on DVD or Blu-Ray"),
                        (RefQuality.preference < bluray_quality, "Buy on Blu-Ray"),
                    ],
                    else_=("Already owned"),
                ).label("instruction"),
            )
            .join(Movie, (Movie.id == File.movie_id))
            .join(RefQuality, (RefQuality.id == File.quality_id))
            .outerjoin(
                rating,
                (rating.c.movie_id == Movie.id) & (rating.c.user_id == current_user.id),
            )
            .join(ranked_files, (ranked_files.c.file_id == File.id))
            .filter(File.feature_type_id == None)
            .filter(ranked_files.c.rank == 1)
            .filter(RefQuality.preference >= min_preference)
            .filter(RefQuality.preference <= max_preference)
            .filter(Movie.id.not_in(physical_media))
            .filter(
                db.or_(
                    db.and_(
                        criterion_release == True,
                        Movie.criterion_spine_number != None,
                        Movie.criterion_in_print == 1,
                    ),
                    criterion_release != True,
                ),
            )
            .filter(
                db.or_(
                    Movie.shopping_list_exclude == None,
                    Movie.shopping_list_exclude == False,
                )
            )
            .order_by(
                db.case(
                    [
                        (
                            db.and_(
                                RefQuality.preference == dvd_quality,
                                Movie.criterion_disc_owned == 1,
                                Movie.criterion_bluray == 0,
                            ),
                            1,
                        ),
                        (
                            db.and_(
                                RefQuality.preference <= bluray_quality,
                                Movie.criterion_disc_owned == 0,
                                Movie.criterion_in_print == 1,
                                Movie.criterion_bluray == 0,
                            ),
                            0,
                        ),
                        (
                            db.and_(
                                RefQuality.preference <= bluray_quality,
                                Movie.criterion_disc_owned == 0,
                                Movie.criterion_in_print == 1,
                                Movie.criterion_bluray == 1,
                            ),
                            0,
                        ),
                        (File.fullscreen == True, 0),
                        (RefQuality.preference < dvd_quality, 0),
                        (RefQuality.preference < bluray_quality, 0),
                    ],
                    else_=1,
                ).asc(),
                db.case([(File.fullscreen == True, 0)], else_=1).asc(),
                db.case(
                    [(rating.c.whole_stars >= 3, rating.c.rating)],
                    else_=0,
                ).desc(),
                db.func.regexp_replace(Movie.title, "^(The|A|An)\s", "").asc(),
                Movie.year.asc(),
                File.version.asc(),
                File.date_added.asc(),
            )
            .paginate(page, 100, False)
        )

    else:
        movies = (
            db.session.query(
                File,
                Movie,
                RefQuality,
                rating.c.rating,
                rating.c.modified_rating,
                rating.c.whole_stars,
                rating.c.half_stars,
                db.case(
                    [
                        (
                            db.and_(
                                RefQuality.preference == dvd_quality,
                                Movie.criterion_disc_owned == 1,
                                Movie.criterion_bluray == 0,
                            ),
                            "Already owned",
                        ),
                        (
                            db.and_(
                                RefQuality.preference <= bluray_quality,
                                Movie.criterion_disc_owned == 0,
                                Movie.criterion_in_print == 1,
                                Movie.criterion_bluray == 0,
                            ),
                            "Buy Criterion edition on DVD",
                        ),
                        (
                            db.and_(
                                RefQuality.preference <= bluray_quality,
                                Movie.criterion_disc_owned == 0,
                                Movie.criterion_in_print == 1,
                                Movie.criterion_bluray == 1,
                            ),
                            "Buy Criterion edition on Blu-Ray",
                        ),
                        (File.fullscreen == True, "Buy any non-fullscreen release"),
                        (RefQuality.preference < dvd_quality, "Buy on DVD or Blu-Ray"),
                        (RefQuality.preference < bluray_quality, "Buy on Blu-Ray"),
                    ],
                    else_=("Already owned"),
                ).label("instruction"),
            )
            .join(Movie, (Movie.id == File.movie_id))
            .join(RefQuality, (RefQuality.id == File.quality_id))
            .outerjoin(
                rating,
                (rating.c.movie_id == Movie.id) & (rating.c.user_id == current_user.id),
            )
            .join(ranked_files, (ranked_files.c.file_id == File.id))
            .filter(File.feature_type_id == None)
            .filter(ranked_files.c.rank == 1)
            .filter(RefQuality.preference >= min_preference)
            .filter(RefQuality.preference <= max_preference)
            .filter(
                db.or_(
                    db.and_(
                        criterion_release == True,
                        Movie.criterion_spine_number != None,
                        Movie.criterion_in_print == 1,
                    ),
                    criterion_release != True,
                ),
            )
            .filter(
                db.or_(
                    Movie.shopping_list_exclude == None,
                    Movie.shopping_list_exclude == False,
                )
            )
            .order_by(
                db.case(
                    [
                        (
                            db.and_(
                                RefQuality.preference == dvd_quality,
                                Movie.criterion_disc_owned == 1,
                                Movie.criterion_bluray == 0,
                            ),
                            1,
                        ),
                        (
                            db.and_(
                                RefQuality.preference <= bluray_quality,
                                Movie.criterion_disc_owned == 0,
                                Movie.criterion_in_print == 1,
                                Movie.criterion_bluray == 0,
                            ),
                            0,
                        ),
                        (
                            db.and_(
                                RefQuality.preference <= bluray_quality,
                                Movie.criterion_disc_owned == 0,
                                Movie.criterion_in_print == 1,
                                Movie.criterion_bluray == 1,
                            ),
                            0,
                        ),
                        (File.fullscreen == True, 0),
                        (RefQuality.preference < dvd_quality, 0),
                        (RefQuality.preference < bluray_quality, 0),
                    ],
                    else_=1,
                ).asc(),
                db.case([(File.fullscreen == True, 0)], else_=1).asc(),
                db.case(
                    [(rating.c.whole_stars >= 3, rating.c.rating)],
                    else_=0,
                ).desc(),
                db.case(
                    [
                        (
                            (RefQuality.physical_media == 0)
                            & (RefQuality.quality_title != "SDTV")
                            & (RefQuality.quality_title.notlike("HDTV-%")),
                            0,
                        ),
                    ],
                    else_=1,
                ).asc(),
                db.func.regexp_replace(Movie.title, "^(The|A|An)\s", "").asc(),
                Movie.year.asc(),
                File.version.asc(),
                File.date_added.asc(),
            )
            .paginate(page, 100, False)
        )

    movie_shopping_exclude_form = MovieShoppingExcludeForm()

    next_url = (
        url_for(
            "main.movie_shopping",
            page=movies.next_num,
            title=title,
            q=q,
            media=media,
            library=library,
            min_quality=min_quality,
            max_quality=max_quality,
            movie_shopping_exclude_form=movie_shopping_exclude_form,
        )
        if movies.has_next
        else None
    )
    prev_url = (
        url_for(
            "main.movie_shopping",
            page=movies.prev_num,
            title=title,
            q=q,
            media=media,
            library=library,
            min_quality=min_quality,
            max_quality=max_quality,
            movie_shopping_exclude_form=movie_shopping_exclude_form,
        )
        if movies.has_prev
        else None
    )

    if (
        movie_shopping_exclude_form.add_submit.data
        and movie_shopping_exclude_form.validate_on_submit()
    ):
        movie = Movie.query.filter_by(
            id=int(movie_shopping_exclude_form.movie_id.data)
        ).first()
        movie.shopping_list_exclude = 1
        db.session.commit()
        flash(f"Added '{movie.title}' to the shopping list")
        return redirect(
            url_for(
                "main.movie_shopping",
                page=page,
                q=q,
                library=library,
                media=media,
                min_quality=min_quality,
                max_quality=max_quality,
            ),
        )

    elif (
        movie_shopping_exclude_form.exclude_submit.data
        and movie_shopping_exclude_form.validate_on_submit()
    ):
        movie = Movie.query.filter_by(
            id=int(movie_shopping_exclude_form.movie_id.data)
        ).first()
        movie.shopping_list_exclude = 1
        db.session.commit()
        flash(f"Removed '{movie.title}' from the shopping list")
        return redirect(
            url_for(
                "main.movie_shopping",
                page=page,
                q=q,
                library=library,
                media=media,
                min_quality=min_quality,
                max_quality=max_quality,
            ),
        )

    return render_template(
        "shopping_movie.html",
        title=title,
        movies=movies.items,
        next_url=next_url,
        prev_url=prev_url,
        pages=movies,
        filter_form=filter_form,
        library_search_form=library_search_form,
        radarr_proxy_url=current_app.config["RADARR_PROXY_URL"],
        movie_shopping_exclude_form=movie_shopping_exclude_form,
    )


@bp.route("/shopping-list/tv", methods=["GET", "POST"])
@login_required
def tv_shopping():
    """Show instructions on how to improve the quality of each TV show season.

    Possible user queries:
    - q          : filter the list for only the tv series that contain this substring
    - min_quality: show all seasons where the worst quality is at least this good
                   (defaults to "Unknown")
    - max_quality: show all seasons where the worst quality is *below* this threshold
                   (defaults to "Bluray-1080p")
    """

    q = request.args.get("q", None, type=str)
    min_quality = request.args.get("min_quality", 0, type=str)
    max_quality = request.args.get(
        "max_quality",
        db.session.query(RefQuality.id)
        .filter(RefQuality.quality_title == "Bluray-2160p Remux")
        .scalar(),
        type=str,
    )

    # Form to filter the shopping list by quality

    filter_form = TVShoppingFilterForm()

    # Create the list of qualities for the dropdown filter

    qualities = (
        db.session.query(RefQuality.id, RefQuality.quality_title)
        .order_by(RefQuality.preference.asc())
        .all()
    )
    filter_form.quality.choices = [(str(id), title) for (id, title) in qualities]

    # If the min_quality ID doesn't exist in our RefQuality table, default to "Unknown"

    if not RefQuality.query.filter_by(id=int(min_quality)).first():
        min_quality = int(
            db.session.query(RefQuality.id)
            .filter(RefQuality.quality_title == "Unknown")
            .scalar()
        )

    # If the max_quality ID doesn't exist in our RefQuality table, default to "Bluray-1080p"

    if not RefQuality.query.filter_by(id=int(max_quality)).first():
        max_quality = int(
            db.session.query(RefQuality.id)
            .filter(RefQuality.quality_title == "Bluray-1080p")
            .filter(RefQuality.physical_media == True)
            .scalar()
        )

    # If the minimum quality is greater than the maximum quality, set them to be the same

    if int(min_quality) > int(max_quality):
        min_quality = int(max_quality)

    # Find the preference associated with the quality ID, and set as the dropdown default

    min_preference = (
        db.session.query(RefQuality.preference).filter_by(id=int(min_quality)).scalar()
    )
    max_preference = (
        db.session.query(RefQuality.preference).filter_by(id=int(max_quality)).scalar()
    )
    filter_form.quality.default = max_quality

    # Form to filter the shopping list by a particular substring

    library_search_form = LibrarySearchForm()
    if filter_form.validate_on_submit():
        return redirect(
            url_for("main.tv_shopping", max_quality=filter_form.quality.data, q=q)
        )

    # Apply the changes to the filter form
    # (not sure why this has to go at this point in the code, but putting it elsewhere
    #  didn't work **shrug emoji**)

    filter_form.process()

    if (
        library_search_form.search_submit.data
        and library_search_form.validate_on_submit()
    ):
        return redirect(
            url_for(
                "main.tv_shopping",
                max_quality=max_quality,
                q=library_search_form.search_query.data,
            )
        )

    # Subqueries to get the preference associated with different quality thresholds

    dvd_quality = (
        db.session.query(RefQuality.preference)
        .filter(RefQuality.quality_title == "DVD")
        .scalar()
    )
    bluray_quality = (
        db.session.query(db.func.min(RefQuality.preference))
        .filter(RefQuality.quality_title.like("Bluray-1080%"))
        .filter(RefQuality.physical_media == True)
        .scalar()
    )

    # Subquery to get the worst quality for each tv show season

    subquery = (
        db.session.query(
            File.series_id,
            File.season,
            db.func.count(db.func.distinct(File.episode)).label("episodes"),
            db.func.min(RefQuality.preference).label("preference"),
        )
        .group_by(File.series_id, File.season)
        .join(RefQuality, (RefQuality.id == File.quality_id))
        .subquery()
    )

    tv = []
    if q:
        title = f"TV Shows to upgrade matching '{q}'"
        q = q.replace(" ", "%")
        t = (
            TVSeries.query.filter(
                db.or_(
                    TVSeries.title.ilike(f"%{q}%"), TVSeries.tmdb_name.ilike(f"%{q}%")
                )
            )
            .order_by(db.func.regexp_replace(TVSeries.title, "^(The|A|An)\s", "").asc())
            .all()
        )

    else:
        t = TVSeries.query.order_by(
            db.func.regexp_replace(TVSeries.title, "^(The|A|An)\s", "").asc()
        ).all()
        title = f"TV Shows to upgrade"

    for series in t:
        seasons = []
        s = (
            db.session.query(
                subquery.c.season,
                subquery.c.episodes,
                RefQuality.quality_title,
                db.case(
                    [
                        (RefQuality.preference < dvd_quality, "Buy on DVD or Blu-Ray"),
                        (RefQuality.preference < bluray_quality, "Buy on Blu-Ray"),
                    ],
                    else_="Already owned",
                ).label("instruction"),
            )
            .join(RefQuality, (RefQuality.preference == subquery.c.preference))
            .filter(subquery.c.series_id == series.id)
            .filter(RefQuality.preference >= min_preference)
            .filter(RefQuality.preference <= max_preference)
            .order_by(
                db.case([(subquery.c.season == 0, 1)], else_=0).asc(),
                subquery.c.season.asc(),
            )
            .all()
        )

        for season, num_episodes, min_quality, instruction in s:
            seasons.append(
                {
                    "season": season,
                    "episode_count": num_episodes,
                    "min_quality": min_quality,
                    "instruction": instruction,
                }
            )

        # Don't show any tv series where there aren't any seasons
        # (Needed because of the quality filter, otherwise we may show a tv series that
        #  doesn't have any seasons that reach the quality filter threshold.)

        if len(seasons) == 0:
            continue

        tv.append(
            {
                "id": series.id,
                "title": series.title,
                "tmdb_id": series.tmdb_id,
                "tmdb_name": series.tmdb_name,
                "tmdb_poster_path": series.tmdb_poster_path,
                "seasons": seasons,
            }
        )

    return render_template(
        "shopping_tv.html",
        title=title,
        filter_form=filter_form,
        library_search_form=library_search_form,
        series=tv,
    )


@bp.route("/queue", methods=["GET", "POST"])
@login_required
def queue():
    """Show a list of all localization and transcode tasks in queue.

    See api.queue_details for how the queue is generated.
    """

    import_form = ImportForm()
    if import_form.submit.data and import_form.validate_on_submit():
        current_app.request_queue.enqueue(
            "app.videos.manual_import_task",
            args=(),
            job_timeout="1h",
            description="Manually scanning import directory for files",
            at_front=True,
        )
        flash(f"Added files in import directory to import queue", "info")
        return redirect(url_for("main.queue"))

    return render_template(
        "queue.html",
        title="Queue",
        import_form=import_form,
    )


@bp.route("/library/files", methods=["GET", "POST"])
@login_required
def files():
    """Show a list of all the files in the library."""

    page = request.args.get("page", 1, type=int)
    q = request.args.get("q", None, type=str)
    quality = request.args.get("quality", "0", type=str)
    audio = request.args.get("audio", None, type=str)

    movie_rank = (
        db.session.query(
            File.id,
            db.func.row_number()
            .over(
                partition_by=(Movie.id, File.plex_title, File.version),
                order_by=(File.fullscreen.asc(), RefQuality.preference.desc()),
            )
            .label("rank"),
        )
        .join(Movie, (Movie.id == File.movie_id))
        .join(RefQuality, (RefQuality.id == File.quality_id))
        .subquery()
    )

    tv_rank = (
        db.session.query(
            File.id,
            db.func.row_number()
            .over(
                partition_by=(TVSeries.id, File.season, File.episode, File.version),
                order_by=(File.fullscreen.asc(), RefQuality.preference.desc()),
            )
            .label("rank"),
        )
        .join(TVSeries, (TVSeries.id == File.series_id))
        .join(RefQuality, (RefQuality.id == File.quality_id))
        .subquery()
    )

    files_with_lossless = (
        db.session.query(
            FileAudioTrack.file_id
        )
        .filter(FileAudioTrack.compression_mode == "Lossless")
        .subquery()
    )

    lossy_files = (
        db.session.query(
            FileAudioTrack.file_id
        )
        .filter(FileAudioTrack.track == 1)
        .filter(FileAudioTrack.compression_mode != "Lossless")
        .subquery()
    )

    if q and int(quality) > 0:
        this_quality = RefQuality.query.filter_by(id=int(quality)).first_or_404()
        title = f"{this_quality.quality_title} files matching '{q}'"
        q = q.replace(" ", "%")
        files = (
            db.session.query(
                File,
                RefQuality,
                RefFeatureType,
                Movie,
                TVSeries,
                db.case(
                    [(movie_rank.c.rank == 1, 1), (tv_rank.c.rank == 1, 1)], else_=0
                ).label("rank"),
            )
            .join(RefQuality, (RefQuality.id == File.quality_id))
            .outerjoin(RefFeatureType, (RefFeatureType.id == File.feature_type_id))
            .outerjoin(Movie, (Movie.id == File.movie_id))
            .outerjoin(TVSeries, (TVSeries.id == File.series_id))
            .outerjoin(movie_rank, (movie_rank.c.id == File.id))
            .outerjoin(tv_rank, (tv_rank.c.id == File.id))
            .filter(File.basename.ilike(f"%{q}%"))
            .filter(RefQuality.id == int(quality))
            .order_by(
                File.media_library,
                db.func.regexp_replace(
                    db.case(
                        [(Movie.tmdb_title != None, Movie.tmdb_title)],
                        else_=Movie.title,
                    ),
                    "^(The|A|An)\s",
                    "",
                ).asc(),
                db.case(
                    [(Movie.tmdb_title != None, Movie.tmdb_release_date)],
                    else_=Movie.year,
                ).asc(),
                File.version.asc(),
                RefFeatureType.feature_type.asc(),
                db.func.regexp_replace(
                    db.case(
                        [(TVSeries.tmdb_name != None, TVSeries.tmdb_name)],
                        else_=TVSeries.title,
                    ),
                    "^(The|A|An)\s",
                    "",
                ).asc(),
                File.season.asc(),
                File.episode.asc(),
                File.last_episode.asc(),
                RefQuality.preference.asc(),
                File.basename.asc(),
            )
            .paginate(page, 1000, False)
        )

    elif q:
        title = f"Files matching '{q}'"
        q = q.replace(" ", "%")
        files = (
            db.session.query(
                File,
                RefQuality,
                RefFeatureType,
                Movie,
                TVSeries,
                db.case(
                    [(movie_rank.c.rank == 1, 1), (tv_rank.c.rank == 1, 1)], else_=0
                ).label("rank"),
            )
            .join(RefQuality, (RefQuality.id == File.quality_id))
            .outerjoin(RefFeatureType, (RefFeatureType.id == File.feature_type_id))
            .outerjoin(Movie, (Movie.id == File.movie_id))
            .outerjoin(TVSeries, (TVSeries.id == File.series_id))
            .outerjoin(movie_rank, (movie_rank.c.id == File.id))
            .outerjoin(tv_rank, (tv_rank.c.id == File.id))
            .filter(File.basename.ilike(f"%{q}%"))
            .order_by(
                File.media_library,
                db.func.regexp_replace(
                    db.case(
                        [(Movie.tmdb_title != None, Movie.tmdb_title)],
                        else_=Movie.title,
                    ),
                    "^(The|A|An)\s",
                    "",
                ).asc(),
                db.case(
                    [(Movie.tmdb_title != None, Movie.tmdb_release_date)],
                    else_=Movie.year,
                ).asc(),
                File.version.asc(),
                RefFeatureType.feature_type.asc(),
                db.func.regexp_replace(
                    db.case(
                        [(TVSeries.tmdb_name != None, TVSeries.tmdb_name)],
                        else_=TVSeries.title,
                    ),
                    "^(The|A|An)\s",
                    "",
                ).asc(),
                File.season.asc(),
                File.episode.asc(),
                File.last_episode.asc(),
                RefQuality.preference.asc(),
                File.basename.asc(),
            )
            .paginate(page, 1000, False)
        )

    elif int(quality) > 0:
        this_quality = RefQuality.query.filter_by(id=int(quality)).first_or_404()
        title = f"{this_quality.quality_title} files"
        files = (
            db.session.query(
                File,
                RefQuality,
                RefFeatureType,
                Movie,
                TVSeries,
                db.case(
                    [(movie_rank.c.rank == 1, 1), (tv_rank.c.rank == 1, 1)], else_=0
                ).label("rank"),
            )
            .join(RefQuality, (RefQuality.id == File.quality_id))
            .outerjoin(RefFeatureType, (RefFeatureType.id == File.feature_type_id))
            .outerjoin(Movie, (Movie.id == File.movie_id))
            .outerjoin(TVSeries, (TVSeries.id == File.series_id))
            .outerjoin(movie_rank, (movie_rank.c.id == File.id))
            .outerjoin(tv_rank, (tv_rank.c.id == File.id))
            .filter(RefQuality.id == int(quality))
            .order_by(
                File.media_library,
                db.func.regexp_replace(
                    db.case(
                        [(Movie.tmdb_title != None, Movie.tmdb_title)],
                        else_=Movie.title,
                    ),
                    "^(The|A|An)\s",
                    "",
                ).asc(),
                db.case(
                    [(Movie.tmdb_title != None, Movie.tmdb_release_date)],
                    else_=Movie.year,
                ).asc(),
                File.version.asc(),
                RefFeatureType.feature_type.asc(),
                db.func.regexp_replace(
                    db.case(
                        [(TVSeries.tmdb_name != None, TVSeries.tmdb_name)],
                        else_=TVSeries.title,
                    ),
                    "^(The|A|An)\s",
                    "",
                ).asc(),
                File.season.asc(),
                File.episode.asc(),
                File.last_episode.asc(),
                RefQuality.preference.asc(),
                File.basename.asc(),
            )
            .paginate(page, 1000, False)
        )

    elif audio == "lossy":
        title = "Files that have lossy first audio tracks"
        files = (
            db.session.query(
                File,
                RefQuality,
                RefFeatureType,
                Movie,
                TVSeries,
                db.case(
                    [(movie_rank.c.rank == 1, 1), (tv_rank.c.rank == 1, 1)], else_=0
                ).label("rank"),
            )
            .join(RefQuality, (RefQuality.id == File.quality_id))
            .outerjoin(RefFeatureType, (RefFeatureType.id == File.feature_type_id))
            .outerjoin(Movie, (Movie.id == File.movie_id))
            .outerjoin(TVSeries, (TVSeries.id == File.series_id))
            .outerjoin(movie_rank, (movie_rank.c.id == File.id))
            .outerjoin(tv_rank, (tv_rank.c.id == File.id))
            .filter(File.id.in_(files_with_lossless))
            .filter(File.id.in_(lossy_files))
            .order_by(
                File.media_library,
                db.func.regexp_replace(
                    db.case(
                        [(Movie.tmdb_title != None, Movie.tmdb_title)],
                        else_=Movie.title,
                    ),
                    "^(The|A|An)\s",
                    "",
                ).asc(),
                db.case(
                    [(Movie.tmdb_title != None, Movie.tmdb_release_date)],
                    else_=Movie.year,
                ).asc(),
                File.version.asc(),
                RefFeatureType.feature_type.asc(),
                db.func.regexp_replace(
                    db.case(
                        [(TVSeries.tmdb_name != None, TVSeries.tmdb_name)],
                        else_=TVSeries.title,
                    ),
                    "^(The|A|An)\s",
                    "",
                ).asc(),
                File.season.asc(),
                File.episode.asc(),
                File.last_episode.asc(),
                RefQuality.preference.asc(),
                File.basename.asc(),
            )
            .paginate(page, 1000, False)
        )

    else:
        title = "All Files"
        files = (
            db.session.query(
                File,
                RefQuality,
                RefFeatureType,
                Movie,
                TVSeries,
                db.case(
                    [(movie_rank.c.rank == 1, 1), (tv_rank.c.rank == 1, 1)], else_=0
                ).label("rank"),
            )
            .join(RefQuality, (RefQuality.id == File.quality_id))
            .outerjoin(RefFeatureType, (RefFeatureType.id == File.feature_type_id))
            .outerjoin(Movie, (Movie.id == File.movie_id))
            .outerjoin(TVSeries, (TVSeries.id == File.series_id))
            .outerjoin(movie_rank, (movie_rank.c.id == File.id))
            .outerjoin(tv_rank, (tv_rank.c.id == File.id))
            .order_by(
                File.media_library,
                db.func.regexp_replace(
                    db.case(
                        [(Movie.tmdb_title != None, Movie.tmdb_title)],
                        else_=Movie.title,
                    ),
                    "^(The|A|An)\s",
                    "",
                ).asc(),
                db.case(
                    [(Movie.tmdb_title != None, Movie.tmdb_release_date)],
                    else_=Movie.year,
                ).asc(),
                File.version.asc(),
                RefFeatureType.feature_type.asc(),
                db.func.regexp_replace(
                    db.case(
                        [(TVSeries.tmdb_name != None, TVSeries.tmdb_name)],
                        else_=TVSeries.title,
                    ),
                    "^(The|A|An)\s",
                    "",
                ).asc(),
                File.season.asc(),
                File.episode.asc(),
                File.last_episode.asc(),
                RefQuality.preference.asc(),
                File.basename.asc(),
            )
            .paginate(page, 1000, False)
        )

    next_url = (
        url_for("main.files", page=files.next_num, quality=quality)
        if files.has_next
        else None
    )
    prev_url = (
        url_for("main.files", page=files.prev_num, quality=quality)
        if files.has_prev
        else None
    )

    filter_form = QualityFilterForm()

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
        return redirect(url_for("main.files", q=q, quality=filter_form.quality.data))

    filter_form.process()

    library_search_form = LibrarySearchForm()
    if library_search_form.validate_on_submit():
        return redirect(
            url_for(
                "main.files",
                q=library_search_form.search_query.data,
                quality=filter_form.quality.data,
            )
        )

    return render_template(
        "files.html",
        title=title,
        files=files.items,
        next_url=next_url,
        prev_url=prev_url,
        pages=files,
        filter_form=filter_form,
        library_search_form=library_search_form,
    )
