import csv
import io
import json
import os
import re
import secrets
import shutil
import time
import traceback

from types import SimpleNamespace

from datetime import date, datetime, timezone
from PIL import Image

from flask import (
    abort,
    current_app,
    jsonify,
    make_response,
    render_template,
    flash,
    redirect,
    url_for,
    request,
    send_from_directory,
)

import requests

# flask.Markup was removed in Flask 2.4; import from its actual home
from markupsafe import Markup
from flask_login import current_user, login_required
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

from app import db, enqueue_import_scan, safe_job_id
from app.main.forms import (
    CriterionFilterForm,
    CriterionForm,
    CriterionRefreshForm,
    CustomPosterRemoveForm,
    CustomPosterUploadForm,
    EditProfileForm,
    FileDeleteForm,
    FailedJobForm,
    FilenameTestForm,
    SubtitleTriageForm,
    ImportForm,
    LibrarySearchForm,
    MKVMergeForm,
    MKVPropEditForm,
    MovieReviewForm,
    MovieMergeForm,
    MovieShoppingExcludeForm,
    MovieShoppingFilterForm,
    RejectActionForm,
    SyncAWSStorageForm,
    QualityFilterForm,
    ReviewExportForm,
    ReviewUploadForm,
    S3DownloadForm,
    SeasonRestoreForm,
    SeriesRestoreForm,
    S3UploadForm,
    SeriesDeleteForm,
    TMDBLookupForm,
    TMDBPosterSelectForm,
    TMDBRefreshForm,
    TrackMetadataScanForm,
    TranscodeForm,
    TVShoppingFilterForm,
    PlexUsernameForm,
    StreamingProvidersForm,
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
    UserStreamingProvider,
    movie_file_rank,
    tmdb_get,
    tv_file_rank,
)
from app.main import bp
from app.email import send_email
from app.maintenance import system_health
from app.recommendations import (
    credit_interest_markers,
    stored_profile,
    stored_recommendations,
)
from app.streaming import (
    batch_title_availability,
    provider_registry,
    rental_matches,
    streaming_matches,
    title_availability,
    user_provider_ids,
    user_streaming,
)
from app.leaving_criterion import leaving_shelf
from app.streaming_rail import stored_rail
from app.videos import (
    evaluate_filename,
    parse_letterboxd_export,
    star_rating_fields,
    track_metadata_scan,
)
from rq.exceptions import NoSuchJobError
from rq.job import Job
from rq.registry import FailedJobRegistry, StartedJobRegistry

from functools import wraps


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


def save_custom_poster(uploaded_data, poster_filename, custom_poster_dir):
    """Validate an uploaded poster, then write the original and its thumbnails.

    Returns the path of the saved original; raises ValueError with a
    flash-ready message when the upload isn't a usable poster image.
    """

    try:
        Image.open(uploaded_data).verify()
    except Exception:
        raise ValueError(f"'{uploaded_data.filename}' is corrupted!")

    with Image.open(uploaded_data) as poster:
        current_app.logger.info(f"Uploaded poster format: {poster.format}")
        if poster.format not in ["JPEG", "PNG"]:
            raise ValueError(f"'{poster.format}' is not an appropriate file type!")

        os.makedirs(os.path.join(custom_poster_dir, "original"), exist_ok=True)
        original_file = os.path.join(custom_poster_dir, "original", poster_filename)
        poster.save(original_file)

        original_width, original_height = poster.size

        for width in ["92", "154", "185", "342", "500", "780"]:
            current_app.logger.info(f"'{original_file}' Creating w{width} thumbnail")

            percent = int(width) / float(original_width)
            height = int(original_height * float(percent))
            size = (int(width), int(height))

            subdir_path = os.path.join(custom_poster_dir, f"w{width}")
            os.makedirs(subdir_path, exist_ok=True)

            poster_thumbnail = poster.copy()
            poster_thumbnail.thumbnail(size)
            if poster.format == "JPEG":
                poster_thumbnail.save(
                    os.path.join(subdir_path, poster_filename), quality=95
                )
            else:
                poster_thumbnail.save(os.path.join(subdir_path, poster_filename))

    return original_file


def replace_library_poster(library_directory, original_file, poster_filename):
    """Remove existing poster art from a library directory, then copy in the new one."""

    for name in os.listdir(library_directory):
        if name.lower().startswith(
            ("cover", "default", "movie", "poster")
        ) and name.lower().endswith(("jpg", "jpeg", "png", "tbn")):
            current_app.logger.info(f"Deleting {os.path.join(library_directory, name)}")
            os.remove(os.path.join(library_directory, name))

    destination_file = os.path.join(library_directory, poster_filename)
    shutil.copy(original_file, destination_file)
    current_app.logger.info(f"'{original_file}' Copied to '{destination_file}'")


@bp.route("/apple-touch-icon-precomposed.png")
@bp.route("/apple-touch-icon.png")
def androidPng():
    """Serve the touch icon at the fixed paths Apple devices request."""

    return send_from_directory(
        os.path.join(current_app.root_path, "static"),
        "apple-touch-icon.png",
        mimetype="image/png",
    )


@bp.route("/favicon.ico")
def favicon():
    """Serve the classic favicon at its fixed root path."""

    return send_from_directory(
        os.path.join(current_app.root_path, "static"),
        "favicon.ico",
        mimetype="image/vnd.microsoft.icon",
    )


@bp.route("/sw.js")
def service_worker():
    # Served from the root (rather than /static/) so the service worker's
    # scope covers the whole application

    """Serve the PWA service worker from the site root, so its scope
    covers the whole application.
    """

    return send_from_directory(
        os.path.join(current_app.root_path, "static"),
        "sw.js",
        mimetype="application/javascript",
    )


@bp.route("/")
@bp.route("/index")
@login_required
def index():
    """The landing page: what to watch tonight, recommended from the
    library by the user's own diary (GitHub #46/#61).

    ?minutes=N filters both rails at view time to films that fit the
    evening — the computed recommendations themselves never consider
    length, and films with unknown runtimes hide only from filtered
    views."""

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
        movie_ids = [item["movie_id"] for item in stored.get("items", [])]
        movies = {
            movie.id: movie
            for movie in Movie.query.filter(Movie.id.in_(movie_ids or [0]))
        }
        for item in stored.get("items", []):
            movie = movies.get(item["movie_id"])
            if movie is None or item["movie_id"] in seen:
                continue
            if minutes and not (movie.tmdb_runtime and movie.tmdb_runtime <= minutes):
                continue
            recs.append({"movie": movie, "because": item.get("because", [])[:3]})
            if len(recs) == 18:
                break
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

    # The second rail: films streaming on this user's services, from the
    # nightly discover-pool recompute. Films logged or acquired since
    # the run drop out immediately; a user with a profile and provider
    # picks but no stored rail gets a one-off compute enqueued

    rail = []
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
            dropped = {t for (t,) in owned_now} | {t for (t,) in logged_now}
        for item in rail_payload.get("items", []):
            if item["tmdb_id"] in dropped:
                continue
            if minutes and not (item.get("runtime") and item["runtime"] <= minutes):
                continue
            rail.append(item)
            if len(rail) == 12:
                break
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
    shelf_departs = None
    if shelf:
        shelf_departs = shelf["departs"].strftime("%B %-d")
        for item in shelf["items"]:
            if minutes and not (item.get("runtime") and item["runtime"] <= minutes):
                continue
            shelf_items.append(item)
            if len(shelf_items) == 12:
                break

    return render_template(
        "index.html",
        title="Home",
        recs=recs,
        computed_at=computed_at,
        has_history=has_history,
        rail=rail,
        rail_computed_at=rail_computed_at,
        shelf=shelf_items,
        shelf_departs=shelf_departs,
        minutes=minutes,
    )


@bp.route("/recently-added")
@login_required
def recently_added():
    """Show the ten most recently added files."""

    page = request.args.get("page", 1, type=int)

    # Show only files added in the last 7 days, as the AWS S3 lifecycle rule migrates
    # older files that have been uploaded to S3 Deep Glacier storage; this way we only
    # show files that are still in a Standard data storage class and can be re-downloaded
    # without needing to unfreeze from S3 Glacier.

    recently_added = (
        File.query.outerjoin(FileAudioTrack, (FileAudioTrack.file_id == File.id))
        .distinct()  # need .distinct() in order to get the result numbers per page correct
        .outerjoin(Movie, (Movie.id == File.movie_id))
        .outerjoin(TVSeries, (TVSeries.id == File.series_id))
        .filter(
            db.func.coalesce(File.date_updated, File.date_added)
            >= db.func.adddate(db.func.current_date(), -7)
        )
        .order_by(db.func.coalesce(File.date_updated, File.date_added).desc())
        .paginate(page=page, per_page=100, error_out=False)
    )

    next_url = (
        url_for("main.recently_added", page=recently_added.next_num)
        if recently_added.has_next
        else None
    )
    prev_url = (
        url_for("main.recently_added", page=recently_added.prev_num)
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
        for job, next_run in current_app.import_scheduler.get_jobs(with_times=True):
            if (
                job.func_name == "app.videos.localization_task"
                and job.origin == "fitzflix-import"
            ):
                deferred.append(
                    {"description": job.description or job.id, "next_run": next_run}
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
        "recently_added.html",
        title="Recently Added",
        recently_added=recently_added.items,
        native_language=[current_app.config["NATIVE_LANGUAGE"], "und", "zxx"],
        import_activity=import_activity,
        next_url=next_url,
        prev_url=prev_url,
        pages=recently_added,
        upgrade_threshold=_upgrade_threshold(),
    )


def _tmdb_person_details(person_id):
    """The person's name, photo, and biographical fields from TMDb, cached
    for a day; None when there's no API key or TMDb doesn't answer with a
    name, which the filmography treats as an unknown person.
    """

    if not current_app.config["TMDB_API_KEY"]:
        return None
    cache_key = f"fitzflix:tmdb:person:{person_id}:details"
    cached = current_app.redis.get(cache_key)
    if cached:
        return json.loads(cached)
    try:
        r = tmdb_get(
            current_app.config["TMDB_API_URL"] + f"/person/{person_id}",
            params={"api_key": current_app.config["TMDB_API_KEY"]},
            timeout=10,
        )
        r.raise_for_status()
        payload = r.json() or {}
    except Exception:
        current_app.logger.warning(traceback.format_exc())
        return None
    if not payload.get("name"):
        return None
    details = {
        "name": payload["name"],
        "profile_path": payload.get("profile_path"),
        "biography": payload.get("biography"),
        "birthday": payload.get("birthday"),
        "deathday": payload.get("deathday"),
        "place_of_birth": payload.get("place_of_birth"),
    }
    current_app.redis.set(cache_key, json.dumps(details), ex=86400)
    return details


def _tmdb_date(value):
    """A date from TMDb's YYYY-MM-DD strings; None when absent or odd."""

    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _person_bio(details):
    """Preformatted born/died lines and biography text for the filmography
    header, from a TMDb person-details dict. Ages compute against the
    death date when there is one.
    """

    birthday = _tmdb_date(details.get("birthday"))
    deathday = _tmdb_date(details.get("deathday"))
    age = None
    if birthday:
        end = deathday or date.today()
        age = (
            end.year
            - birthday.year
            - ((end.month, end.day) < (birthday.month, birthday.day))
        )
    born_line = None
    if birthday:
        born_line = f"Born {birthday.strftime('%B %-d, %Y')}"
        if details.get("place_of_birth"):
            born_line += f" in {details['place_of_birth']}"
        if not deathday and age is not None:
            born_line += f" (age {age})"
    died_line = None
    if deathday:
        died_line = f"Died {deathday.strftime('%B %-d, %Y')}"
        if age is not None:
            died_line += f" (aged {age})"
    return {
        "born_line": born_line,
        "died_line": died_line,
        "biography": (details.get("biography") or "").strip(),
    }


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
            movie_file_rank(),
        )
        .join(Movie, (Movie.id == File.movie_id))
        .join(RefQuality, (RefQuality.id == File.quality_id))
        .subquery()
    )

    if credit:
        # Credit ids are TMDb person ids, so the filmography isn't limited
        # to people with local credit rows: anyone TMDb knows can be
        # browsed from any cast list. The day-cached TMDb person lookup
        # supplies the biography for everyone; a local credit row backstops
        # the name and photo when TMDb can't be reached

        person = TMDBCredit.query.filter_by(id=int(credit)).first()
        details = _tmdb_person_details(int(credit)) or {}
        person_name = details.get("name") or (person.name if person else None)
        person_profile_path = details.get("profile_path") or (
            person.tmdb_profile_path if person else None
        )
        if person_name is None:
            abort(404)
        bio = _person_bio(details) if details else None

        # The filmography shows the person's entire TMDb career, whether
        # or not a film has any local record. Local rows attach the best
        # owned file through an outer join (the rank condition has to live
        # in the join, not the WHERE clause, or file-less review-only
        # records would be filtered away); the full credit list comes from
        # TMDb, cached for a day.

        best_file_ids = db.session.query(ranked_files.c.id).filter(
            ranked_files.c.rank == 1
        )
        local_rows = (
            db.session.query(File, Movie, RefQuality)
            .select_from(Movie)
            .join(MovieCast, (MovieCast.movie_id == Movie.id))
            .outerjoin(
                File,
                db.and_(
                    File.movie_id == Movie.id,
                    File.feature_type_id == None,
                    File.id.in_(best_file_ids),
                ),
            )
            .outerjoin(RefQuality, (RefQuality.id == File.quality_id))
            .filter(MovieCast.credit_id == int(credit))
            .all()
        )

        # Best owned copy per movie (a movie can have several rank-1
        # editions; the filmography shows one entry per film)

        local = {}
        for file, film, quality in local_rows:
            existing = local.get(film.id)
            if (
                existing is None
                or (quality is not None and existing["quality"] is None)
                or (
                    quality is not None
                    and existing["quality"] is not None
                    and quality.preference > existing["quality"].preference
                )
            ):
                local[film.id] = {"movie": film, "file": file, "quality": quality}

        reviewed = {
            movie_id: bool(liked)
            for movie_id, liked in db.session.query(
                UserMovieReview.movie_id,
                db.func.max(db.case((UserMovieReview.liked == True, 1), else_=0)),
            )
            .filter(UserMovieReview.user_id == int(current_user.id))
            .filter(UserMovieReview.movie_id.in_(list(local.keys()) or [0]))
            .group_by(UserMovieReview.movie_id)
            .all()
        }

        # The person's full TMDb credit list, cached for a day

        tmdb_credits = None
        if current_app.config["TMDB_API_KEY"]:
            cache_key = f"fitzflix:tmdb:person:{int(credit)}:credits"
            cached = current_app.redis.get(cache_key)
            if cached:
                tmdb_credits = json.loads(cached)
            else:
                try:
                    r = tmdb_get(
                        current_app.config["TMDB_API_URL"]
                        + f"/person/{int(credit)}/movie_credits",
                        params={"api_key": current_app.config["TMDB_API_KEY"]},
                        timeout=10,
                    )
                    r.raise_for_status()
                    tmdb_credits = r.json().get("cast") or []
                    current_app.redis.set(cache_key, json.dumps(tmdb_credits), ex=86400)
                except Exception:
                    current_app.logger.warning(traceback.format_exc())

        # Merge: one row per film, TMDb credits first (deduped by film,
        # combining characters), then any local credits TMDb didn't list

        local_by_tmdb_id = {
            entry["movie"].tmdb_id: entry
            for entry in local.values()
            if entry["movie"].tmdb_id is not None
        }
        rows = {}
        for cast_credit in tmdb_credits or []:
            tmdb_id = cast_credit.get("id")
            if tmdb_id is None:
                continue
            release_year = (cast_credit.get("release_date") or "")[:4]
            row = rows.get(tmdb_id)
            if row is None:
                entry = local_by_tmdb_id.get(tmdb_id)
                row = rows[tmdb_id] = {
                    "tmdb_id": tmdb_id,
                    "title": cast_credit.get("title"),
                    "year": int(release_year) if release_year.isdigit() else None,
                    "poster_path": cast_credit.get("poster_path"),
                    "genre_ids": cast_credit.get("genre_ids") or [],
                    "overview": cast_credit.get("overview"),
                    "characters": [],
                    "movie": entry["movie"] if entry else None,
                    "file": entry["file"] if entry else None,
                    "quality": entry["quality"] if entry else None,
                }
            if cast_credit.get("character"):
                row["characters"].append(cast_credit["character"])

        matched_tmdb_ids = set(rows.keys())
        for entry in local.values():
            if entry["movie"].tmdb_id in matched_tmdb_ids:
                continue
            rows[f"local:{entry['movie'].id}"] = {
                "tmdb_id": entry["movie"].tmdb_id,
                "title": entry["movie"].tmdb_title or entry["movie"].title,
                "year": entry["movie"].year,
                "poster_path": entry["movie"].tmdb_poster_path,
                "overview": entry["movie"].tmdb_overview,
                "characters": [],
                "movie": entry["movie"],
                "file": entry["file"],
                "quality": entry["quality"],
            }

        filmography = sorted(
            rows.values(), key=lambda row: (row["year"] is None, row["year"] or 0)
        )
        for row in filmography:
            row["seen"] = row["movie"] is not None and row["movie"].id in reviewed
            row["liked"] = bool(row["movie"] and reviewed.get(row["movie"].id))

        # Modest "might interest you" markers on unowned films, scored at
        # render time from the already-cached credits payload against the
        # user's stored taste profile — no TMDb calls, nothing persisted

        profile = stored_profile(current_app.redis, current_user.id)
        interesting = credit_interest_markers(profile, int(credit), filmography)
        for row in filmography:
            row["might_interest"] = row["tmdb_id"] in interesting

        # Streaming badges on films without a local file, filtered to
        # this user's services. Availability is batch-fetched cache-first,
        # but a career can span hundreds of films and every fetch shares
        # the app-wide TMDb rate limiter, so a render fetches at most 50
        # and a background task warms the rest for the next visit

        streaming_attribution = False
        provider_ids = user_provider_ids(current_user)
        if provider_ids:
            availability_by_id, deferred = batch_title_availability(
                (
                    row["tmdb_id"]
                    for row in filmography
                    if row["tmdb_id"] and not row["quality"]
                ),
                fetch_limit=50,
            )
            if deferred and current_app.redis.set(
                f"fitzflix:streaming:warm:{int(credit)}", "1", nx=True, ex=900
            ):
                current_app.maintenance_queue.enqueue(
                    "app.streaming.warm_title_availability",
                    args=(deferred,),
                    job_timeout="30m",
                    description=(
                        f"Warming streaming availability for {len(deferred)} films"
                    ),
                )
            for row in filmography:
                if row["quality"] or not row["tmdb_id"]:
                    continue
                availability = availability_by_id.get(row["tmdb_id"])
                matches = streaming_matches(availability, provider_ids)
                rentals = rental_matches(availability, provider_ids)
                if matches:
                    row["streaming"] = matches
                if rentals:
                    row["rentals"] = rentals
                if matches or rentals:
                    streaming_attribution = True

        return render_template(
            "filmography.html",
            title=f"Movies starring {person_name}",
            person_name=person_name,
            profile_path=person_profile_path,
            bio=bio,
            filmography=filmography,
            tmdb_unavailable=tmdb_credits is None,
            streaming_attribution=streaming_attribution,
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
                        (Movie.tmdb_title != None, Movie.tmdb_title),
                        else_=Movie.title,
                    ),
                    "^(The|A|An) ",
                    "",
                ).asc(),
                db.case(
                    (Movie.tmdb_title != None, Movie.tmdb_release_date),
                    else_=Movie.year,
                ).asc(),
                File.edition.asc(),
            )
            .paginate(page=page, per_page=120, error_out=False)
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
                        (Movie.tmdb_title != None, Movie.tmdb_title),
                        else_=Movie.title,
                    ),
                    "^(The|A|An) ",
                    "",
                ).asc(),
                db.case(
                    (Movie.tmdb_title != None, Movie.tmdb_release_date),
                    else_=Movie.year,
                ).asc(),
                File.edition.asc(),
            )
            .paginate(page=page, per_page=120, error_out=False)
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
                        (Movie.tmdb_title != None, Movie.tmdb_title),
                        else_=Movie.title,
                    ),
                    "^(The|A|An) ",
                    "",
                ).asc(),
                db.case(
                    (Movie.tmdb_title != None, Movie.tmdb_release_date),
                    else_=Movie.year,
                ).asc(),
                File.edition.asc(),
            )
            .paginate(page=page, per_page=120, error_out=False)
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
        upgrade_threshold=_upgrade_threshold(),
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
            movie_file_rank(),
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
                Movie.criterion_spine_number != None,
                Movie.criterion_set_title != None,
            )
        )
        .filter(File.edition == None)
        .filter(
            db.or_(
                Movie.criterion_disc_owned == True, Movie.criterion_disc_owned == owned
            )
        )
        .order_by(
            db.case((Movie.criterion_spine_number != None, 1), else_=0).desc(),
            Movie.criterion_spine_number.asc(),
            Movie.criterion_set_title.asc(),
            db.case(
                (Movie.tmdb_title != None, Movie.tmdb_release_date), else_=Movie.year
            ).asc(),
            db.func.regexp_replace(
                db.case(
                    (Movie.tmdb_title != None, Movie.tmdb_title), else_=Movie.title
                ),
                "^(The|A|An) ",
                "",
            ).asc(),
            File.edition.asc(),
        )
        .all()
    )

    return render_template(
        "library_criterion.html",
        title="Criterion Collection films",
        movies=movies,
        filter_form=filter_form,
    )


@bp.route("/movie/<int:movie_id>", methods=["GET", "POST"])
@login_required
def movie(movie_id):
    """Show details for a particular movie."""

    movie = Movie.query.filter_by(id=movie_id).first_or_404()
    title = f"{movie.tmdb_title if movie.tmdb_title else movie.title} ({movie.tmdb_release_date.strftime('%Y') if movie.tmdb_title else movie.year})"
    # Every credited actor in billing order for the cast scroller

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
    genres = [genre.name for genre in movie.genres]
    review = (
        UserMovieReview.query.filter_by(user_id=int(current_user.id), movie_id=movie.id)
        .order_by(UserMovieReview.date_reviewed.desc())
        .first()
    )
    films = (
        File.query.join(RefQuality, (RefQuality.id == File.quality_id))
        .filter(File.movie_id == movie_id)
        .filter(File.feature_type_id == None)
        .order_by(
            File.fullscreen.asc(), File.edition.asc(), RefQuality.preference.desc()
        )
        .all()
    )
    features = (
        File.query.filter(File.movie_id == movie_id)
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
        # The rating is optional — a review can be just a like and/or text.
        # star_rating_fields rounds a given rating to the nearest half star
        # for display.

        rating = (
            float(movie_review_form.rating.data)
            if movie_review_form.rating.data is not None
            else None
        )
        # A bare submission (no rating, like, or text) is a plain diary
        # entry — a watch, not a review — so it carries no review date.
        # Rewatch is computed the way Plex watches compute it: any earlier
        # row for this user and film makes this a repeat viewing.

        is_review = bool(
            rating is not None
            or movie_review_form.liked.data
            or (movie_review_form.review.data or "").strip()
        )
        rewatch = (
            db.session.query(UserMovieReview.id)
            .filter_by(user_id=current_user.id, movie_id=movie.id)
            .first()
            is not None
        )
        review = UserMovieReview(
            user_id=current_user.id,
            movie_id=movie.id,
            review=movie_review_form.review.data,
            liked=movie_review_form.liked.data,
            date_watched=_watched_timestamp(movie_review_form.date_watched.data),
            date_reviewed=datetime.now() if is_review else None,
            rewatch=rewatch,
            **star_rating_fields(rating),
        )
        db.session.add(review)
        db.session.commit()
        if rating is not None:
            flash(f"Rated '{title}' {rating:g} out of 5 stars", "success")
        elif is_review:
            flash(f"Logged review for '{title}'", "success")
        else:
            flash(f"Logged '{title}' in your history", "success")
        return redirect(url_for("main.movie", movie_id=movie.id))

    transcode_form = TranscodeForm()

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
    qualities = (
        db.session.query(RefQuality.id, RefQuality.quality_title)
        .filter(
            db.or_(
                RefQuality.id == 1,
                db.and_(
                    RefQuality.physical_media == 1,
                    db.or_(
                        RefQuality.quality_title == "DVD",
                        RefQuality.quality_title.like("%1080p"),
                        RefQuality.quality_title.like("%2160p"),
                    ),
                ),
            )
        )
        .order_by(RefQuality.preference.asc())
        .all()
    )
    criterion_form.quality.choices = [(str(id), title) for (id, title) in qualities]
    criterion_form.quality.default = movie.criterion_quality_id
    if criterion_form.criterion_submit.data and criterion_form.validate_on_submit():
        movie.criterion_spine_number = criterion_form.spine_number.data
        if criterion_form.set_title.data:
            movie.criterion_set_title = criterion_form.set_title.data

        else:
            movie.criterion_set_title = None

        movie.criterion_in_print = criterion_form.in_print.data
        movie.criterion_disc_owned = criterion_form.owned.data
        movie.criterion_quality_id = criterion_form.quality.data

        db.session.commit()
        flash(f"Updated Criterion Collection details for '{title}'")
        return redirect(url_for("main.movie", movie_id=movie.id))
    criterion_form.process()

    # Streaming availability for this user's services; quiet for users
    # who picked none. Owned films lead with "In your library" (with or
    # without a streaming match), while a film with no local files says
    # "not on your services" — that's where the watch-or-buy decision
    # actually lives

    streaming = (
        user_streaming(
            movie.tmdb_id, current_user, negative=not films, local=bool(films)
        )
        if movie.tmdb_id
        else None
    )

    return render_template(
        "movie.html",
        title=title,
        movie=movie,
        cast=cast,
        genres=genres,
        review=review,
        films=films,
        features=features,
        movie_shopping_exclude_form=movie_shopping_exclude_form,
        movie_review_form=movie_review_form,
        transcode_form=transcode_form,
        tmdb_lookup_form=tmdb_lookup_form,
        criterion_form=criterion_form,
        streaming=streaming,
        radarr_proxy_url=current_app.config["RADARR_PROXY_URL"],
    )


def _custom_poster_dir(scope, record_id):
    """Where a movie's or file's custom poster tree lives.

    CUSTOM_ARTWORK_DIR is app/static/custom in production, so these files
    are served by url_for('static', ...) and mirrored to S3 by the nightly
    backup (which also propagates deletions).
    """

    return os.path.join(current_app.config["CUSTOM_ARTWORK_DIR"], scope, str(record_id))


def _assign_movie_poster(movie, uploaded_data):
    """Run a poster image through the custom-poster pipeline for a movie:
    thumbnails, a copy beside each main-feature file, precedence column.

    Returns a message to flash on failure, or None on success.
    """

    file_ext = os.path.splitext(secure_filename(uploaded_data.filename))[1]
    poster_filename = f"poster{file_ext}"

    if file_ext not in [".jpg", ".jpeg", ".png", ".tbn"]:
        return f"'{poster_filename}' is an invalid movie poster file type!"

    movie_files = (
        File.query.filter(File.movie_id == movie.id)
        .filter(File.feature_type_id == None)
        .all()
    )

    try:
        try:
            original_file = save_custom_poster(
                uploaded_data,
                poster_filename,
                _custom_poster_dir("movie", movie.id),
            )
        except ValueError as error:
            return str(error)

        for file in movie_files:
            replace_library_poster(
                os.path.join(current_app.config["LIBRARY_DIR"], file.dirname),
                original_file,
                poster_filename,
            )

        movie.custom_poster = poster_filename
        db.session.commit()

    except Exception:
        current_app.logger.error(traceback.format_exc())
        db.session.rollback()
        return f"Unable to assign a custom poster to '{movie.title}'!"

    return None


def _assign_file_poster(file, uploaded_data):
    """The file-scoped twin of _assign_movie_poster: one file's custom
    poster, replacing the library copy only for a main feature."""

    file_ext = os.path.splitext(secure_filename(uploaded_data.filename))[1]
    poster_filename = f"poster{file_ext}"

    if file_ext not in [".jpg", ".jpeg", ".png", ".tbn"]:
        return f"'{poster_filename}' is an invalid movie poster file type!"

    try:
        try:
            original_file = save_custom_poster(
                uploaded_data,
                poster_filename,
                _custom_poster_dir("file", file.id),
            )
        except ValueError as error:
            return str(error)

        if file.feature_type_id == None:
            replace_library_poster(
                os.path.join(current_app.config["LIBRARY_DIR"], file.dirname),
                original_file,
                poster_filename,
            )

        file.custom_poster = poster_filename
        db.session.commit()

    except Exception:
        current_app.logger.error(traceback.format_exc())
        db.session.rollback()
        return f"Unable to assign a custom poster to '{file.basename}'!"

    return None


def _remove_movie_poster(movie):
    """Delete a movie's custom poster: the custom-artwork tree, the copies
    beside its library files, and the precedence column.

    A main-feature file with its own custom poster keeps that art — the
    file-over-movie precedence means its library copy gets restored from
    the file-scoped original rather than deleted.

    Returns a message to flash on failure, or None on success.
    """

    poster_filename = movie.custom_poster
    try:
        movie_files = (
            File.query.filter(File.movie_id == movie.id)
            .filter(File.feature_type_id == None)
            .all()
        )
        for file in movie_files:
            library_directory = os.path.join(
                current_app.config["LIBRARY_DIR"], file.dirname
            )
            if not os.path.isdir(library_directory):
                continue
            if file.custom_poster:
                file_original = os.path.join(
                    _custom_poster_dir("file", file.id), "original", file.custom_poster
                )
                if os.path.isfile(file_original):
                    replace_library_poster(
                        library_directory, file_original, file.custom_poster
                    )
                    continue
            library_copy = os.path.join(library_directory, poster_filename)
            if os.path.isfile(library_copy):
                os.remove(library_copy)
                current_app.logger.info(f"Deleted '{library_copy}'")

        shutil.rmtree(_custom_poster_dir("movie", movie.id), ignore_errors=True)
        movie.custom_poster = None
        db.session.commit()

    except Exception:
        current_app.logger.error(traceback.format_exc())
        db.session.rollback()
        return f"Unable to remove the custom poster for '{movie.title}'!"

    return None


def _remove_file_poster(file):
    """The file-scoped twin of _remove_movie_poster.

    When the movie still has its own custom poster, that art is restored
    to the library directory; otherwise the poster copy is deleted.
    """

    poster_filename = file.custom_poster
    try:
        if file.feature_type_id == None:
            library_directory = os.path.join(
                current_app.config["LIBRARY_DIR"], file.dirname
            )
            if os.path.isdir(library_directory):
                movie = file.movie
                movie_original = (
                    os.path.join(
                        _custom_poster_dir("movie", movie.id),
                        "original",
                        movie.custom_poster,
                    )
                    if movie and movie.custom_poster
                    else None
                )
                if movie_original and os.path.isfile(movie_original):
                    replace_library_poster(
                        library_directory, movie_original, movie.custom_poster
                    )
                else:
                    library_copy = os.path.join(library_directory, poster_filename)
                    if os.path.isfile(library_copy):
                        os.remove(library_copy)
                        current_app.logger.info(f"Deleted '{library_copy}'")

        shutil.rmtree(_custom_poster_dir("file", file.id), ignore_errors=True)
        file.custom_poster = None
        db.session.commit()

    except Exception:
        current_app.logger.error(traceback.format_exc())
        db.session.rollback()
        return f"Unable to remove the custom poster for '{file.basename}'!"

    return None


def _tmdb_poster_gallery(tmdb_id):
    """The TMDb poster gallery for a movie, cached for a day.

    Returns the /movie/{id}/images posters list, or None when the gallery
    is unavailable (no TMDb id, no API key, or the fetch failed).
    """

    if not tmdb_id:
        return None
    cache_key = f"fitzflix:tmdb:movie:{tmdb_id}:posters"
    cached = current_app.redis.get(cache_key)
    if cached:
        return json.loads(cached)
    if not current_app.config["TMDB_API_KEY"]:
        return None
    try:
        r = tmdb_get(
            current_app.config["TMDB_API_URL"] + f"/movie/{tmdb_id}/images",
            params={"api_key": current_app.config["TMDB_API_KEY"]},
            timeout=10,
        )
        r.raise_for_status()
        posters = r.json().get("posters") or []
    except Exception:
        current_app.logger.warning(traceback.format_exc())
        return None
    current_app.redis.set(cache_key, json.dumps(posters), ex=86400)
    return posters


def _fetch_tmdb_poster(poster_path):
    """Download a TMDb poster and wrap it like a form upload, so a picked
    poster flows through the exact same pipeline as an uploaded one.

    Returns (file_storage, error_message).
    """

    if not re.fullmatch(r"/[A-Za-z0-9]+\.(?:jpg|jpeg|png)", poster_path or ""):
        return None, "That isn't a TMDb poster path."
    try:
        r = requests.get(
            f"{current_app.config['TMDB_IMAGE_URL']}/original{poster_path}",
            timeout=30,
        )
        r.raise_for_status()
    except Exception:
        current_app.logger.error(traceback.format_exc())
        return None, "Couldn't download that poster from TMDb."
    return (
        FileStorage(
            stream=io.BytesIO(r.content), filename=os.path.basename(poster_path)
        ),
        None,
    )


def _poster_gallery_context(posters):
    """Split a poster gallery into the languages present and the subset to
    show for the request's ?language= filter."""

    languages = sorted({p.get("iso_639_1") or "none" for p in posters or []})
    active = request.args.get("language")
    if active not in languages and active != "all":
        # Default to English posters when any exist, otherwise show all
        active = "en" if "en" in languages else "all"
    if posters and active != "all":
        shown = [p for p in posters if (p.get("iso_639_1") or "none") == active]
    else:
        shown = posters
    return shown, languages, active


@bp.route("/movie/<int:movie_id>/poster", methods=["GET", "POST"])
@login_required
def movie_poster(movie_id):
    """Poster picker: choose from the TMDb gallery or upload an image."""

    movie = Movie.query.filter_by(id=movie_id).first_or_404()
    title = f"{movie.tmdb_title if movie.tmdb_title else movie.title} ({movie.tmdb_release_date.strftime('%Y') if movie.tmdb_title else movie.year})"

    custom_poster_form = CustomPosterUploadForm()
    poster_select_form = TMDBPosterSelectForm()
    poster_remove_form = CustomPosterRemoveForm()

    if (
        poster_remove_form.poster_remove_submit.data
        and poster_remove_form.validate_on_submit()
    ):
        if not movie.custom_poster:
            flash(f"'{title}' has no custom poster to remove.", "warning")
            return redirect(url_for("main.movie_poster", movie_id=movie.id))
        error = _remove_movie_poster(movie)
        if error:
            flash(error, "danger")
        else:
            flash(f"Removed the custom poster for '{title}'", "success")
        return redirect(url_for("main.movie_poster", movie_id=movie.id))

    if (
        custom_poster_form.poster_submit.data
        and custom_poster_form.validate_on_submit()
    ):
        error = _assign_movie_poster(movie, custom_poster_form.custom_poster.data)
        if error:
            flash(error, "danger")
            return redirect(url_for("main.movie_poster", movie_id=movie.id))
        flash(f"Uploaded a custom poster for '{title}'", "success")
        return redirect(url_for("main.movie", movie_id=movie.id))

    if (
        poster_select_form.poster_select_submit.data
        and poster_select_form.validate_on_submit()
    ):
        uploaded_data, error = _fetch_tmdb_poster(poster_select_form.poster_path.data)
        if not error:
            error = _assign_movie_poster(movie, uploaded_data)
        if error:
            flash(error, "danger")
            return redirect(url_for("main.movie_poster", movie_id=movie.id))
        flash(f"Set the poster for '{title}' from TMDb", "success")
        return redirect(url_for("main.movie", movie_id=movie.id))

    posters, languages, active_language = _poster_gallery_context(
        _tmdb_poster_gallery(movie.tmdb_id)
    )

    return render_template(
        "poster_picker.html",
        title=f'Poster for "{title}"',
        movie=movie,
        back_url=url_for("main.movie", movie_id=movie.id),
        back_label=title,
        posters=posters,
        languages=languages,
        active_language=active_language,
        language_url=lambda language: url_for(
            "main.movie_poster", movie_id=movie.id, language=language
        ),
        custom_poster_form=custom_poster_form,
        poster_select_form=poster_select_form,
        poster_remove_form=poster_remove_form,
        has_custom_poster=bool(movie.custom_poster),
        default_poster_path=movie.tmdb_poster_path,
        upload_enabled=True,
    )


@bp.route("/people")
@login_required
def people():
    """Browse every credited person across the library's films.

    Defaults to people appearing in multiple films, since the long tail is
    one-appearance day players; searching by name widens to everyone, and
    uncredited-only roles never count toward the filter (Glenn's spec from
    GitHub #13). Each person links to their filmography page.
    """

    page = request.args.get("page", 1, type=int)
    query_text = (request.args.get("q") or "").strip()
    minimum_films = 1 if query_text else 2

    film_count = db.func.count(db.distinct(MovieCast.movie_id)).label("film_count")
    people_query = (
        db.session.query(
            TMDBCredit.id,
            TMDBCredit.name,
            TMDBCredit.tmdb_profile_path,
            film_count,
        )
        .join(MovieCast, MovieCast.credit_id == TMDBCredit.id)
        .filter(
            db.or_(
                MovieCast.character == None,
                db.not_(MovieCast.character.like("%(uncredited)%")),
            )
        )
        .group_by(TMDBCredit.id, TMDBCredit.name, TMDBCredit.tmdb_profile_path)
    )
    if query_text:
        people_query = people_query.filter(TMDBCredit.name.ilike(f"%{query_text}%"))
    # Ties break on surname: TMDb has no structured sort name, so the last
    # whitespace-separated token stands in for it (wrong for "Jr." suffixes
    # and multi-word surnames, fine as a tie-break)

    people_page = (
        people_query.having(film_count >= minimum_films)
        .order_by(
            film_count.desc(),
            db.func.substring_index(TMDBCredit.name, " ", -1).asc(),
            TMDBCredit.name.asc(),
        )
        .paginate(page=page, per_page=120, error_out=False)
    )

    return render_template(
        "people.html",
        title="People",
        people=people_page.items,
        pages=people_page,
        query_text=query_text,
        next_url=(
            url_for("main.people", page=people_page.next_num, q=query_text or None)
            if people_page.has_next
            else None
        ),
        prev_url=(
            url_for("main.people", page=people_page.prev_num, q=query_text or None)
            if people_page.has_prev
            else None
        ),
    )


@bp.route("/movie/<int:movie_id>/files")
@login_required
def movie_files(movie_id):
    """Show all files for a particular movie, regardless of ranking."""

    movie = Movie.query.filter_by(id=movie_id).first_or_404()
    title = f"Files for \"{movie.tmdb_title if movie.tmdb_title else movie.title} ({movie.tmdb_release_date.strftime('%Y') if movie.tmdb_title else movie.year})\""

    # Subquery to get the ranking for each of this movie's files

    ranked_files = (
        db.session.query(
            File.id,
            movie_file_rank(),
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
        .filter(Movie.id == movie_id)
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

    ranked_files = (
        db.session.query(
            File.id,
            tv_file_rank(),
        )
        .join(TVSeries, (TVSeries.id == File.series_id))
        .join(RefQuality, (RefQuality.id == File.quality_id))
        .subquery()
    )

    subquery = (
        db.session.query(
            File.series_id,
            File.season,
            db.func.count(db.func.distinct(File.episode)).label("episodes"),
            db.func.min(RefQuality.preference).label("preference"),
        )
        .group_by(File.series_id, File.season)
        .join(RefQuality, (RefQuality.id == File.quality_id))
        .join(ranked_files, (ranked_files.c.id == File.id))
        .filter(ranked_files.c.rank == 1)
        .subquery()
    )

    upgrade_threshold = _upgrade_threshold()

    # Run the season aggregate once for the whole library and bucket the rows
    # by series, rather than re-running the ranked subquery once per series

    season_rows = (
        db.session.query(
            subquery.c.series_id,
            subquery.c.season,
            subquery.c.episodes,
            RefQuality.preference,
            RefQuality.physical_media,
            RefQuality.quality_title,
        )
        .join(RefQuality, (RefQuality.preference == subquery.c.preference))
        .order_by(
            subquery.c.series_id,
            db.case((subquery.c.season == 0, 1), else_=0).asc(),
            subquery.c.season.asc(),
        )
        .all()
    )

    seasons_by_series = {}
    for (
        series_id,
        season,
        num_episodes,
        preference,
        physical,
        min_quality,
    ) in season_rows:
        seasons_by_series.setdefault(series_id, []).append(
            {
                "season": season,
                "episode_count": num_episodes,
                "min_quality": min_quality,
                # Physical-media seasons (DVD, SD/720p Blu-ray) are often the
                # only release that will ever exist, so they don't count as
                # upgradable
                "upgradable": not physical and preference < upgrade_threshold,
            }
        )

    tv = []
    for series in (
        TVSeries.query.join(File, (File.series_id == TVSeries.id))
        .distinct()
        .order_by(db.func.regexp_replace(TVSeries.title, "^(The|A|An) ", "").asc())
        .all()
    ):
        tv.append(
            {
                "id": series.id,
                "title": series.title,
                "tmdb_id": series.tmdb_id,
                "tmdb_name": series.tmdb_name,
                "tmdb_poster_path": series.tmdb_poster_path,
                "seasons": seasons_by_series.get(series.id, []),
            }
        )

    return render_template("library_tv.html", title="TV Library", series=tv)


def restore_cost_estimate(files, bulk=False):
    """Estimate the AWS cost of restoring and downloading archived files.

    Uses the archived object's exact size when it's been recorded; otherwise
    falls back to the localized copy's size with a 1.25x fudge-factor, since
    the archived original is typically larger than the localized copy.
    """

    if bulk:
        restore_request_cost = (
            current_app.config["AWS_RESTORE_PER_1K_REQUEST_BULK_COST"] / 1000
        )
        restore_per_gb_cost = current_app.config["AWS_RESTORE_PER_GB_BULK_COST"]
    else:
        restore_request_cost = (
            current_app.config["AWS_RESTORE_PER_1K_REQUEST_COST"] / 1000
        )
        restore_per_gb_cost = current_app.config["AWS_RESTORE_PER_GB_COST"]
    gigabytes = (
        sum(
            (
                file.aws_untouched_filesize_bytes
                if file.aws_untouched_filesize_bytes
                else file.filesize_bytes * 1.25 if file.filesize_bytes else 0
            )
            for file in files
        )
    ) / 1024**3
    cost = (len(files) * restore_request_cost) + (
        gigabytes
        * (restore_per_gb_cost + current_app.config["AWS_DOWNLOAD_PER_GB_COST"])
    )
    return {"count": len(files), "gigabytes": gigabytes, "cost": cost}


@bp.route("/tv/<int:series_id>", methods=["GET", "POST"])
@login_required
def tv(series_id):
    """Show details for a particular TV series."""

    tv = TVSeries.query.filter_by(id=series_id).first_or_404()
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
                tv_file_rank(),
            )
            .join(TVSeries, (TVSeries.id == File.series_id))
            .join(RefQuality, (RefQuality.id == File.quality_id))
            .subquery()
        )

        # Get details for all the best files for this TV series

        files = (
            File.query.join(ranked_files, (ranked_files.c.id == File.id))
            .filter(File.series_id == series_id)
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
                job_id=safe_job_id(file.plex_title),
            )

        flash(f"Added all files for '{title}' to transcoding queue", "success")
        return redirect(url_for("main.tv", series_id=tv.id))

    # Form to request every archived file for this series to be restored from
    # AWS Glacier; the hourly SQS poll downloads each one once it's ready.
    # Restores cost real money, so show an estimate and require the user's
    # password before requesting anything

    restore_ranked_files = (
        db.session.query(
            File.id,
            tv_file_rank(),
        )
        .join(TVSeries, (TVSeries.id == File.series_id))
        .join(RefQuality, (RefQuality.id == File.quality_id))
        .subquery()
    )

    series_restorable = (
        File.query.join(restore_ranked_files, (restore_ranked_files.c.id == File.id))
        .filter(File.series_id == series_id)
        .filter(restore_ranked_files.c.rank == 1)
        .filter(File.aws_untouched_key != None)
        .order_by(File.season.asc(), File.episode.asc())
        .all()
    )
    series_restore_estimate = restore_cost_estimate(series_restorable, bulk=True)

    series_restore_form = SeriesRestoreForm()
    if (
        series_restore_form.series_restore_submit.data
        and series_restore_form.validate_on_submit()
    ):
        if not current_user.check_password(series_restore_form.password.data):
            flash("Incorrect password provided!", "danger")

        else:
            for file in series_restorable:
                current_app.request_queue.enqueue(
                    "app.videos.aws_restore",
                    args=(file.aws_untouched_key,),
                    kwargs={"tier": "Bulk"},
                    job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
                    description=f"'{file.untouched_basename}'",
                )

            flash(
                f"Requesting {len(series_restorable)} file(s) for '{title}' to "
                f"be restored from AWS Glacier "
                f"(≈ ${series_restore_estimate['cost']:.2f})",
                "info",
            )

        return redirect(url_for("main.tv", series_id=tv.id))

    # Delete the TV series from the database

    series_delete_form = SeriesDeleteForm()
    if (
        series_delete_form.delete_submit.data
        and series_delete_form.validate_on_submit()
    ):
        aws_untouched_keys = []

        try:
            files = File.query.filter(File.series_id == series_id).all()
            for file in files:
                if file.aws_untouched_key:
                    aws_untouched_keys.append(file.aws_untouched_key)
                file.delete_local_file(delete_directory_tree=True)
                db.session.delete(file)

            db.session.delete(tv)
            db.session.commit()

        except Exception:
            db.session.rollback()
            flash(f"Unable to delete TV series '{title}'!", "danger")
            return redirect(url_for("main.tv", series_id=series_id))

        # Delete the AWS copies only after the database delete has committed, so
        # a failed commit can't leave database records whose backups are gone

        for aws_untouched_key in aws_untouched_keys:
            current_app.request_queue.enqueue(
                "app.videos.aws_delete",
                args=(aws_untouched_key,),
                job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
            )

        flash(
            f"Deleted TV series '{title}' and its files from the database.", "success"
        )
        return redirect(url_for("main.tv_library"))

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
                ).format(tv.title, url_for("main.tv", series_id=tv_id)),
                "info",
            )

        return redirect(url_for("main.tv", series_id=tv_id))

    return render_template(
        "tv.html",
        title=title,
        tv=tv,
        seasons=seasons,
        transcode_form=transcode_form,
        series_restore_form=series_restore_form,
        series_restore_estimate=series_restore_estimate,
        tmdb_lookup_form=tmdb_lookup_form,
        series_delete_form=series_delete_form,
    )


@bp.route("/tv/<int:series_id>/<int:season>", methods=["GET", "POST"])
@login_required
def season(series_id, season):
    """Show all files for a TV show's season, regardless of ranking.

    The int converters make a non-numeric series or season in the URL a 404
    instead of a ValueError further down.
    """

    tv = TVSeries.query.filter_by(id=series_id).first_or_404()

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
            tv_file_rank(),
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
        .filter(TVSeries.id == series_id)
        .filter(File.season == season)
        .order_by(
            File.episode.asc(), RefQuality.preference.desc(), File.last_episode.desc()
        )
        .all()
    )

    # Form to request this season's best archived files to be restored from
    # AWS Glacier; the hourly SQS poll downloads each one once it's ready.
    # Restores cost real money, so show an estimate and require the user's
    # password before requesting anything

    restorable = [
        file for file, _, _, rank in files if rank == 1 and file.aws_untouched_key
    ]
    season_restore_estimate = restore_cost_estimate(restorable, bulk=True)

    season_restore_form = SeasonRestoreForm()
    if (
        season_restore_form.season_restore_submit.data
        and season_restore_form.validate_on_submit()
    ):
        if not current_user.check_password(season_restore_form.password.data):
            flash("Incorrect password provided!", "danger")

        else:
            for file in restorable:
                current_app.request_queue.enqueue(
                    "app.videos.aws_restore",
                    args=(file.aws_untouched_key,),
                    kwargs={"tier": "Bulk"},
                    job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
                    description=f"'{file.untouched_basename}'",
                )

            season_name = "specials" if season == 0 else f"season {season}"
            flash(
                f"Requesting {len(restorable)} file(s) for {season_name} to be "
                f"restored from AWS Glacier "
                f"(≈ ${season_restore_estimate['cost']:.2f})",
                "info",
            )

        return redirect(url_for("main.season", series_id=series_id, season=season))

    return render_template(
        "season.html",
        title=title,
        tv=tv,
        season=season,
        files=files,
        season_restore_form=season_restore_form,
        season_restore_estimate=season_restore_estimate,
    )


@bp.route("/file/<int:file_id>", methods=["GET", "POST"])
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

    file = File.query.filter_by(id=file_id).first_or_404()
    title = file.basename

    # When the file isn't present in the local library, only restoring it from
    # AWS or deleting it make sense: the template disables the other forms,
    # and their submit handlers below refuse stale submissions

    file_exists_locally = os.path.isfile(
        os.path.join(current_app.config["LIBRARY_DIR"], file.file_path)
    )

    # Since the video file can be for either a movie or a tv show, determine which
    # it belongs to based off whether it has a movie_id or a series_id, get the
    # associated movie or tv series information

    if file.movie_id:
        movie = Movie.query.filter_by(id=int(file.movie_id)).first_or_404()
        tv = None
        file_rank = (
            db.session.query(
                File.id,
                movie_file_rank(),
            )
            .join(Movie, (Movie.id == File.movie_id))
            .join(RefQuality, (RefQuality.id == File.quality_id))
            .subquery()
        )
        best_file = (
            db.session.query(
                File,
                db.case((file_rank.c.rank == 1, 1), else_=0).label("rank"),
            )
            .join(file_rank, (file_rank.c.id == File.id))
            .filter(File.id == file_id)
            .filter(file_rank.c.rank == 1)
            .first()
        )

    elif file.series_id:
        movie = None
        tv = TVSeries.query.filter_by(id=int(file.series_id)).first_or_404()
        file_rank = (
            db.session.query(
                File.id,
                tv_file_rank(),
            )
            .join(TVSeries, (TVSeries.id == File.series_id))
            .join(RefQuality, (RefQuality.id == File.quality_id))
            .subquery()
        )
        best_file = (
            db.session.query(
                File,
                db.case((file_rank.c.rank == 1, 1), else_=0).label("rank"),
            )
            .join(file_rank, (file_rank.c.id == File.id))
            .filter(File.id == file_id)
            .filter(file_rank.c.rank == 1)
            .first()
        )

    # Get the details of each of the audio and subtitle tracks for this file

    audio_tracks = FileAudioTrack.query.filter_by(file_id=file.id).all()
    subtitle_tracks = FileSubtitleTrack.query.filter_by(file_id=file.id).all()

    # Form to rescan the file's metadata

    metadata_scan_form = TrackMetadataScanForm()

    if metadata_scan_form.scan_submit.data and metadata_scan_form.validate_on_submit():
        if not file_exists_locally:
            flash(f"'{file.basename}' is not present locally.", "warning")
            return redirect(url_for("main.file", file_id=file.id))

        if track_metadata_scan(file.id):
            flash(f"Rescanned track metadata for '{file.basename}'", "info")
        else:
            flash(
                f"'{file.basename}' is being processed by another task; "
                f"try again once it finishes.",
                "warning",
            )
        return redirect(url_for("main.file", file_id=file.id))

    # Form to edit the file's attributes

    mkvpropedit_form = MKVPropEditForm()

    # Choices and defaults are strings, since that's what WTForms coerces the
    # submitted values to; ints here would fail the "valid choice" validation

    default_audio_choices = []
    default_audio_track_number = "1"
    for audio_track in audio_tracks:
        if (
            audio_track.compression_mode == "Lossless"
            and audio_track.bit_depth
            and audio_track.sampling_rate_khz
        ):
            default_audio_choices.append(
                (
                    str(audio_track.track),
                    f"{audio_track.language_name}: {audio_track.codec} {audio_track.channels} ({audio_track.bit_depth}-bit {audio_track.sampling_rate_khz} khz)",
                )
            )
        elif audio_track.bitrate_kbps:
            default_audio_choices.append(
                (
                    str(audio_track.track),
                    f"{audio_track.language_name}: {audio_track.codec} {audio_track.channels} ({audio_track.bitrate_kbps} kbps)",
                )
            )
        else:
            default_audio_choices.append(
                (
                    str(audio_track.track),
                    f"{audio_track.language_name}: {audio_track.codec} {audio_track.channels}",
                )
            )

        if audio_track.default == True:
            default_audio_track_number = str(audio_track.track)

    if audio_tracks:
        mkvpropedit_form.default_audio.choices = default_audio_choices
        mkvpropedit_form.default_audio.default = default_audio_track_number

    else:
        # No audio tracks: remove the field entirely, so its empty radio group
        # can't fail validation and block subtitle-only property edits
        # (the template already skips rendering it via {% if audio_tracks %})

        del mkvpropedit_form.default_audio

    default_subtitle_choices = [("0", "None")]
    default_subtitle_track_number = "0"

    forced_subtitle_choices = []
    default_forced_subtitles = []

    for subtitle_track in subtitle_tracks:
        default_subtitle_choices.append(
            (
                str(subtitle_track.track),
                f"{subtitle_track.elements}-element {subtitle_track.language_name}",
            )
        )
        if subtitle_track.default == True:
            default_subtitle_track_number = str(subtitle_track.track)

        forced_subtitle_choices.append(
            (
                str(subtitle_track.track),
                f"{subtitle_track.elements}-element {subtitle_track.language_name}",
            )
        )
        if subtitle_track.forced == True:
            default_forced_subtitles.append(str(subtitle_track.track))

    mkvpropedit_form.default_subtitle.choices = default_subtitle_choices
    mkvpropedit_form.default_subtitle.default = default_subtitle_track_number

    mkvpropedit_form.forced_subtitles.choices = forced_subtitle_choices
    mkvpropedit_form.forced_subtitles.default = default_forced_subtitles

    if (
        mkvpropedit_form.mkvpropedit_submit.data
        and mkvpropedit_form.validate_on_submit()
    ):
        if not file_exists_locally:
            flash(f"'{file.basename}' is not present locally.", "warning")
            return redirect(url_for("main.file", file_id=file.id))

        # The default_audio field is deleted from the form when the file has
        # no audio tracks; None tells the task there's no default to set

        default_audio_track = (
            mkvpropedit_form.default_audio.data if audio_tracks else None
        )

        current_app.logger.debug(f"Default audio: {default_audio_track}")
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
                    default_audio_track,
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
        if (
            audio_track.compression_mode == "Lossless"
            and audio_track.bit_depth
            and audio_track.sampling_rate_khz
        ):
            audio_track_choices.append(
                (
                    str(audio_track.track),
                    f"{audio_track.language_name}: {audio_track.codec} {audio_track.channels} ({audio_track.bit_depth}-bit {audio_track.sampling_rate_khz} khz)",
                )
            )
        elif audio_track.bitrate_kbps:
            audio_track_choices.append(
                (
                    str(audio_track.track),
                    f"{audio_track.language_name}: {audio_track.codec} {audio_track.channels} ({audio_track.bitrate_kbps} kbps)",
                )
            )
        else:
            audio_track_choices.append(
                (
                    str(audio_track.track),
                    f"{audio_track.language_name}: {audio_track.codec} {audio_track.channels}",
                )
            )
        default_audio_tracks.append(str(audio_track.track))

    for subtitle_track in subtitle_tracks:
        subtitle_track_choices.append(
            (
                str(subtitle_track.track),
                f"{subtitle_track.elements}-element {subtitle_track.language_name}",
            )
        )
        default_subtitle_tracks.append(str(subtitle_track.track))

    mkvmerge_form.audio_tracks.choices = audio_track_choices
    mkvmerge_form.audio_tracks.default = default_audio_tracks

    mkvmerge_form.subtitle_tracks.choices = subtitle_track_choices
    mkvmerge_form.subtitle_tracks.default = default_subtitle_tracks

    if mkvmerge_form.mkvmerge_submit.data and mkvmerge_form.validate_on_submit():
        if not file_exists_locally:
            flash(f"'{file.basename}' is not present locally.", "warning")
            return redirect(url_for("main.file", file_id=file.id))

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
        if not file_exists_locally:
            flash(f"'{file.basename}' is not present locally.", "warning")
            return redirect(url_for("main.file", file_id=file.id))

        # Enqueue a transcode task for this file

        current_app.transcode_queue.enqueue(
            "app.videos.transcode_task",
            args=(file.id,),
            job_timeout=current_app.config["TRANSCODE_TASK_TIMEOUT"],
            description=f"'{file.plex_title}'",
            job_id=safe_job_id(file.plex_title),
        )
        flash(f"Added '{file.plex_title}' to transcoding queue", "success")
        return redirect(url_for("main.file", file_id=file.id))

    # Form to request this file be uploaded to AWS S3 storage

    upload_form = S3UploadForm()
    if upload_form.s3_upload_submit.data and upload_form.validate_on_submit():
        if not file_exists_locally:
            flash(f"'{file.basename}' is not present locally.", "warning")
            return redirect(url_for("main.file", file_id=file.id))

        # Enqueue an upload task for this file

        current_app.file_queue.enqueue(
            "app.videos.upload_task",
            args=(
                file.id,
                current_app.config["AWS_UNTOUCHED_PREFIX"],
                True,
                True,
                "DEEP_ARCHIVE",
            ),
            job_timeout=current_app.config["UPLOAD_TASK_TIMEOUT"],
            description=f"'{file.basename}'",
            at_front=True,
        )
        flash(f"Uploading '{file.basename}' to AWS S3 storage", "info")
        return redirect(url_for("main.file", file_id=file.id))

    file_restore_estimate = restore_cost_estimate(
        [file] if file.aws_untouched_key else []
    )

    download_form = S3DownloadForm()
    if download_form.s3_download_submit.data and download_form.validate_on_submit():
        if not current_user.check_password(download_form.password.data):
            flash("Incorrect password provided!", "danger")
            return redirect(url_for("main.file", file_id=file.id))

        # Enqueue a restore task for this file

        current_app.request_queue.enqueue(
            "app.videos.aws_restore",
            args=(file.aws_untouched_key,),
            job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
            description=f"'{file.untouched_basename}'",
        )
        flash(
            f"Requesting '{file.untouched_basename}' to be restored from AWS "
            f"Glacier (≈ ${file_restore_estimate['cost']:.2f})",
            "info",
        )
        return redirect(url_for("main.file", file_id=file.id))

    # Form to delete and purge the file from the database

    delete_form = FileDeleteForm()
    if delete_form.delete_submit.data and delete_form.validate_on_submit():
        aws_untouched_key = file.aws_untouched_key

        try:
            file.delete_local_file(delete_directory_tree=True)
            db.session.delete(file)
            db.session.commit()

        except Exception:
            db.session.rollback()
            flash(f"Unable to delete '{file.basename}'!", "danger")
            return redirect(url_for("main.file", file_id=file.id))

        # Delete the AWS copy only after the database delete has committed, so a
        # failed commit can't leave a database record whose backup is gone

        if aws_untouched_key:
            current_app.request_queue.enqueue(
                "app.videos.aws_delete",
                args=(aws_untouched_key,),
                job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
            )

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
        file_restore_estimate=file_restore_estimate,
        delete_form=delete_form,
        best_file=best_file,
        file_exists_locally=file_exists_locally,
    )


@bp.route("/file/<int:file_id>/poster", methods=["GET", "POST"])
@login_required
def file_poster(file_id):
    """The poster picker's file-scoped twin: one file's custom poster.

    The TMDb gallery appears for movie files; TV files get the upload form
    only, since TMDb season/episode artwork isn't wired up.
    """

    file = File.query.filter_by(id=file_id).first_or_404()
    movie = file.movie

    # A custom poster is written next to the library file, so there must
    # be a library file to write next to

    file_exists_locally = os.path.isfile(
        os.path.join(current_app.config["LIBRARY_DIR"], file.file_path)
    )

    custom_poster_form = CustomPosterUploadForm()
    poster_select_form = TMDBPosterSelectForm()
    poster_remove_form = CustomPosterRemoveForm()

    if (
        poster_remove_form.poster_remove_submit.data
        and poster_remove_form.validate_on_submit()
    ):
        if not file.custom_poster:
            flash(f"'{file.basename}' has no custom poster to remove.", "warning")
            return redirect(url_for("main.file_poster", file_id=file.id))
        error = _remove_file_poster(file)
        if error:
            flash(error, "danger")
        else:
            flash(f"Removed the custom poster for '{file.basename}'", "success")
        return redirect(url_for("main.file_poster", file_id=file.id))

    if (
        custom_poster_form.poster_submit.data
        and custom_poster_form.validate_on_submit()
    ):
        if not file_exists_locally:
            flash(f"'{file.basename}' is not present locally.", "warning")
            return redirect(url_for("main.file_poster", file_id=file.id))
        error = _assign_file_poster(file, custom_poster_form.custom_poster.data)
        if error:
            flash(error, "danger")
            return redirect(url_for("main.file_poster", file_id=file.id))
        flash(f"Uploaded a custom poster for '{file.basename}'", "success")
        return redirect(url_for("main.file", file_id=file.id))

    if (
        poster_select_form.poster_select_submit.data
        and poster_select_form.validate_on_submit()
    ):
        if not file_exists_locally:
            flash(f"'{file.basename}' is not present locally.", "warning")
            return redirect(url_for("main.file_poster", file_id=file.id))
        uploaded_data, error = _fetch_tmdb_poster(poster_select_form.poster_path.data)
        if not error:
            error = _assign_file_poster(file, uploaded_data)
        if error:
            flash(error, "danger")
            return redirect(url_for("main.file_poster", file_id=file.id))
        flash(f"Set the poster for '{file.basename}' from TMDb", "success")
        return redirect(url_for("main.file", file_id=file.id))

    posters, languages, active_language = _poster_gallery_context(
        _tmdb_poster_gallery(movie.tmdb_id if movie else None)
    )

    return render_template(
        "poster_picker.html",
        title=f'Poster for "{file.basename}"',
        movie=movie,
        file=file,
        back_url=url_for("main.file", file_id=file.id),
        back_label=file.basename,
        posters=posters,
        languages=languages,
        active_language=active_language,
        language_url=lambda language: url_for(
            "main.file_poster", file_id=file.id, language=language
        ),
        custom_poster_form=custom_poster_form,
        poster_select_form=poster_select_form,
        poster_remove_form=poster_remove_form,
        has_custom_poster=bool(file.custom_poster),
        default_poster_path=movie.tmdb_poster_path if movie else None,
        upload_enabled=file_exists_locally,
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

    movie_review_form = MovieReviewForm()
    if movie_review_form.review_submit.data and movie_review_form.validate_on_submit():
        rating = (
            float(movie_review_form.rating.data)
            if movie_review_form.rating.data is not None
            else None
        )
        for field, value in star_rating_fields(rating).items():
            setattr(user_review, field, value)
        user_review.liked = movie_review_form.liked.data

        # The date-only form field can't improve on a stored timestamp
        # (e.g. a Plex watch's actual clock time), so only replace the
        # value when the calendar date itself changed

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

        new_text = movie_review_form.review.data or ""
        if new_text != (user_review.review or ""):
            user_review.review = new_text
            if user_review.date_reviewed:
                user_review.date_updated = datetime.now()
            else:
                user_review.date_reviewed = datetime.now()

        db.session.commit()
        flash(f"Updated your review of '{title}'", "success")
        return redirect(url_for("main.history"))

    if request.method == "GET":
        movie_review_form = MovieReviewForm(
            rating=user_review.rating,
            liked=user_review.liked,
            review=user_review.review,
            date_watched=(
                user_review.date_watched.date() if user_review.date_watched else None
            ),
        )

    return render_template(
        "review_edit.html",
        title=f'Edit review for "{title}"',
        movie=movie,
        user_review=user_review,
        movie_review_form=movie_review_form,
    )


@bp.route("/history", methods=["GET", "POST"])
@login_required
def history():
    """Display all of a user's viewings and reviews."""

    # Paginate a user's movie reviews, show 50 reviews per page

    page = request.args.get("page", 1, type=int)

    # Chronological by watch date, newest first — unreviewed viewings (Plex
    # watches) sort by recency like everything else. DESC puts NULL watch
    # dates last in both MySQL and SQLite, so dateless rating-only entries
    # trail the dated history rather than burying it.

    reviews = (
        UserMovieReview.query.join(Movie, (Movie.id == UserMovieReview.movie_id))
        .filter(UserMovieReview.user_id == int(current_user.id))
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

        export_query = UserMovieReview.query.join(
            Movie, (Movie.id == UserMovieReview.movie_id)
        ).filter(UserMovieReview.user_id == int(current_user.id))

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
            attachments=[(filename, "text/csv", f.getvalue())],
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

    return render_template(
        "history.html",
        title="My History",
        review_export_form=review_export_form,
        review_upload_form=review_upload_form,
        reviews=reviews.items,
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
        streaming_form=streaming_form,
        provider_logos={p["provider_id"]: p["logo_path"] for p in picker},
    )


@bp.route("/system", methods=["GET", "POST"])
@login_required
@admin_required
def system():
    """System status: health, worker and scheduler state, and failed jobs."""

    queues_by_name = {
        queue.name: queue
        for queue in (
            current_app.import_queue,
            current_app.sql_queue,
            current_app.request_queue,
            current_app.transcode_queue,
            current_app.file_queue,
            current_app.maintenance_queue,
        )
    }

    # Form to requeue or forget a failed background job

    failed_job_form = FailedJobForm()
    if (
        failed_job_form.requeue_submit.data or failed_job_form.forget_submit.data
    ) and failed_job_form.validate_on_submit():
        queue = queues_by_name.get(failed_job_form.failed_queue.data)
        job_id = failed_job_form.failed_job_id.data
        registry = FailedJobRegistry(queue=queue) if queue else None

        if registry and job_id in registry.get_job_ids():
            if failed_job_form.requeue_submit.data:
                registry.requeue(job_id)
                flash(f"Requeued '{job_id}'", "info")
            else:
                try:
                    job = Job.fetch(job_id, connection=current_app.redis)
                    registry.remove(job, delete_job=True)
                except NoSuchJobError:
                    registry.connection.zrem(registry.key, job_id)
                flash(f"Removed failed job '{job_id}'", "info")
        else:
            flash("That failed job no longer exists.", "warning")

        return redirect(url_for("main.system"))

    failed_jobs = []
    for queue_name, queue in queues_by_name.items():
        registry = FailedJobRegistry(queue=queue)
        for job_id in registry.get_job_ids():
            job = queue.fetch_job(job_id)
            if job is None:
                continue
            exc_lines = (job.exc_info or "").strip().splitlines()
            failed_jobs.append(
                {
                    "id": job_id,
                    "queue": queue_name,
                    "description": job.description or job.func_name,
                    "failed_at": job.ended_at,
                    "error": exc_lines[-1][:200] if exc_lines else "",
                }
            )
    # rq 2 job timestamps are timezone-aware, so the missing-date fallback
    # must be aware too or the sort can't compare them

    failed_jobs.sort(
        key=lambda job: job["failed_at"] or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )

    return render_template(
        "system.html",
        title="System",
        health=system_health(current_app),
        scheduled_tasks=_scheduled_tasks(),
        relative_time=_relative_time,
        failed_jobs=failed_jobs,
        failed_job_form=failed_job_form,
    )


def _scheduled_tasks():
    """Status of the recurring scheduled tasks, for the polled fragment.

    The schedulers share one scheduled-jobs set, so each scheduler's
    results are filtered to its own queue.
    """

    cron_descriptions = {
        "0 0 * * *": "Daily at midnight",
        "30 0 * * *": "Daily at 12:30 AM",
        "0 1 * * 0": "Weekly on Sunday at 1:00 AM",
        "0 3 18 * *": "Monthly on the 18th at 3:00 AM",
        "0 4 1 * *": "Monthly on the 1st at 4:00 AM",
        "0 * * * *": "Hourly",
        "30 * * * *": "Hourly at :30",
        "*/10 * * * *": "Every 10 minutes",
        "*/15 * * * *": "Every 15 minutes",
        "* * * * *": "Every minute",
    }
    scheduled_tasks = []
    for scheduler in (current_app.maintenance_scheduler,):
        for job, next_run in scheduler.get_jobs(with_times=True):
            if job.origin != scheduler.queue_name:
                continue
            cron_string = job.meta.get("cron_string", "")
            scheduled_tasks.append(
                {
                    "name": job.description or job.id,
                    "schedule": cron_descriptions.get(cron_string, cron_string),
                    "last_run": job.ended_at,
                    "next_run_text": _next_run_text(next_run),
                }
            )
    return scheduled_tasks


def _next_run_text(next_run):
    """Render a task's next-run time without ever calling it the past.

    A due job's stored time sits in the past until the scheduler's next
    tick (60s interval) moves it onto the queue and re-computes the
    following run, so the 5s poll routinely catches slightly-past values:
    those are "due now". Older than a couple of ticks means the scheduler
    has actually stalled, which the health card's badge also shows.
    """

    if next_run.tzinfo is None:
        next_run = next_run.replace(tzinfo=timezone.utc)
    lateness = (datetime.now(timezone.utc) - next_run).total_seconds()
    if lateness <= 0:
        return _relative_time(next_run)
    if lateness <= 120:
        return "due now"
    return "overdue"


def _relative_time(moment_dt):
    """Coarse relative-time text: '4 minutes ago', or 'in 4 minutes' for
    future times like a task's next run.

    The health fragment is re-rendered by every poll, so server-side text
    stays current without flask-moment — whose scripts wouldn't re-run
    inside swapped-in HTML anyway.
    """

    if moment_dt.tzinfo is None:
        moment_dt = moment_dt.replace(tzinfo=timezone.utc)
    # round(), not int(): truncation toward zero would undercount future
    # spans ("in 3 days" minus a microsecond is still 3 days, not 2)
    seconds = round((datetime.now(timezone.utc) - moment_dt).total_seconds())
    future = seconds < 0
    seconds = abs(seconds)
    minutes = seconds // 60
    hours = minutes // 60
    if seconds < 60:
        text = "under a minute"
    elif minutes < 60:
        text = f"{minutes} minute{'s' if minutes != 1 else ''}"
    elif hours < 24:
        text = f"{hours} hour{'s' if hours != 1 else ''}"
    else:
        days = hours // 24
        text = f"{days} day{'s' if days != 1 else ''}"
    return f"in {text}" if future else f"{text} ago"


@bp.route("/system/metrics")
@login_required
@admin_required
def system_metrics():
    """The live health fragment the System page's poller swaps in.

    Everything rendered here reads Redis or the local filesystem; the
    external-service badges come from the health_probe task's snapshot, so
    polling generates no external traffic.
    """

    fragment = render_template(
        "_system_health.html",
        health=system_health(current_app),
        scheduled_tasks=_scheduled_tasks(),
        relative_time=_relative_time,
    )
    response = make_response(fragment)
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.route("/maintenance", methods=["GET", "POST"])
@login_required
@admin_required
def maintenance():
    """Library maintenance: rejected-file triage, duplicate movies, the
    filename tester, and the library-wide bulk operations."""

    # Form to update the Criterion Collection information for the entire movie library

    criterion_refresh_form = CriterionRefreshForm()
    if (
        criterion_refresh_form.criterion_refresh.data
        and criterion_refresh_form.validate_on_submit()
    ):
        # On the user-request queue, like the monthly scheduled refresh runs
        # on maintenance: the forced Wikidata fetch would otherwise block
        # the single sql worker on network I/O

        current_app.request_queue.enqueue(
            "app.videos.refresh_criterion_collection_info",
            args=None,
            job_timeout="1h",
            description="Refreshing Criterion Collection information for all movies in library",
            at_front=True,
        )
        flash(
            "Refreshing Criterion Collection information for all movies in library",
            "info",
        )
        return redirect(url_for("main.maintenance"))

    # Form to update the TMDb data for the entire library, both movies and TV shows

    tmdb_refresh_form = TMDBRefreshForm()
    if tmdb_refresh_form.tmdb_refresh.data and tmdb_refresh_form.validate_on_submit():
        movies = Movie.query.order_by(Movie.title.asc(), Movie.year.asc()).all()
        tv_shows = TVSeries.query.order_by(TVSeries.title.asc()).all()

        # On the user-request queue: each job is a TMDb API call plus
        # artwork downloads, and thousands of them would starve the single
        # sql worker of import work for the whole run

        for movie in movies:
            current_app.request_queue.enqueue(
                "app.videos.refresh_tmdb_info",
                args=("Movies", movie.id, movie.tmdb_id),
                job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
                description=f"Refreshing TMDB data for '{movie.title} ({movie.year})'",
            )

        for tv in tv_shows:
            current_app.request_queue.enqueue(
                "app.videos.refresh_tmdb_info",
                args=("TV Shows", tv.id, tv.tmdb_id),
                job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
                description=f"Refreshing TMDB data for '{tv.title}'",
            )

        flash("Refreshing TMDb information for entire library", "info")
        return redirect(url_for("main.maintenance"))

    sync_form = SyncAWSStorageForm()
    if sync_form.sync_submit.data and sync_form.validate_on_submit():
        if not current_user.admin:
            flash("Need to be an admin user for this task!", "danger")

        elif current_user.check_password(sync_form.password.data):
            current_app.sql_queue.enqueue(
                "app.videos.sync_aws_s3_storage_task",
                args=None,
                job_timeout="24h",
                description="Syncing files from AWS S3 storage",
                at_front=True,
            )
            flash("Syncing files with AWS S3 storage", "info")

        else:
            flash("Incorrect password provided!", "danger")

        return redirect(url_for("main.maintenance"))

    # Form to rescan metadata for all the files

    metadata_scan_form = TrackMetadataScanForm()

    if metadata_scan_form.scan_submit.data and metadata_scan_form.validate_on_submit():
        current_app.sql_queue.enqueue(
            "app.videos.track_metadata_scan_library",
            args=(),
            job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
            description="Scanning track metadata for all files in the library",
        )
        flash("Scanning track metadata for all files in the library", "info")
        return redirect(url_for("main.maintenance"))

    import_form = ImportForm()
    if import_form.submit.data and import_form.validate_on_submit():
        enqueue_import_scan(
            current_app.request_queue,
            description="Manually scanning import directory for files",
            at_front=True,
        )
        current_app.logger.info("Manually scanning import directory for files")
        flash("Manually scanning import directory for files", "info")
        return redirect(url_for("main.maintenance"))

    # Form to merge a group of movies that share a TMDb id: each duplicate
    # is fed through refresh_tmdb_info, whose merge path (serialized with
    # the import pipeline by title locks) moves files and reviews to the
    # oldest record and deletes the duplicate

    movie_merge_form = MovieMergeForm()
    if movie_merge_form.merge_submit.data and movie_merge_form.validate_on_submit():
        merge_tmdb_id = int(movie_merge_form.merge_tmdb_id.data)
        group = (
            Movie.query.filter_by(tmdb_id=merge_tmdb_id)
            .order_by(Movie.date_created.asc())
            .all()
        )
        if len(group) < 2:
            flash("No duplicates found for that TMDb id.", "danger")

        else:
            canonical = group[0]
            for duplicate in group[1:]:
                current_app.request_queue.enqueue(
                    "app.videos.refresh_tmdb_info",
                    args=("Movies", duplicate.id, merge_tmdb_id),
                    job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
                    description=(
                        f"Merging '{duplicate.title} ({duplicate.year})' into "
                        f"'{canonical.title} ({canonical.year})'"
                    ),
                )
            flash(
                f"Merging {len(group) - 1} duplicate(s) into "
                f"'{canonical.title} ({canonical.year})'",
                "info",
            )
        return redirect(url_for("main.maintenance"))

    # Form to preview how a filename would be parsed and filed on import

    filename_test_form = FilenameTestForm()
    filename_test_result = None
    if (
        filename_test_form.filename_test_submit.data
        and filename_test_form.validate_on_submit()
    ):
        test_filename = filename_test_form.test_filename.data.strip()
        filename_test_result = {
            "filename": test_filename,
            "details": evaluate_filename(test_filename, log=False),
        }

    return render_template(
        "maintenance.html",
        title="Library Maintenance",
        rejected_count=len(_rejected_files()),
        subtitle_triage_count=len(_forced_subtitle_candidates()),
        duplicate_groups=_duplicate_movie_groups(),
        movie_merge_form=movie_merge_form,
        filename_test_form=filename_test_form,
        filename_test_result=filename_test_result,
        criterion_refresh_form=criterion_refresh_form,
        tmdb_refresh_form=tmdb_refresh_form,
        sync_form=sync_form,
        metadata_scan_form=metadata_scan_form,
        import_form=import_form,
    )


def _forced_subtitle_candidates():
    """Subtitle tracks that look forced but aren't flagged, grouped by file.

    A track is suspicious when it's unforced and holds a quarter or less
    of the elements of the largest same-language track in the same file —
    the shape of a foreign-parts-only track sitting beside the full
    subtitles. Only meaningful comparisons count (the full track needs at
    least 100 elements, the candidate can't be empty). Files marked
    reviewed are excluded, as are files that already carry a forced track
    — their forced needs are met, so a small unforced sibling is probably
    a commentary or variant.
    """

    ForcedSibling = db.aliased(FileSubtitleTrack)
    sibling_max = (
        db.session.query(
            FileSubtitleTrack.file_id.label("sibling_file_id"),
            FileSubtitleTrack.language.label("sibling_language"),
            db.func.max(FileSubtitleTrack.elements).label("max_elements"),
        )
        .group_by(FileSubtitleTrack.file_id, FileSubtitleTrack.language)
        .subquery()
    )
    rows = (
        db.session.query(FileSubtitleTrack, File, sibling_max.c.max_elements)
        .join(
            sibling_max,
            db.and_(
                FileSubtitleTrack.file_id == sibling_max.c.sibling_file_id,
                FileSubtitleTrack.language == sibling_max.c.sibling_language,
            ),
        )
        .join(File, File.id == FileSubtitleTrack.file_id)
        .filter(
            db.or_(
                FileSubtitleTrack.forced == False,
                FileSubtitleTrack.forced.is_(None),
            ),
            ~db.session.query(ForcedSibling.id)
            .filter(
                ForcedSibling.file_id == FileSubtitleTrack.file_id,
                ForcedSibling.forced == True,
            )
            .exists(),
            File.subtitle_triage_reviewed.is_(None),
            sibling_max.c.max_elements >= 100,
            FileSubtitleTrack.elements > 0,
            FileSubtitleTrack.elements * 4 <= sibling_max.c.max_elements,
        )
        .order_by(File.plex_title.asc(), FileSubtitleTrack.track.asc())
        .all()
    )

    grouped = {}
    for track, file, max_elements in rows:
        entry = grouped.setdefault(file.id, {"file": file, "tracks": []})
        entry["tracks"].append({"track": track, "max_elements": max_elements})
    return list(grouped.values())


@bp.route("/maintenance/subtitles", methods=["GET", "POST"])
@login_required
@admin_required
def subtitle_triage():
    """Triage subtitle tracks that look forced but aren't flagged.

    Marking a track forced reuses the file page's mkvpropedit pipeline
    with the file's current defaults preserved; dismissing marks the whole
    file's subtitles as reviewed so it stops being flagged.
    """

    triage_form = SubtitleTriageForm()

    if triage_form.mark_forced_submit.data and triage_form.validate_on_submit():
        track = FileSubtitleTrack.query.filter_by(
            id=triage_form.track_id.data
        ).first_or_404()
        file = db.session.get(File, track.file_id)

        if file.container != "Matroska":
            flash(
                f"'{file.basename}' isn't an MKV file, so its subtitle flags "
                f"can't be edited in place.",
                "danger",
            )
            return redirect(url_for("main.subtitle_triage"))
        if not os.path.isfile(
            os.path.join(current_app.config["LIBRARY_DIR"], file.file_path)
        ):
            flash(f"'{file.basename}' is not present locally.", "warning")
            return redirect(url_for("main.subtitle_triage"))

        # Preserve the file's current selections, adding this track to
        # the forced set — the same arguments the file page's form sends

        audio_default = FileAudioTrack.query.filter_by(
            file_id=file.id, default=True
        ).first()
        default_audio_track = str(audio_default.track) if audio_default else None
        subtitle_default = FileSubtitleTrack.query.filter_by(
            file_id=file.id, default=True
        ).first()
        default_subtitle_track = (
            str(subtitle_default.track) if subtitle_default else None
        )
        forced_tracks = sorted(
            {
                str(existing.track)
                for existing in FileSubtitleTrack.query.filter_by(
                    file_id=file.id, forced=True
                )
            }
            | {str(track.track)},
            key=int,
        )

        current_app.file_queue.enqueue(
            "app.videos.mkvpropedit_task",
            args=(
                file.id,
                default_audio_track,
                default_subtitle_track,
                forced_tracks,
            ),
            job_timeout=current_app.config["MKVPROPEDIT_TASK_TIMEOUT"],
            description=f"'{file.basename}'",
        )
        flash(
            f"Marking track {track.track} of '{file.basename}' as forced",
            "info",
        )
        return redirect(url_for("main.subtitle_triage"))

    if triage_form.dismiss_submit.data and triage_form.validate_on_submit():
        file = File.query.filter_by(id=triage_form.file_id.data).first_or_404()
        file.subtitle_triage_reviewed = datetime.now()
        db.session.commit()
        flash(f"Marked '{file.basename}' subtitles as reviewed", "success")
        return redirect(url_for("main.subtitle_triage"))

    return render_template(
        "subtitle_triage.html",
        title="Possibly-forced subtitles",
        candidates=_forced_subtitle_candidates(),
        triage_form=triage_form,
    )


def _duplicate_movie_groups():
    """Movies sharing a TMDb id, each group oldest-first.

    The oldest record is the one refresh_tmdb_info keeps when merging, so
    the first movie in each group is the survivor.
    """

    duplicated_ids = [
        tmdb_id
        for (tmdb_id,) in db.session.query(Movie.tmdb_id)
        .filter(Movie.tmdb_id != None)
        .group_by(Movie.tmdb_id)
        .having(db.func.count(Movie.id) > 1)
        .all()
    ]
    if not duplicated_ids:
        return []

    groups = {}
    for movie in (
        Movie.query.filter(Movie.tmdb_id.in_(duplicated_ids))
        .order_by(Movie.tmdb_id.asc(), Movie.date_created.asc())
        .all()
    ):
        groups.setdefault(movie.tmdb_id, []).append(movie)
    return list(groups.values())


def _rejected_files():
    """Every real file under the rejects directory, newest first."""

    rejects_dir = os.path.realpath(current_app.config["REJECTS_DIR"])
    entries = []
    for dirpath, dirnames, filenames in os.walk(rejects_dir):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name.startswith("."):
                continue
            full_path = os.path.join(dirpath, name)
            try:
                stats = os.stat(full_path)
            except OSError:
                continue
            relative_path = os.path.relpath(full_path, rejects_dir)
            entries.append(
                {
                    "path": relative_path,
                    "basename": name,
                    "reason": os.path.dirname(relative_path) or "unknown",
                    "size": stats.st_size,
                    "rejected_at": datetime.fromtimestamp(stats.st_mtime, timezone.utc),
                }
            )
    entries.sort(key=lambda entry: entry["rejected_at"], reverse=True)
    return entries


@bp.route("/rejects", methods=["GET", "POST"])
@login_required
@admin_required
def rejects():
    """Triage rejected files: send them back for re-import, or delete them.

    Re-importing is just a move into the import directory — the filesystem
    watcher and the hourly sweep take it from there.
    """

    rejects_dir = os.path.realpath(current_app.config["REJECTS_DIR"])
    form = RejectActionForm()

    if form.validate_on_submit():
        # The posted path must resolve to a real file inside the rejects
        # directory: no traversal, no symlink escapes

        requested = os.path.realpath(os.path.join(rejects_dir, form.file_path.data))
        if not requested.startswith(rejects_dir + os.sep) or not os.path.isfile(
            requested
        ):
            flash("That file no longer exists in the rejects directory.", "danger")
            return redirect(url_for("main.rejects"))

        basename = os.path.basename(requested)

        if form.delete_submit.data:
            os.remove(requested)
            current_app.logger.info(f"'{basename}' Deleted from the rejects directory")
            flash(f"Deleted '{basename}'.", "success")

        else:
            destination = os.path.join(current_app.config["IMPORT_DIR"], basename)
            if os.path.exists(destination):
                flash(
                    f"'{basename}' already exists in the import directory; "
                    f"not overwriting it.",
                    "danger",
                )
                return redirect(url_for("main.rejects"))
            shutil.move(requested, destination)
            current_app.logger.info(
                f"'{basename}' Moved from rejects to the import directory"
            )
            flash(f"Moved '{basename}' to the import directory.", "success")

        # Tidy the reason folder if this was its last file (never the
        # rejects directory itself)

        reason_dir = os.path.dirname(requested)
        if reason_dir != rejects_dir:
            try:
                os.rmdir(reason_dir)
            except OSError:
                pass

        return redirect(url_for("main.rejects"))

    return render_template(
        "rejects.html",
        title="Rejected files",
        rejected=_rejected_files(),
        form=form,
    )


@bp.route("/about")
def about():
    """Show general information about the Fitzflix application."""

    return render_template("about.html")


def _upgrade_threshold():
    """The quality preference below which a copy counts as upgradable."""

    return (
        db.session.query(RefQuality.preference)
        .filter(RefQuality.quality_title == "Bluray-1080p")
        .scalar()
        or 0
    )


def _movie_search_results(wildcard, limit=50):
    """Movies whose titles match, each with its best owned copy.

    Only films with a local main-feature file appear: review-only
    records (a diary entry for an unowned film) belong to the TMDb
    search, not the library search."""

    upgrade_threshold = _upgrade_threshold()

    results = []
    movies = (
        Movie.query.filter(
            db.or_(
                Movie.title.ilike(f"%{wildcard}%"),
                Movie.tmdb_title.ilike(f"%{wildcard}%"),
            )
        )
        .filter(Movie.files.any(File.feature_type_id.is_(None)))
        .order_by(Movie.title.asc(), Movie.year.asc())
        .limit(limit)
        .all()
    )
    for movie in movies:
        best = (
            movie.files.filter(File.feature_type_id == None)
            .join(RefQuality, (RefQuality.id == File.quality_id))
            .order_by(File.fullscreen.asc(), RefQuality.preference.desc())
            .first()
        )
        review = movie.ratings.first()
        results.append(
            {
                "movie": movie,
                "best_file": best,
                "quality": best.quality.quality_title if best else None,
                "upgradable": bool(
                    best
                    and (best.fullscreen or best.quality.preference < upgrade_threshold)
                    and not (movie.shopping_list_exclude == 1)
                ),
                "rating": review.rating if review else None,
                "excluded": movie.shopping_list_exclude == 1,
            }
        )
    return results


def _tv_search_results(wildcard, limit=50):
    """TV series whose titles match, each season summarized by the worst
    quality among its best (rank-1) episode files.

    TV shows are usually bought season by season, so a series-wide "best
    quality" would hide the seasons that need upgrading: what matters in a
    store is each season's weakest link.
    """

    series_list = (
        TVSeries.query.filter(
            db.or_(
                TVSeries.title.ilike(f"%{wildcard}%"),
                TVSeries.tmdb_name.ilike(f"%{wildcard}%"),
            )
        )
        .order_by(TVSeries.title.asc())
        .limit(limit)
        .all()
    )
    if not series_list:
        return []

    upgrade_threshold = _upgrade_threshold()
    series_ids = [series.id for series in series_list]

    # Same shape as the TV library page: rank each episode's copies, keep
    # the best copy per episode, then take each season's worst best-copy

    ranked_files = (
        db.session.query(
            File.id,
            tv_file_rank(),
        )
        .join(TVSeries, (TVSeries.id == File.series_id))
        .join(RefQuality, (RefQuality.id == File.quality_id))
        .subquery()
    )

    season_aggregate = (
        db.session.query(
            File.series_id,
            File.season,
            db.func.count(db.func.distinct(File.episode)).label("episodes"),
            db.func.min(RefQuality.preference).label("preference"),
        )
        .group_by(File.series_id, File.season)
        .join(RefQuality, (RefQuality.id == File.quality_id))
        .join(ranked_files, (ranked_files.c.id == File.id))
        .filter(ranked_files.c.rank == 1)
        .filter(File.series_id.in_(series_ids))
        .subquery()
    )

    season_rows = (
        db.session.query(
            season_aggregate.c.series_id,
            season_aggregate.c.season,
            season_aggregate.c.episodes,
            season_aggregate.c.preference,
            RefQuality.physical_media,
            RefQuality.quality_title,
        )
        .join(RefQuality, (RefQuality.preference == season_aggregate.c.preference))
        .order_by(
            season_aggregate.c.series_id,
            db.case((season_aggregate.c.season == 0, 1), else_=0).asc(),
            season_aggregate.c.season.asc(),
        )
        .all()
    )

    seasons_by_series = {}
    for series_id, season, episodes, preference, physical, worst_quality in season_rows:
        seasons_by_series.setdefault(series_id, []).append(
            {
                "season": season,
                "episode_count": episodes,
                "worst_quality": worst_quality,
                "preference": preference,
                # Physical-media seasons (DVD, SD/720p Blu-ray) are often the
                # only release that will ever exist, so they don't count as
                # upgradable
                "upgradable": not physical and preference < upgrade_threshold,
            }
        )

    return [
        {
            "series": series,
            "file_count": series.files.count(),
            "seasons": seasons_by_series.get(series.id, []),
        }
        for series in series_list
    ]


def _people_search_results(wildcard, limit=12):
    """Credited people whose names match, with their library film counts.

    Mirrors the People page's rules: uncredited-only roles never count,
    and matches order by film count with the surname tie-break.
    """

    film_count = db.func.count(db.distinct(MovieCast.movie_id)).label("film_count")
    return (
        db.session.query(
            TMDBCredit.id,
            TMDBCredit.name,
            TMDBCredit.tmdb_profile_path,
            film_count,
        )
        .join(MovieCast, MovieCast.credit_id == TMDBCredit.id)
        .filter(
            db.or_(
                MovieCast.character == None,
                db.not_(MovieCast.character.like("%(uncredited)%")),
            )
        )
        .filter(TMDBCredit.name.ilike(f"%{wildcard}%"))
        .group_by(TMDBCredit.id, TMDBCredit.name, TMDBCredit.tmdb_profile_path)
        .order_by(
            film_count.desc(),
            db.func.substring_index(TMDBCredit.name, " ", -1).asc(),
            TMDBCredit.name.asc(),
        )
        .limit(limit)
        .all()
    )


@bp.route("/search")
@login_required
def search():
    """Search movies and TV series from one box, anywhere in the app."""

    q = (request.args.get("q") or "").strip()
    movie_results = []
    tv_results = []
    people_results = []

    if q:
        # Spaces become wildcards so word order and punctuation don't matter

        wildcard = q.replace(" ", "%")
        movie_results = _movie_search_results(wildcard)
        tv_results = _tv_search_results(wildcard)
        people_results = _people_search_results(wildcard)

    return render_template(
        "search.html",
        title=f"Search results for '{q}'" if q else "Search",
        q=q,
        movie_results=movie_results,
        tv_results=tv_results,
        people_results=people_results,
    )


@bp.route("/search.json")
@login_required
def search_json():
    """Type-ahead suggestions for the global search box."""

    q = (request.args.get("q") or "").strip()
    results = []

    if len(q) >= 2:
        wildcard = q.replace(" ", "%")

        for result in _movie_search_results(wildcard, limit=5):
            movie = result["movie"]
            display_title = movie.tmdb_title if movie.tmdb_title else movie.title
            display_year = (
                movie.tmdb_release_date.year
                if movie.tmdb_title and movie.tmdb_release_date
                else movie.year
            )
            results.append(
                {
                    "type": "Movie",
                    "title": f"{display_title} ({display_year})",
                    "detail": result["quality"],
                    "url": url_for("main.movie", movie_id=movie.id),
                }
            )

        for result in _tv_search_results(wildcard, limit=5):
            series = result["series"]
            seasons = result["seasons"]
            if seasons:
                worst = min(seasons, key=lambda season: season["preference"])
                detail = (
                    f"{len(seasons)} season{'s' if len(seasons) != 1 else ''}, "
                    f"worst {worst['worst_quality']}"
                )
            else:
                detail = "No copy in library"
            results.append(
                {
                    "type": "TV",
                    "title": (series.tmdb_name if series.tmdb_name else series.title),
                    "detail": detail,
                    "url": url_for("main.tv", series_id=series.id),
                }
            )

        for person in _people_search_results(wildcard, limit=5):
            results.append(
                {
                    "type": "Person",
                    "title": person.name,
                    "detail": (
                        f"{person.film_count} film"
                        f"{'s' if person.film_count != 1 else ''}"
                    ),
                    "url": url_for("main.movie_library", credit=person.id),
                }
            )

    return jsonify({"results": results})


@bp.route("/search/tmdb")
@login_required
def search_tmdb():
    """Look a title up on TMDb, to confirm what exists beyond the library."""

    q = (request.args.get("q") or "").strip()
    movie_matches = []
    tv_matches = []
    error = None
    streaming_attribution = False

    if q and not current_app.config["TMDB_API_KEY"]:
        error = "TMDB_API_KEY is not configured, so TMDb can't be searched."

    elif q:
        params = {"api_key": current_app.config["TMDB_API_KEY"], "query": q}
        try:
            for url, bucket, title_key, date_key in (
                ("/search/movie", movie_matches, "title", "release_date"),
                ("/search/tv", tv_matches, "name", "first_air_date"),
            ):
                r = tmdb_get(
                    current_app.config["TMDB_API_URL"] + url,
                    params=params,
                    timeout=10,
                )
                r.raise_for_status()
                for result in (r.json().get("results") or [])[:10]:
                    bucket.append(
                        {
                            "tmdb_id": result.get("id"),
                            "title": result.get(title_key),
                            "year": (result.get(date_key) or "")[:4],
                            "overview": result.get("overview"),
                            "poster_path": result.get("poster_path"),
                            "library_id": None,
                        }
                    )

        except Exception:
            current_app.logger.warning(traceback.format_exc())
            error = "TMDb could not be reached; try again in a moment."

        # Annotate which results are already in the library, by TMDb id.
        # "In library" means a local main-feature file exists — a
        # review-only record (a logged unowned film) doesn't count

        if movie_matches:
            owned = dict(
                db.session.query(Movie.tmdb_id, Movie.id)
                .filter(Movie.tmdb_id.in_([m["tmdb_id"] for m in movie_matches]))
                .filter(Movie.files.any(File.feature_type_id.is_(None)))
                .all()
            )
            for match in movie_matches:
                match["library_id"] = owned.get(match["tmdb_id"])

        if tv_matches:
            owned = dict(
                db.session.query(TVSeries.tmdb_id, TVSeries.id)
                .filter(TVSeries.tmdb_id.in_([m["tmdb_id"] for m in tv_matches]))
                .all()
            )
            for match in tv_matches:
                match["library_id"] = owned.get(match["tmdb_id"])

        # Streaming and rent/buy badges on unowned movie matches, both
        # filtered to this user's services (lookups are day-cached per
        # title); the flag turns on the mandatory JustWatch credit

        provider_ids = user_provider_ids(current_user)
        if provider_ids:
            for match in movie_matches:
                if match["library_id"] is not None or match["tmdb_id"] is None:
                    continue
                availability = title_availability(match["tmdb_id"])
                matches = streaming_matches(availability, provider_ids)
                rentals = rental_matches(availability, provider_ids)
                if matches:
                    match["streaming"] = matches
                if rentals:
                    match["rentals"] = rentals
                if matches or rentals:
                    streaming_attribution = True

    return render_template(
        "search_tmdb.html",
        title=f"TMDb results for '{q}'" if q else "TMDb search",
        q=q,
        movie_matches=movie_matches,
        tv_matches=tv_matches,
        error=error,
        streaming_attribution=streaming_attribution,
    )


@bp.route("/review/tmdb/<int:tmdb_id>", methods=["GET", "POST"])
@login_required
def review_tmdb(tmdb_id):
    """Review a film that isn't in the library, looked up on TMDb.

    Reviewing creates a review-only movie record — enriched afterwards
    through the standard TMDb refresh pipeline — so the film shows up in
    search and filmographies like any other seen-but-unowned title. Films
    already in the library redirect to their movie page, which has the
    same review form.
    """

    movie = Movie.query.filter_by(tmdb_id=tmdb_id).first()
    if movie:
        return redirect(url_for("main.movie", movie_id=movie.id))

    if not current_app.config["TMDB_API_KEY"]:
        flash("TMDB_API_KEY is not configured, so TMDb can't be queried.", "warning")
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
        flash("TMDb could not be reached; try again in a moment.", "warning")
        return redirect(url_for("main.history"))

    details = r.json()

    # Runtime, genres, US certification, and top billing, mirroring what
    # the movie page shows for library films

    genres = [g.get("name") for g in details.get("genres") or [] if g.get("name")]
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

    # The filmography page serves any TMDb person id, so every cast member
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
            "TMDb has no title or release year for that film yet, so it "
            "can't be reviewed.",
            "warning",
        )
        return redirect(url_for("main.history"))
    year = int(release_year)

    movie_review_form = MovieReviewForm(date_watched=datetime.now())
    if movie_review_form.review_submit.data and movie_review_form.validate_on_submit():
        # The movie may have appeared since the redirect check above (an
        # import or concurrent review), and the canonical name may collide
        # with an existing record — reuse rather than violating the unique
        # title + year constraint

        movie = Movie.query.filter_by(tmdb_id=tmdb_id).first()
        if movie is None:
            movie = Movie.query.filter_by(title=film_title, year=year).first()
            if movie is not None and movie.tmdb_id is None:
                movie.tmdb_id = tmdb_id
        created = movie is None
        if created:
            movie = Movie(title=film_title, year=year, tmdb_id=tmdb_id)
            db.session.add(movie)
            db.session.flush()

        rating = (
            float(movie_review_form.rating.data)
            if movie_review_form.rating.data is not None
            else None
        )
        # A bare submission (no rating, like, or text) is a plain diary
        # entry — a watch, not a review — so it carries no review date.
        # Rewatch is computed the way Plex watches compute it: any earlier
        # row for this user and film makes this a repeat viewing.

        is_review = bool(
            rating is not None
            or movie_review_form.liked.data
            or (movie_review_form.review.data or "").strip()
        )
        rewatch = (
            db.session.query(UserMovieReview.id)
            .filter_by(user_id=current_user.id, movie_id=movie.id)
            .first()
            is not None
        )
        review = UserMovieReview(
            user_id=current_user.id,
            movie_id=movie.id,
            review=movie_review_form.review.data,
            liked=movie_review_form.liked.data,
            date_watched=_watched_timestamp(movie_review_form.date_watched.data),
            date_reviewed=datetime.now() if is_review else None,
            rewatch=rewatch,
            **star_rating_fields(rating),
        )
        db.session.add(review)
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
        radarr_proxy_url=current_app.config["RADARR_PROXY_URL"],
    )


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

    # The page heading is derived from the active filters (below, once the
    # quality bounds are normalized) rather than read from a ?title= query
    # parameter — the old approach let any crafted URL put arbitrary text
    # in the heading

    # Form to filter the shopping list by Criterion release or quality

    filter_form = MovieShoppingFilterForm()
    if library == "criterion":
        criterion_release = True
        filter_form.filter_status.default = "criterion"

    else:
        criterion_release = None
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

    # If the min_quality ID doesn't exist in our RefQuality table, default
    # to "Not in library" — the virtual bottom of the scale, so the default view
    # includes liked-but-unowned films

    if not RefQuality.query.filter_by(id=int(min_quality)).first():
        min_quality = int(
            db.session.query(RefQuality.id)
            .filter(RefQuality.quality_title == "Not in library")
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

    # Find the preference associated with the quality ID, and set as the dropdown default

    min_preference = (
        db.session.query(RefQuality.preference).filter_by(id=int(min_quality)).scalar()
    )
    max_preference = (
        db.session.query(RefQuality.preference).filter_by(id=int(max_quality)).scalar()
    )

    # If the minimum quality outranks the maximum, collapse the range to
    # just the minimum. Compared by preference — quality ids don't
    # reliably follow quality order

    if min_preference > max_preference:
        max_quality = int(min_quality)
        max_preference = min_preference

    filter_form.min_quality.default = min_quality
    filter_form.max_quality.default = max_quality

    # Derive the heading from the filter state; the search branches below
    # override it with their own more specific titles

    if library == "criterion":
        title = "Criterion Collection movies to upgrade"
    elif media == "digital":
        title = "Digital downloads to get as physical media"
    else:
        title = "Movies to upgrade"

    not_in_library_quality = bottom_quality = (
        db.session.query(RefQuality.preference)
        .filter(RefQuality.quality_title == "Not in library")
        .scalar()
    )
    top_quality = (
        db.session.query(RefQuality.preference)
        .filter(RefQuality.quality_title == "Bluray-2160p Remux")
        .scalar()
    )
    min_quality_title = (
        db.session.query(RefQuality.quality_title)
        .filter_by(id=int(min_quality))
        .scalar()
    )
    max_quality_title = (
        db.session.query(RefQuality.quality_title)
        .filter_by(id=int(max_quality))
        .scalar()
    )
    if min_quality_title == max_quality_title:
        # Equal titles mean equal preferences, so testing one bound suffices
        if min_preference == not_in_library_quality:
            title = f"{title} that have been liked but aren't in the library"
        else:
            title = f"{title} ({min_quality_title} quality)"
    elif min_preference > bottom_quality and max_preference < top_quality:
        title = f"{title} (between {min_quality_title} and {max_quality_title} quality)"
    elif max_preference < top_quality:
        title = f"{title} ({max_quality_title} quality and below)"
    elif min_preference > bottom_quality:
        title = f"{title} ({min_quality_title} quality and above)"

    # Form to filter the shopping list by a particular substring

    library_search_form = LibrarySearchForm()
    if filter_form.validate_on_submit():
        return redirect(
            url_for(
                "main.movie_shopping",
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
            File.edition,
            RefQuality.quality_title,
            movie_file_rank(),
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
                (
                    db.func.mod(
                        (db.func.round(db.func.avg(UserMovieReview.rating) * 2) / 2),
                        1,
                    )
                    == 0,
                    0,
                ),
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
    uhd_quality = (
        db.session.query(db.func.min(RefQuality.preference))
        .filter(RefQuality.quality_title.like("Bluray-2160%"))
        .filter(RefQuality.physical_media == True)
        .scalar()
    )

    CriterionQuality = db.aliased(RefQuality)

    # These CASE expressions are shared by every shopping query variant

    shopping_instruction_case = db.case(
        (Movie.shopping_list_exclude == True, "Already owned"),
        (
            db.and_(
                db.or_(
                    Movie.criterion_spine_number != None,
                    Movie.criterion_set_title != None,
                ),
                Movie.criterion_disc_owned == True,
                #                                 db.and_(
                #                                     db.or_(
                #                                         db.and_(
                #                                             CriterionQuality.preference == dvd_quality,
                #                                             RefQuality.preference >= dvd_quality,
                #                                         ),
                #                                         db.and_(
                #                                             CriterionQuality.preference
                #                                             >= bluray_quality,
                #                                             RefQuality.preference >= bluray_quality,
                #                                         ),
                #                                     ),
                #                                 ),
            ),
            "Already owned",
        ),
        (
            db.and_(
                db.or_(
                    Movie.criterion_spine_number != None,
                    Movie.criterion_set_title != None,
                ),
                Movie.criterion_disc_owned == False,
                CriterionQuality.preference == uhd_quality,
                # RefQuality.preference <= uhd_quality,
            ),
            "Buy Criterion edition on 4K UHD Blu-Ray",
        ),
        (
            db.and_(
                db.or_(
                    Movie.criterion_spine_number != None,
                    Movie.criterion_set_title != None,
                ),
                Movie.criterion_disc_owned == False,
                CriterionQuality.preference == bluray_quality,
                # RefQuality.preference <= bluray_quality,
            ),
            "Buy Criterion edition on Blu-Ray",
        ),
        (
            db.and_(
                db.or_(
                    Movie.criterion_spine_number != None,
                    Movie.criterion_set_title != None,
                ),
                Movie.criterion_disc_owned == False,
                CriterionQuality.preference == dvd_quality,
                # RefQuality.preference <= dvd_quality,
            ),
            "Buy Criterion edition on DVD",
        ),
        # A liked movie with no files (possible since the Letterboxd
        # import) is wanted but entirely unowned
        (File.id == None, "Buy on Blu-Ray"),
        (File.fullscreen == True, "Buy any non-fullscreen release"),
        (
            RefQuality.preference < dvd_quality,
            "Buy on DVD or Blu-Ray",
        ),
        (RefQuality.preference < bluray_quality, "Buy on Blu-Ray"),
        else_=("Already owned"),
    )

    shopping_urgency_order_case = db.case(
        (Movie.criterion_disc_owned == True, -1),
        (Movie.shopping_list_exclude == True, -1),
        (File.id == None, 1),
        (RefQuality.preference < bluray_quality, 1),
        (
            db.and_(
                db.or_(
                    Movie.criterion_spine_number != None,
                    Movie.criterion_set_title != None,
                ),
                db.or_(
                    Movie.criterion_disc_owned == False,
                    Movie.criterion_disc_owned == None,
                ),
            ),
            1,
        ),
        else_=(-1),
    )

    cart_priority_order_case = db.case(
        (Movie.criterion_disc_owned == True, 0),
        (
            db.and_(
                File.id == None,
                db.or_(
                    Movie.shopping_list_exclude == False,
                    Movie.shopping_list_exclude == None,
                ),
            ),
            Movie.shopping_cart_priority,
        ),
        (
            db.and_(
                db.or_(
                    Movie.shopping_list_exclude == False,
                    Movie.shopping_list_exclude == None,
                ),
                RefQuality.preference < bluray_quality,
            ),
            Movie.shopping_cart_priority,
        ),
        (
            db.and_(
                db.or_(
                    Movie.criterion_spine_number != None,
                    Movie.criterion_set_title != None,
                ),
                db.or_(
                    Movie.criterion_disc_owned == False,
                    Movie.criterion_disc_owned == None,
                ),
            ),
            Movie.shopping_cart_priority,
        ),
        else_=(0),
    )

    quality_order_case = db.case(
        (Movie.criterion_disc_owned == True, 99),
        # Nothing owned at all sorts ahead of even the worst owned quality
        (
            db.and_(
                File.id == None,
                db.or_(
                    Movie.shopping_list_exclude == False,
                    Movie.shopping_list_exclude == None,
                ),
            ),
            0,
        ),
        (
            db.and_(
                db.or_(
                    Movie.shopping_list_exclude == False,
                    Movie.shopping_list_exclude == None,
                ),
                RefQuality.preference < bluray_quality,
            ),
            RefQuality.preference,
        ),
        (
            db.and_(
                db.or_(
                    Movie.criterion_spine_number != None,
                    Movie.criterion_set_title != None,
                ),
                db.or_(
                    Movie.criterion_disc_owned == False,
                    Movie.criterion_disc_owned == None,
                ),
            ),
            RefQuality.preference,
        ),
        else_=(99),
    )

    cart_age_order_case = db.case(
        (Movie.criterion_disc_owned == True, 0),
        (
            db.and_(
                File.id == None,
                db.or_(
                    Movie.shopping_list_exclude == False,
                    Movie.shopping_list_exclude == None,
                ),
            ),
            Movie.shopping_cart_add_date,
        ),
        (
            db.and_(
                db.or_(
                    Movie.shopping_list_exclude == False,
                    Movie.shopping_list_exclude == None,
                ),
                RefQuality.preference < bluray_quality,
            ),
            Movie.shopping_cart_add_date,
        ),
        (
            db.and_(
                db.or_(
                    Movie.criterion_spine_number != None,
                    Movie.criterion_set_title != None,
                ),
                db.or_(
                    Movie.criterion_disc_owned == False,
                    Movie.criterion_disc_owned == None,
                ),
            ),
            Movie.shopping_cart_add_date,
        ),
        else_=(0),
    )

    # Movies with a liked review but no files (possible since the
    # Letterboxd import) belong on the shopping list too: they're wanted
    # films not owned in any form. Selecting File and RefQuality through
    # always-false outer joins yields NULL columns for them, so these rows
    # take the same shape as owned titles and can be UNIONed in below.

    liked_movie_ids = db.session.query(UserMovieReview.movie_id).filter(
        UserMovieReview.user_id == int(current_user.id),
        UserMovieReview.liked == True,
    )

    # TV episode files carry a NULL movie_id, and a single NULL in a NOT IN
    # subquery makes the predicate false for every row — filter them out

    owned_movie_ids = db.session.query(File.movie_id).filter(
        File.feature_type_id == None, File.movie_id != None
    )

    def liked_unowned_query():
        return (
            db.session.query(
                File,
                Movie,
                RefQuality,
                rating.c.rating,
                rating.c.modified_rating,
                rating.c.whole_stars,
                rating.c.half_stars,
                shopping_instruction_case.label("instruction"),
            )
            .select_from(Movie)
            .outerjoin(File, db.and_(File.movie_id == Movie.id, File.id == None))
            .outerjoin(RefQuality, RefQuality.id == File.quality_id)
            .outerjoin(
                CriterionQuality, (CriterionQuality.id == Movie.criterion_quality_id)
            )
            .outerjoin(
                rating,
                (rating.c.movie_id == Movie.id) & (rating.c.user_id == current_user.id),
            )
            .filter(Movie.id.in_(liked_movie_ids))
            .filter(Movie.id.not_in(owned_movie_ids))
        )

    if q:
        if re.match(r"tmdb:(?P<tmdb_id>\d+)", q):
            tmdb_id = re.match(r"tmdb:(?P<tmdb_id>\d+)", q).group(1)
            movie = Movie.query.filter_by(tmdb_id=int(tmdb_id)).first()
            if not movie:
                title = f"Upgrade details for TMDB ID {tmdb_id}"
            else:
                title = f"Upgrade details for \"{movie.tmdb_title if movie.tmdb_title else movie.title} ({movie.tmdb_release_date.strftime('%Y') if movie.tmdb_title else movie.year})\""
            movies = (
                db.session.query(
                    File,
                    Movie,
                    RefQuality,
                    rating.c.rating,
                    rating.c.modified_rating,
                    rating.c.whole_stars,
                    rating.c.half_stars,
                    shopping_instruction_case.label("instruction"),
                )
                .join(Movie, (Movie.id == File.movie_id))
                .join(RefQuality, (RefQuality.id == File.quality_id))
                .outerjoin(
                    CriterionQuality,
                    (CriterionQuality.id == Movie.criterion_quality_id),
                )
                .outerjoin(
                    rating,
                    (rating.c.movie_id == Movie.id)
                    & (rating.c.user_id == current_user.id),
                )
                .join(ranked_files, (ranked_files.c.file_id == File.id))
                .filter(File.feature_type_id == None)
                .filter(ranked_files.c.rank == 1)
                .filter(RefQuality.preference >= min_preference)
                .filter(RefQuality.preference <= max_preference)
                .filter(Movie.tmdb_id == tmdb_id)
                .order_by(
                    db.func.regexp_replace(Movie.title, "^(The|A|An) ", "").asc(),
                    Movie.year.asc(),
                    File.edition.asc(),
                    RefQuality.preference.asc(),
                    File.date_added.asc(),
                )
                .paginate(page=page, per_page=100, error_out=False)
            )

        else:
            title = f"Movies to upgrade matching '{q}'"
            owned_matches = (
                db.session.query(
                    File,
                    Movie,
                    RefQuality,
                    rating.c.rating,
                    rating.c.modified_rating,
                    rating.c.whole_stars,
                    rating.c.half_stars,
                    shopping_instruction_case.label("instruction"),
                )
                .join(Movie, (Movie.id == File.movie_id))
                .join(RefQuality, (RefQuality.id == File.quality_id))
                .outerjoin(
                    CriterionQuality,
                    (CriterionQuality.id == Movie.criterion_quality_id),
                )
                .outerjoin(
                    rating,
                    (rating.c.movie_id == Movie.id)
                    & (rating.c.user_id == current_user.id),
                )
                .join(ranked_files, (ranked_files.c.file_id == File.id))
                .filter(File.feature_type_id == None)
                .filter(ranked_files.c.rank == 1)
                .filter(RefQuality.preference >= min_preference)
                .filter(RefQuality.preference <= max_preference)
                .filter(
                    db.or_(
                        Movie.title.ilike(f"%{q}%"), Movie.tmdb_title.ilike(f"%{q}%")
                    )
                )
            )
            liked_matches = liked_unowned_query().filter(
                db.or_(Movie.title.ilike(f"%{q}%"), Movie.tmdb_title.ilike(f"%{q}%"))
            )

            # Films with no local copy count as the virtual bottom quality, so they
            # only appear when the range's minimum reaches down to "Not in library"

            candidates = (
                owned_matches.union_all(liked_matches)
                if min_preference <= bottom_quality
                else owned_matches
            )
            movies = candidates.order_by(
                db.func.regexp_replace(Movie.title, "^(The|A|An) ", "").asc(),
                Movie.year.asc(),
                File.edition.asc(),
                RefQuality.preference.asc(),
                File.date_added.asc(),
            ).paginate(page=page, per_page=100, error_out=False)

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
                shopping_instruction_case.label("instruction"),
            )
            .join(Movie, (Movie.id == File.movie_id))
            .join(RefQuality, (RefQuality.id == File.quality_id))
            .outerjoin(
                CriterionQuality, (CriterionQuality.id == Movie.criterion_quality_id)
            )
            .outerjoin(
                rating,
                (rating.c.movie_id == Movie.id) & (rating.c.user_id == current_user.id),
            )
            .join(ranked_files, (ranked_files.c.file_id == File.id))
            .filter(File.feature_type_id == None)
            .filter(ranked_files.c.rank == 1)
            .filter(RefQuality.preference >= min_preference)
            .filter(RefQuality.preference <= max_preference)
            .filter(Movie.id.not_in(db.select(physical_media.c.movie_id)))
            .filter(
                db.or_(
                    db.and_(
                        criterion_release == True,
                        db.or_(
                            Movie.criterion_spine_number != None,
                            Movie.criterion_set_title != None,
                        ),
                        # Movie.criterion_in_print == 1,
                        # CriterionQuality.preference >= RefQuality.preference,
                    ),
                    criterion_release != True,
                ),
            )
            .order_by(
                shopping_urgency_order_case.desc(),
                cart_priority_order_case.desc(),
                quality_order_case.asc(),
                cart_age_order_case.desc(),
                db.func.regexp_replace(Movie.title, "^(The|A|An) ", "").asc(),
                Movie.year.asc(),
                File.edition.asc(),
                File.date_added.asc(),
            )
            .paginate(page=page, per_page=100, error_out=False)
        )

    else:
        owned_titles = (
            db.session.query(
                File,
                Movie,
                RefQuality,
                rating.c.rating,
                rating.c.modified_rating,
                rating.c.whole_stars,
                rating.c.half_stars,
                shopping_instruction_case.label("instruction"),
            )
            .join(Movie, (Movie.id == File.movie_id))
            .join(RefQuality, (RefQuality.id == File.quality_id))
            .outerjoin(
                CriterionQuality, (CriterionQuality.id == Movie.criterion_quality_id)
            )
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
                        db.or_(
                            Movie.criterion_spine_number != None,
                            Movie.criterion_set_title != None,
                        ),
                        # Movie.criterion_in_print == 1,
                        # CriterionQuality.preference >= RefQuality.preference,
                    ),
                    criterion_release != True,
                ),
            )
        )
        liked_titles = liked_unowned_query().filter(
            db.or_(
                db.and_(
                    criterion_release == True,
                    db.or_(
                        Movie.criterion_spine_number != None,
                        Movie.criterion_set_title != None,
                    ),
                ),
                criterion_release != True,
            ),
        )
        # Films with no local copy count as the virtual bottom quality, so they only
        # appear when the range's minimum reaches down to "Not in library"

        candidates = (
            owned_titles.union_all(liked_titles)
            if min_preference <= bottom_quality
            else owned_titles
        )
        movies = candidates.order_by(
            shopping_urgency_order_case.desc(),
            cart_priority_order_case.desc(),
            quality_order_case.asc(),
            cart_age_order_case.desc(),
            db.func.regexp_replace(Movie.title, "^(The|A|An) ", "").asc(),
            Movie.year.asc(),
            File.edition.asc(),
            File.date_added.asc(),
        ).paginate(page=page, per_page=100, error_out=False)

    movie_shopping_exclude_form = MovieShoppingExcludeForm()

    next_url = (
        url_for(
            "main.movie_shopping",
            page=movies.next_num,
            q=q,
            media=media,
            library=library,
            min_quality=min_quality,
            max_quality=max_quality,
        )
        if movies.has_next
        else None
    )
    prev_url = (
        url_for(
            "main.movie_shopping",
            page=movies.prev_num,
            q=q,
            media=media,
            library=library,
            min_quality=min_quality,
            max_quality=max_quality,
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
        movie.shopping_list_exclude = None
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

    # "Not in library" is the movie shopping list's virtual quality; TV has no
    # unowned rows, so it stays out of this dropdown

    qualities = (
        db.session.query(RefQuality.id, RefQuality.quality_title)
        .filter(RefQuality.quality_title != "Not in library")
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

    # Find the preference associated with the quality ID, and set as the dropdown default

    min_preference = (
        db.session.query(RefQuality.preference).filter_by(id=int(min_quality)).scalar()
    )
    max_preference = (
        db.session.query(RefQuality.preference).filter_by(id=int(max_quality)).scalar()
    )

    # If the minimum quality outranks the maximum, collapse the range to
    # the maximum. Compared by preference — quality ids don't reliably
    # follow quality order

    if min_preference > max_preference:
        min_quality = int(max_quality)
        min_preference = max_preference

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

    # Run the season aggregate once for the whole library and bucket the rows
    # by series, rather than re-running the subquery once per series

    season_rows = (
        db.session.query(
            subquery.c.series_id,
            subquery.c.season,
            subquery.c.episodes,
            RefQuality.quality_title,
            db.case(
                (RefQuality.preference < dvd_quality, "Buy on DVD or Blu-Ray"),
                (RefQuality.preference < bluray_quality, "Buy on Blu-Ray"),
                else_="Already owned",
            ).label("instruction"),
        )
        .join(RefQuality, (RefQuality.preference == subquery.c.preference))
        .filter(RefQuality.preference >= min_preference)
        .filter(RefQuality.preference <= max_preference)
        .order_by(
            subquery.c.series_id,
            db.case((subquery.c.season == 0, 1), else_=0).asc(),
            subquery.c.season.asc(),
        )
        .all()
    )

    seasons_by_series = {}
    for series_id, season, num_episodes, min_quality, instruction in season_rows:
        seasons_by_series.setdefault(series_id, []).append(
            {
                "season": season,
                "episode_count": num_episodes,
                "min_quality": min_quality,
                "instruction": instruction,
            }
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
            .order_by(db.func.regexp_replace(TVSeries.title, "^(The|A|An) ", "").asc())
            .all()
        )

    else:
        t = TVSeries.query.order_by(
            db.func.regexp_replace(TVSeries.title, "^(The|A|An) ", "").asc()
        ).all()
        title = "TV Shows to upgrade"

    for series in t:
        seasons = seasons_by_series.get(series.id, [])

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


@bp.route("/queue")
@login_required
def queue():
    """Show a list of all localization and transcode tasks in queue.

    See api.queue_details for how the queue is generated.
    """

    return render_template("queue.html", title="Queue")


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
            movie_file_rank(),
        )
        .join(Movie, (Movie.id == File.movie_id))
        .join(RefQuality, (RefQuality.id == File.quality_id))
        .subquery()
    )

    tv_rank = (
        db.session.query(
            File.id,
            tv_file_rank(),
        )
        .join(TVSeries, (TVSeries.id == File.series_id))
        .join(RefQuality, (RefQuality.id == File.quality_id))
        .subquery()
    )

    files_with_lossless = (
        db.session.query(FileAudioTrack.file_id)
        .filter(FileAudioTrack.compression_mode == "Lossless")
        .subquery()
    )

    lossy_files = (
        db.session.query(FileAudioTrack.file_id)
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
                    (movie_rank.c.rank == 1, 1), (tv_rank.c.rank == 1, 1), else_=0
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
                        (Movie.tmdb_title != None, Movie.tmdb_title),
                        else_=Movie.title,
                    ),
                    "^(The|A|An) ",
                    "",
                ).asc(),
                db.case(
                    (Movie.tmdb_title != None, Movie.tmdb_release_date),
                    else_=Movie.year,
                ).asc(),
                File.edition.asc(),
                RefFeatureType.feature_type.asc(),
                db.func.regexp_replace(
                    db.case(
                        (TVSeries.tmdb_name != None, TVSeries.tmdb_name),
                        else_=TVSeries.title,
                    ),
                    "^(The|A|An) ",
                    "",
                ).asc(),
                File.season.asc(),
                File.episode.asc(),
                File.last_episode.asc(),
                RefQuality.preference.asc(),
                File.basename.asc(),
            )
            .paginate(page=page, per_page=1000, error_out=False)
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
                    (movie_rank.c.rank == 1, 1), (tv_rank.c.rank == 1, 1), else_=0
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
                        (Movie.tmdb_title != None, Movie.tmdb_title),
                        else_=Movie.title,
                    ),
                    "^(The|A|An) ",
                    "",
                ).asc(),
                db.case(
                    (Movie.tmdb_title != None, Movie.tmdb_release_date),
                    else_=Movie.year,
                ).asc(),
                File.edition.asc(),
                RefFeatureType.feature_type.asc(),
                db.func.regexp_replace(
                    db.case(
                        (TVSeries.tmdb_name != None, TVSeries.tmdb_name),
                        else_=TVSeries.title,
                    ),
                    "^(The|A|An) ",
                    "",
                ).asc(),
                File.season.asc(),
                File.episode.asc(),
                File.last_episode.asc(),
                RefQuality.preference.asc(),
                File.basename.asc(),
            )
            .paginate(page=page, per_page=1000, error_out=False)
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
                    (movie_rank.c.rank == 1, 1), (tv_rank.c.rank == 1, 1), else_=0
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
                        (Movie.tmdb_title != None, Movie.tmdb_title),
                        else_=Movie.title,
                    ),
                    "^(The|A|An) ",
                    "",
                ).asc(),
                db.case(
                    (Movie.tmdb_title != None, Movie.tmdb_release_date),
                    else_=Movie.year,
                ).asc(),
                File.edition.asc(),
                RefFeatureType.feature_type.asc(),
                db.func.regexp_replace(
                    db.case(
                        (TVSeries.tmdb_name != None, TVSeries.tmdb_name),
                        else_=TVSeries.title,
                    ),
                    "^(The|A|An) ",
                    "",
                ).asc(),
                File.season.asc(),
                File.episode.asc(),
                File.last_episode.asc(),
                RefQuality.preference.asc(),
                File.basename.asc(),
            )
            .paginate(page=page, per_page=1000, error_out=False)
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
                    (movie_rank.c.rank == 1, 1), (tv_rank.c.rank == 1, 1), else_=0
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
                        (Movie.tmdb_title != None, Movie.tmdb_title),
                        else_=Movie.title,
                    ),
                    "^(The|A|An) ",
                    "",
                ).asc(),
                db.case(
                    (Movie.tmdb_title != None, Movie.tmdb_release_date),
                    else_=Movie.year,
                ).asc(),
                File.edition.asc(),
                RefFeatureType.feature_type.asc(),
                db.func.regexp_replace(
                    db.case(
                        (TVSeries.tmdb_name != None, TVSeries.tmdb_name),
                        else_=TVSeries.title,
                    ),
                    "^(The|A|An) ",
                    "",
                ).asc(),
                File.season.asc(),
                File.episode.asc(),
                File.last_episode.asc(),
                RefQuality.preference.asc(),
                File.basename.asc(),
            )
            .paginate(page=page, per_page=1000, error_out=False)
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
                    (movie_rank.c.rank == 1, 1), (tv_rank.c.rank == 1, 1), else_=0
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
                        (Movie.tmdb_title != None, Movie.tmdb_title),
                        else_=Movie.title,
                    ),
                    "^(The|A|An) ",
                    "",
                ).asc(),
                db.case(
                    (Movie.tmdb_title != None, Movie.tmdb_release_date),
                    else_=Movie.year,
                ).asc(),
                File.edition.asc(),
                RefFeatureType.feature_type.asc(),
                db.func.regexp_replace(
                    db.case(
                        (TVSeries.tmdb_name != None, TVSeries.tmdb_name),
                        else_=TVSeries.title,
                    ),
                    "^(The|A|An) ",
                    "",
                ).asc(),
                File.season.asc(),
                File.episode.asc(),
                File.last_episode.asc(),
                RefQuality.preference.asc(),
                File.basename.asc(),
            )
            .paginate(page=page, per_page=1000, error_out=False)
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
