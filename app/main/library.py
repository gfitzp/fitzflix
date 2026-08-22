"""The library pages (#17's slice f): movie and TV browsing,
filmographies, the Criterion spine catalog, people, and the per-title
movie/tv/season/file detail pages."""

import json
import os
import re
import time
import traceback


from datetime import date, datetime

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
from markupsafe import Markup
from flask_login import current_user, login_required

from app import db, safe_job_id
from app.main.forms import (
    CriterionForm,
    FileDeleteForm,
    NotInterestedForm,
    LibrarySearchForm,
    MKVMergeForm,
    MKVPropEditForm,
    MovieReviewForm,
    MovieShoppingExcludeForm,
    RadarrForm,
    QualityFilterForm,
    S3DownloadForm,
    SeasonRestoreForm,
    SeriesRestoreForm,
    S3UploadForm,
    SeriesDeleteForm,
    TMDBLookupForm,
    TrackMetadataScanForm,
    TranscodeForm,
    WatchlistForm,
)
from app.models import (
    CatalogExclusion,
    File,
    FileAudioTrack,
    FileSubtitleTrack,
    Movie,
    MovieAward,
    MovieCast,
    MovieCrew,
    RefFeatureType,
    RefQuality,
    TMDBCredit,
    TMDBGenre,
    TVCast,
    TVCrew,
    TVEpisode,
    TVSeries,
    UserMovieReview,
    UserMovieStatus,
    UserWatchlist,
    movie_file_rank,
    movie_genres,
    tmdb_get,
    tv_file_rank,
)
from app.main import bp
from app.plex_player import play_movie, remote_playback_configured
from app.main.helpers import (
    _card_fetch,
    _enqueue_profile_recompute,
    _ladder_fetch,
    _ladder_state,
    _latest_review_row,
    _mark_not_interested,
    _quick_rating,
    _same_day_rerate,
    _upgrade_threshold,
    _watched_timestamp,
)
from app.recommendations import (
    CREW_ROLE_JOBS,
    coarse_interest_score,
    credit_interest_markers,
    estimated_rating,
    marker_bar,
    not_interested_movie_ids,
    recommended_movie_ids,
    resolved_score,
    stored_profile,
)
from app.streaming import (
    batch_title_availability,
    rental_matches,
    streaming_matches,
    user_provider_ids,
    user_streaming,
)
from app.elicitation import (
    set_last_response,
    suggestions_after_rating,
)
from app.radarr_push import (
    radarr_configured,
    radarr_tmdb_ids,
)
from app.triage import (
    forced_subtitle_candidates,
)
from app.videos import (
    clear_not_interested,
    clear_watchlist,
    criterion_release_lookups,
    get_criterion_collection_from_wikidata,
    star_rating_fields,
    track_metadata_scan,
    untouched_key_still_claimed,
)

# The crew jobs that count as key roles for search and filmographies —
# the same roles the taste engine scores, labeled as nouns (Glenn's
# call: only these join the film-count ordering, so grips and gaffers
# don't outrank directors)

CREW_ROLE_LABELS = {
    job: role.capitalize() for role, (jobs, _) in CREW_ROLE_JOBS.items() for job in jobs
}


# A multi-role credit line reads in conventional closing-credit order —
# directed, written, shot, edited, scored — not TMDb payload order

CLOSING_CREDIT_ORDER = ("Director", "Writer", "Cinematographer", "Editor", "Composer")


# A TV role that is the person appearing as themselves — "Self",
# "Self - Host", "Herself (archive footage)" — as TMDb writes them:
# the self-word leads the line. Word-bounded so genuine characters
# that merely contain the letters (Harry Selfridge) survive

SELF_ROLE = re.compile(r"(?:him|her|them)?sel(?:f|ves)\b", re.IGNORECASE)


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
    genre = request.args.get("genre", None, type=int)
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

        def local_credit_rows(credit_table):
            """The person's local films through a credit join table, each
            with its best owned file outer-joined."""

            query = (
                db.session.query(File, Movie, RefQuality)
                .select_from(Movie)
                .join(credit_table, (credit_table.movie_id == Movie.id))
                .outerjoin(
                    File,
                    db.and_(
                        File.movie_id == Movie.id,
                        File.feature_type_id == None,
                        File.id.in_(best_file_ids),
                    ),
                )
                .outerjoin(RefQuality, (RefQuality.id == File.quality_id))
                .filter(credit_table.credit_id == int(credit))
            )
            if credit_table is MovieCrew:
                query = query.filter(MovieCrew.job.in_(list(CREW_ROLE_LABELS)))
            return query.all()

        local_rows = local_credit_rows(MovieCast) + local_credit_rows(MovieCrew)

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
                    payload = r.json()
                    tmdb_credits = {
                        "cast": payload.get("cast") or [],
                        "crew": [
                            crew_credit
                            for crew_credit in payload.get("crew") or []
                            if crew_credit.get("job") in CREW_ROLE_LABELS
                        ],
                    }
                    current_app.redis.set(cache_key, json.dumps(tmdb_credits), ex=86400)
                except Exception:
                    current_app.logger.warning(traceback.format_exc())

        # Day-cached payloads written before crew credits joined the
        # filmography are bare cast lists

        if isinstance(tmdb_credits, list):
            tmdb_credits = {"cast": tmdb_credits, "crew": []}

        # Merge: one row per film, TMDb credits first (deduped by film,
        # combining characters), then any local credits TMDb didn't list

        local_by_tmdb_id = {
            entry["movie"].tmdb_id: entry
            for entry in local.values()
            if entry["movie"].tmdb_id is not None
        }
        rows = {}

        def credit_row(entry):
            """The merged filmography row for a TMDb credit entry,
            created on first sight — cast and crew credits for the same
            film share one row."""

            tmdb_id = entry.get("id")
            row = rows.get(tmdb_id)
            if row is None:
                release_year = (entry.get("release_date") or "")[:4]
                local_entry = local_by_tmdb_id.get(tmdb_id)
                row = rows[tmdb_id] = {
                    "tmdb_id": tmdb_id,
                    "title": entry.get("title"),
                    "year": int(release_year) if release_year.isdigit() else None,
                    "poster_path": entry.get("poster_path"),
                    "genre_ids": entry.get("genre_ids") or [],
                    "overview": entry.get("overview"),
                    "characters": [],
                    "jobs": [],
                    "movie": local_entry["movie"] if local_entry else None,
                    "file": local_entry["file"] if local_entry else None,
                    "quality": local_entry["quality"] if local_entry else None,
                }
            return row

        for cast_credit in (tmdb_credits or {}).get("cast") or []:
            if cast_credit.get("id") is None:
                continue
            row = credit_row(cast_credit)
            if cast_credit.get("character"):
                row["characters"].append(cast_credit["character"])

        for crew_credit in (tmdb_credits or {}).get("crew") or []:
            if crew_credit.get("id") is None:
                continue
            row = credit_row(crew_credit)
            label = CREW_ROLE_LABELS.get(crew_credit.get("job"))
            if label and label not in row["jobs"]:
                row["jobs"].append(label)
        for row in rows.values():
            row["jobs"].sort(key=CLOSING_CREDIT_ORDER.index)

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
                "jobs": [],
                "movie": entry["movie"],
                "file": entry["file"],
                "quality": entry["quality"],
            }

        filmography = sorted(
            rows.values(), key=lambda row: (row["year"] is None, row["year"] or 0)
        )
        watchlisted = {
            movie_id
            for (movie_id,) in db.session.query(UserWatchlist.movie_id)
            .filter(UserWatchlist.user_id == int(current_user.id))
            .filter(UserWatchlist.movie_id.in_(list(local.keys()) or [0]))
        }
        for row in filmography:
            row["seen"] = row["movie"] is not None and row["movie"].id in reviewed
            row["liked"] = bool(row["movie"] and reviewed.get(row["movie"].id))
            row["watchlisted"] = (
                row["movie"] is not None and row["movie"].id in watchlisted
            )

        # "Might interest you" markers: unowned films score at render
        # time from the already-cached credits payload against the
        # user's stored taste profile (no TMDb calls, nothing
        # persisted); owned unwatched films badge when the nightly
        # recompute ranked them in the stored recommendations, so
        # filmographies agree with the library rail and search pages

        profile = stored_profile(current_app.redis, current_user.id)
        rec_ids = recommended_movie_ids(current_app.redis, current_user.id)
        refused = not_interested_movie_ids(current_user.id)
        interesting = credit_interest_markers(profile, int(credit), filmography)
        for row in filmography:
            if row["movie"] is not None and row["movie"].id in refused:
                row["might_interest"] = False
                continue
            unowned_marker = (
                row["quality"] is None
                and not row["seen"]
                and row["tmdb_id"] in interesting
            )
            owned_marker = bool(
                row["movie"] and not row["seen"] and row["movie"].id in rec_ids
            )
            row["might_interest"] = unowned_marker or owned_marker

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

        # Television credits (#78 step 6): the person's TMDb TV career,
        # one row per series, day-cached like the film list. Self
        # appearances are dropped — talk-show and awards-night rows
        # would swamp the acting credits (the #27 key-roles spirit).
        # Owned series link to their pages; TV has no review flow, so
        # unowned rows render unlinked (#30's rule).

        tv_rows = {}
        tv_credits = None
        if current_app.config["TMDB_API_KEY"]:
            tv_cache_key = f"fitzflix:tmdb:person:{int(credit)}:tv_credits"
            cached = current_app.redis.get(tv_cache_key)
            if cached:
                tv_credits = json.loads(cached)
            else:
                try:
                    r = tmdb_get(
                        current_app.config["TMDB_API_URL"]
                        + f"/person/{int(credit)}/tv_credits",
                        params={"api_key": current_app.config["TMDB_API_KEY"]},
                        timeout=10,
                    )
                    r.raise_for_status()
                    payload = r.json()
                    tv_credits = {
                        "cast": [
                            entry
                            for entry in payload.get("cast") or []
                            if not SELF_ROLE.match(entry.get("character") or "")
                        ],
                        "crew": [
                            entry
                            for entry in payload.get("crew") or []
                            if entry.get("job") in CREW_ROLE_LABELS
                        ],
                    }
                    current_app.redis.set(
                        tv_cache_key, json.dumps(tv_credits), ex=86400
                    )
                except Exception:
                    current_app.logger.warning(traceback.format_exc())

        if tv_credits:
            credited_ids = {
                entry.get("id")
                for entry in (tv_credits.get("cast") or [])
                + (tv_credits.get("crew") or [])
                if entry.get("id") is not None
            }
            local_series = {
                series.tmdb_id: series
                for series in TVSeries.query.filter(
                    TVSeries.tmdb_id.in_(credited_ids or [0])
                )
            }

            def tv_credit_row(entry):
                tmdb_id = entry.get("id")
                row = tv_rows.get(tmdb_id)
                if row is None:
                    first_air = (entry.get("first_air_date") or "")[:4]
                    series = local_series.get(tmdb_id)
                    row = tv_rows[tmdb_id] = {
                        "tmdb_id": tmdb_id,
                        "name": entry.get("name"),
                        "year": int(first_air) if first_air.isdigit() else None,
                        "poster_path": entry.get("poster_path"),
                        "overview": entry.get("overview"),
                        "characters": [],
                        "jobs": [],
                        "episode_count": entry.get("episode_count"),
                        "series": series,
                        "owned": bool(series and series.files.count()),
                    }
                return row

            for entry in tv_credits.get("cast") or []:
                if entry.get("id") is None:
                    continue
                row = tv_credit_row(entry)
                if entry.get("character"):
                    row["characters"].append(entry["character"])
            for entry in tv_credits.get("crew") or []:
                if entry.get("id") is None:
                    continue
                row = tv_credit_row(entry)
                label = CREW_ROLE_LABELS.get(entry.get("job"))
                if label and label not in row["jobs"]:
                    row["jobs"].append(label)
            for row in tv_rows.values():
                row["jobs"].sort(key=CLOSING_CREDIT_ORDER.index)

        television = sorted(
            tv_rows.values(), key=lambda row: (row["year"] is None, row["year"] or 0)
        )

        return render_template(
            "filmography.html",
            title=person_name,
            person_name=person_name,
            profile_path=person_profile_path,
            bio=bio,
            filmography=filmography,
            television=television,
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

    elif genre:
        # Genre links on the movie pages land here (#56): the library
        # filtered to films carrying the TMDb genre, composable with
        # the quality dropdown

        genre_row = db.session.get(TMDBGenre, int(genre))
        if genre_row is None:
            abort(404)
        title = f"{genre_row.name} Movies"
        movies = (
            db.session.query(File, Movie, RefQuality)
            .join(Movie, (Movie.id == File.movie_id))
            .join(RefQuality, (RefQuality.id == File.quality_id))
            .join(ranked_files, (ranked_files.c.id == File.id))
            .join(movie_genres, (movie_genres.c.movie_id == Movie.id))
            .filter(movie_genres.c.genre_id == int(genre))
            .filter(File.feature_type_id == None)
            .filter(ranked_files.c.rank == 1)
        )
        if int(quality) > 0:
            movies = movies.filter(RefQuality.id == int(quality))
        movies = movies.order_by(
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
        ).paginate(page=page, per_page=120, error_out=False)

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
        url_for(
            "main.movie_library", page=movies.next_num, quality=quality, genre=genre
        )
        if movies.has_next
        else None
    )
    prev_url = (
        url_for(
            "main.movie_library", page=movies.prev_num, quality=quality, genre=genre
        )
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


CRITERION_CHANNEL_PROVIDER_ID = 258


CRITERION_CATALOG_PER_PAGE = 120


def _page_window(current, last):
    """Page numbers for a pagination bar, with None marking a gap.

    The same shape Flask-SQLAlchemy's iter_pages renders on the people
    page: the first and last couple of pages, a window around the
    current one, ellipses between.
    """

    numbers = []
    previous = 0
    for number in range(1, last + 1):
        if number <= 2 or abs(number - current) <= 2 or number > last - 2:
            if previous and number - previous > 1:
                numbers.append(None)
            numbers.append(number)
            previous = number
    return numbers


@bp.route("/library/criterion-collection")
@login_required
def criterion_collection():
    """The full Criterion Collection spine catalog, library and beyond.

    Every release from the Wikidata spine cache renders, not just the
    library's films: owned films keep their settled/amber verdicts,
    releases the library lacks render like TMDb search rows (their row
    opens the log page, so they're watchlistable), and the handful of
    releases Wikidata has no TMDb id for list as plain spine rows. A
    Criterion Channel badge marks what's streaming there right now.
    """

    filter_status = request.args.get("filter", "all")
    if filter_status not in ("all", "library", "settled"):
        filter_status = "all"
    page = max(request.args.get("page", 1, type=int) or 1, 1)

    # The whole spine catalog from the weekly Wikidata cache; the page
    # degrades to library-only rows if the cache is cold and Wikidata
    # is unreachable

    try:
        releases = get_criterion_collection_from_wikidata()
    except Exception:
        current_app.logger.warning(traceback.format_exc())
        releases = []
    by_tmdb_id, by_title_year = criterion_release_lookups(releases)
    release_tmdb_ids = [
        release["tmdb_id"] for release in releases if release.get("tmdb_id")
    ]

    # Library rows: best main-feature file per film, for films marked
    # with Criterion metadata OR matching a release by TMDb id (a film
    # whose record predates its release never got marked, but the
    # catalog knows its spine)

    ranked_files = (
        db.session.query(
            File.id,
            movie_file_rank(),
        )
        .join(Movie, (Movie.id == File.movie_id))
        .join(RefQuality, (RefQuality.id == File.quality_id))
        .subquery()
    )

    results = (
        db.session.query(File, Movie, RefQuality)
        .join(Movie, (Movie.id == File.movie_id))
        .join(RefQuality, (RefQuality.id == File.quality_id))
        .join(ranked_files, (ranked_files.c.id == File.id))
        .filter(File.feature_type_id == None)
        .filter(ranked_files.c.rank == 1)
        .filter(File.edition == None)
        .filter(
            db.or_(
                Movie.criterion_spine_number != None,
                Movie.criterion_set_title != None,
                Movie.tmdb_id.in_(release_tmdb_ids or [0]),
            )
        )
        .all()
    )

    # A library row is SETTLED — the Fitzflix library badge, nothing to
    # do — when the Criterion disc is owned AND the local file matches
    # the release's own format, with the bar CAPPED at the app-wide
    # threshold (Glenn: an owned disc with a Bluray-1080p file is
    # settled here even if Criterion re-released it in 2160p — chasing
    # that upgrade is the shopping list's job, not this page's). The
    # threshold also covers releases whose quality was never recorded.
    # Anything else shows its amber quality tier: go find the Criterion
    # version

    movie_ids = [movie.id for _, movie, _ in results]
    CriterionQuality = db.aliased(RefQuality)
    criterion_prefs = dict(
        db.session.query(Movie.id, CriterionQuality.preference)
        .join(CriterionQuality, CriterionQuality.id == Movie.criterion_quality_id)
        .filter(Movie.id.in_(movie_ids or [0]))
    )
    threshold = _upgrade_threshold()

    # Each library film consumes its catalog release (TMDb id first,
    # title+year fallback — the import's own matching order), so the
    # remainder renders as beyond-the-library rows. A film with both a
    # standalone release and a set membership consumes both through its
    # shared TMDb id

    consumed_tmdb = set()
    consumed_title_year = set()
    library_rows = []
    for file, movie, quality in results:
        release = by_tmdb_id.get(movie.tmdb_id) if movie.tmdb_id else None
        if release is None and movie.title and movie.year:
            release = by_title_year.get((movie.title.upper(), movie.year))
        if movie.tmdb_id:
            consumed_tmdb.add(movie.tmdb_id)
        if release:
            if release.get("tmdb_id"):
                consumed_tmdb.add(release["tmdb_id"])
            if release.get("title") and release.get("year"):
                consumed_title_year.add((release["title"], release["year"]))
        target = min(criterion_prefs.get(movie.id) or threshold, threshold)
        upgradable = bool(file.fullscreen) or quality.preference < target
        library_rows.append(
            {
                "kind": "library",
                "file": file,
                "movie": movie,
                "quality": quality,
                "settled": bool(movie.criterion_disc_owned) and not upgradable,
                "tmdb_id": movie.tmdb_id,
                "title": movie.tmdb_title or movie.title,
                "year": (
                    movie.tmdb_release_date.year
                    if movie.tmdb_title and movie.tmdb_release_date
                    else movie.year
                ),
                "spine": movie.criterion_spine_number
                or (release or {}).get("spine_number"),
                "set_title": movie.criterion_set_title
                or (release or {}).get("set_title"),
            }
        )

    # The rest of the catalog. Standalone entries precede set entries
    # in the cache, so a film with both keeps its own spine; releases
    # without a TMDb id render as plain spine rows. Box-set CONTAINER
    # items are redundant: Wikidata gives the set item the spine (and
    # no TMDb id — TMDb has no set entries) while its member films
    # arrive separately wearing the set title, so a TMDb-less row whose
    # spine belongs to a set would just shadow its own members ("#88
    # Ivan the Terrible" between the actual Parts I–III)

    set_spines = {
        release["spine_number"] for release in releases if release.get("set_title")
    }
    excluded_tmdb = {
        tmdb_id for (tmdb_id,) in db.session.query(CatalogExclusion.tmdb_id)
    }
    catalog_rows = []
    catalog_keys = set()
    for release in releases:
        tmdb_id = release.get("tmdb_id")
        if (
            not tmdb_id
            and not release.get("set_title")
            and release.get("spine_number") in set_spines
        ):
            continue
        # Hand-excluded ids (Wikidata junk — see CatalogExclusion)
        # neither render nor get records created
        if tmdb_id and tmdb_id in excluded_tmdb:
            continue
        if tmdb_id and tmdb_id in consumed_tmdb:
            continue
        title_year = (release.get("title"), release.get("year"))
        if title_year in consumed_title_year:
            continue
        key = tmdb_id or title_year
        if key in catalog_keys:
            continue
        catalog_keys.add(key)
        catalog_rows.append(
            {
                "kind": "tmdb" if tmdb_id else "plain",
                "movie": None,
                "tmdb_id": tmdb_id,
                "title": release.get("label") or release.get("title"),
                "year": release.get("year"),
                "spine": release.get("spine_number"),
                "set_title": release.get("set_title"),
            }
        )

    # File-less local records (logged or watchlisted unowned films)
    # dress their catalog rows with the stored title, poster, and
    # overview — and carry the funnel badges

    records = {}
    catalog_tmdb_ids = [row["tmdb_id"] for row in catalog_rows if row["tmdb_id"]]
    if catalog_tmdb_ids:
        records = {
            record.tmdb_id: record
            for record in Movie.query.filter(Movie.tmdb_id.in_(catalog_tmdb_ids))
        }
    for row in catalog_rows:
        record = records.get(row["tmdb_id"]) if row["tmdb_id"] else None
        if record is None:
            continue
        row["movie"] = record
        if record.tmdb_title:
            row["title"] = record.tmdb_title
            if record.tmdb_release_date:
                row["year"] = record.tmdb_release_date.year

    # The personal funnel, per-user like everywhere else: seen films
    # never badge might-interest. Owned films badge on stored-ranking
    # membership; catalog rows with a refreshed record score through
    # the coarse scorer against the profile-relative bar (rows without
    # a record have no genres to score — they stay unmarked)

    funnel_ids = movie_ids + [record.id for record in records.values()]
    seen_ids = {
        movie_id
        for (movie_id,) in db.session.query(UserMovieReview.movie_id)
        .filter(UserMovieReview.user_id == int(current_user.id))
        .filter(UserMovieReview.movie_id.in_(funnel_ids or [0]))
    }
    watchlisted_ids = {
        movie_id
        for (movie_id,) in db.session.query(UserWatchlist.movie_id)
        .filter(UserWatchlist.user_id == int(current_user.id))
        .filter(UserWatchlist.movie_id.in_(funnel_ids or [0]))
    }
    rec_ids = recommended_movie_ids(current_app.redis, current_user.id)
    refused_ids = not_interested_movie_ids(current_user.id)
    profile = stored_profile(current_app.redis, current_user.id)
    bar = marker_bar(profile) if profile else None

    for row in library_rows:
        movie_id = row["movie"].id
        row["seen"] = movie_id in seen_ids
        row["watchlisted"] = movie_id in watchlisted_ids
        row["might_interest"] = (
            movie_id in rec_ids
            and movie_id not in seen_ids
            and movie_id not in refused_ids
        )
    for row in catalog_rows:
        record = row["movie"]
        row["seen"] = record is not None and record.id in seen_ids
        row["watchlisted"] = record is not None and record.id in watchlisted_ids
        row["might_interest"] = False
        if record is not None and record.id in refused_ids:
            continue
        if record is not None and profile is not None and not row["seen"]:
            genre_ids = [genre.id for genre in record.genres]
            if genre_ids:
                score = coarse_interest_score(profile, genre_ids, row["year"])
                row["might_interest"] = score > bar

    # One spine order across owned and unowned: set members sort at
    # their set's spine (year, then title within), spine-less local
    # rows keep their old place at the end

    def sort_key(row):
        """Spine order, set members at their set's number."""

        spine = row.get("spine")
        title = re.sub(
            r"^(The|A|An)\s+", "", row.get("title") or "", flags=re.IGNORECASE
        )
        return (
            0 if spine is not None else 1,
            spine if spine is not None else 0,
            row.get("set_title") or "",
            row.get("year") or 9999,
            title.upper(),
        )

    merged = sorted(library_rows + catalog_rows, key=sort_key)

    counts = {
        "all": len(merged),
        "library": len(library_rows),
        "settled": sum(1 for row in library_rows if row["settled"]),
    }
    if filter_status == "library":
        filtered = [row for row in merged if row["kind"] == "library"]
    elif filter_status == "settled":
        filtered = [
            row for row in merged if row["kind"] == "library" and row["settled"]
        ]
    else:
        filtered = merged

    last_page = max(
        (len(filtered) + CRITERION_CATALOG_PER_PAGE - 1) // CRITERION_CATALOG_PER_PAGE,
        1,
    )
    page = min(page, last_page)
    start = (page - 1) * CRITERION_CATALOG_PER_PAGE
    rows = filtered[start : start + CRITERION_CATALOG_PER_PAGE]

    # The Criterion Channel badge (provider 258), for the rows on this
    # page only: availability is day-cached per title and fetches are
    # bounded like the filmography pages — at most 50 synchronous
    # misses, the rest warmed in the background for the next visit

    streaming_attribution = False
    availability_by_id, deferred = batch_title_availability(
        (row["tmdb_id"] for row in rows if row["tmdb_id"]),
        fetch_limit=50,
    )
    if deferred and current_app.redis.set(
        f"fitzflix:streaming:warm:criterion:{filter_status}:{page}",
        "1",
        nx=True,
        ex=900,
    ):
        current_app.maintenance_queue.enqueue(
            "app.streaming.warm_title_availability",
            args=(deferred,),
            job_timeout="30m",
            description=(f"Warming streaming availability for {len(deferred)} films"),
        )
    for row in rows:
        if not row["tmdb_id"]:
            continue
        matches = streaming_matches(
            availability_by_id.get(row["tmdb_id"]),
            {CRITERION_CHANNEL_PROVIDER_ID},
        )
        if matches:
            row["streaming"] = matches
            streaming_attribution = True

    return render_template(
        "library_criterion.html",
        title="Criterion Collection films",
        rows=rows,
        filter_status=filter_status,
        counts=counts,
        page_numbers=_page_window(page, last_page),
        current_page=page,
        streaming_attribution=streaming_attribution,
        prev_url=(
            url_for("main.criterion_collection", filter=filter_status, page=page - 1)
            if page > 1
            else None
        ),
        next_url=(
            url_for("main.criterion_collection", filter=filter_status, page=page + 1)
            if page < last_page
            else None
        ),
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
    # (credit id, name) pairs so the directed-by line links to
    # filmography pages, like the rating drive's featured card
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

    # The date field starts BLANK: the default log is date-less ("seen
    # sometime, unknown when") — Plex supplies real timestamps for
    # watches it sees, and the field is there for the times a date is
    # actually known

    movie_review_form = MovieReviewForm()
    quick_present, quick_rating = _quick_rating()
    if (
        movie_review_form.review_submit.data or quick_present
    ) and movie_review_form.validate_on_submit():
        if quick_present and quick_rating is None:
            flash("That rating didn't make sense", "warning")
            return redirect(url_for("main.movie", movie_id=movie.id))
        # The ladder is the only rating input; Log Watch without a tap
        # is a bare diary entry. 3+ stars auto-flag liked. The date and
        # review text submit as they stand either way. A tap on a
        # SUGGESTION card carries that film's movie_id and rates it
        # (date-less), while the strip stays anchored to this page

        rating = quick_rating
        target = movie
        if quick_present:
            form_movie_id = (request.form.get("movie_id") or "").strip()
            if form_movie_id.isdigit() and int(form_movie_id) != movie.id:
                target = db.session.get(Movie, int(form_movie_id)) or movie

        # ✕ is "not interested, never saw it" — a status flag, never a
        # review (#51). The film leaves every recommendation surface, a
        # seen film can't be flagged (its floor is 1 star), and tapping
        # a lit ✕ undoes the flag (#54)

        if rating == 0:
            target_title = (
                title
                if target.id == movie.id
                else (
                    f"{target.tmdb_title if target.tmdb_title else target.title} "
                    f"({target.tmdb_release_date.strftime('%Y') if target.tmdb_title and target.tmdb_release_date else target.year})"
                )
            )
            existing_flag = UserMovieStatus.query.filter_by(
                user_id=int(current_user.id), movie_id=target.id, kind="not_interested"
            ).first()
            if existing_flag is not None:
                db.session.delete(existing_flag)
                db.session.commit()
                _enqueue_profile_recompute()
                if not _ladder_fetch():
                    flash(f"'{target_title}' can be recommended again", "success")
            elif _mark_not_interested(current_user.id, target.id):
                # A poster-card post (#45c) never steers the drive —
                # only a film's own page (and the featured card) moves
                # the last-response state
                if target.id == movie.id and not request.form.get("from_card"):
                    set_last_response(
                        current_app.redis, current_user.id, movie.id, "not_interested"
                    )
                _enqueue_profile_recompute()
                if not _ladder_fetch():
                    flash(f"Got it — '{target_title}' won't be recommended", "info")
            elif not _ladder_fetch():
                flash(
                    f"You've logged '{target_title}' — the lowest rating "
                    f"for a seen film is 1 star",
                    "warning",
                )
            if _ladder_fetch():
                return _ladder_state(current_user.id, target.id)
            return redirect(url_for("main.movie", movie_id=movie.id))

        # Tapping your current rating removes it (#54): a bare drive-
        # style row (no watch date, no text) disappears entirely, while
        # a viewing with real history only loses its stars

        if rating is not None:
            current_row = _latest_review_row(current_user.id, target.id)
            if (
                current_row is not None
                and current_row.rating is not None
                and float(current_row.rating) == rating
            ):
                target_title = (
                    title
                    if target.id == movie.id
                    else (
                        f"{target.tmdb_title if target.tmdb_title else target.title} "
                        f"({target.tmdb_release_date.strftime('%Y') if target.tmdb_title and target.tmdb_release_date else target.year})"
                    )
                )
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
                db.session.commit()
                _enqueue_profile_recompute()
                if _ladder_fetch():
                    return _ladder_state(current_user.id, target.id)
                flash(f"Removed your rating of '{target_title}'", "success")
                return redirect(url_for("main.movie", movie_id=movie.id))

        # A different star on a day you already reviewed corrects that
        # review in place — tastes change, but not twice a day; a form
        # carrying text or a watch date is a real new log and skips this

        if (
            quick_present
            and rating is not None
            and not (movie_review_form.review.data or "").strip()
            and movie_review_form.date_watched.data is None
        ):
            edited = _same_day_rerate(current_user.id, target.id, rating)
            if edited is not None:
                clear_watchlist(current_user.id, target.id)
                clear_not_interested(current_user.id, target.id)
                db.session.commit()
                if target.id == movie.id and not request.form.get("from_card"):
                    set_last_response(
                        current_app.redis,
                        current_user.id,
                        movie.id,
                        "rated",
                        positive=rating >= 3,
                    )
                _enqueue_profile_recompute()
                if _ladder_fetch():
                    return _ladder_state(current_user.id, target.id)
                target_title = (
                    title
                    if target.id == movie.id
                    else (
                        f"{target.tmdb_title if target.tmdb_title else target.title} "
                        f"({target.tmdb_release_date.strftime('%Y') if target.tmdb_title and target.tmdb_release_date else target.year})"
                    )
                )
                flash(f"Rated '{target_title}' {rating:g} out of 5 stars", "success")
                return redirect(url_for("main.movie", movie_id=movie.id))

        # A bare submission (no rating or text) is a plain diary
        # entry — a watch, not a review — so it carries no review date.
        # Rewatch is computed the way Plex watches compute it: any earlier
        # row for this user and film makes this a repeat viewing.

        is_review = bool(
            rating is not None or (movie_review_form.review.data or "").strip()
        )
        rewatch = (
            db.session.query(UserMovieReview.id)
            .filter_by(user_id=current_user.id, movie_id=target.id)
            .first()
            is not None
        )
        review = UserMovieReview(
            user_id=current_user.id,
            movie_id=target.id,
            review=movie_review_form.review.data,
            liked=rating is not None and rating >= 3,
            date_watched=_watched_timestamp(movie_review_form.date_watched.data),
            date_reviewed=datetime.now() if is_review else None,
            rewatch=rewatch,
            **star_rating_fields(rating),
        )
        db.session.add(review)
        clear_watchlist(current_user.id, target.id)
        clear_not_interested(current_user.id, target.id)
        db.session.commit()
        target_title = (
            title
            if target.id == movie.id
            else (
                f"{target.tmdb_title if target.tmdb_title else target.title} "
                f"({target.tmdb_release_date.strftime('%Y') if target.tmdb_title and target.tmdb_release_date else target.year})"
            )
        )
        if rating is not None:
            # The same last-response state the rating drive keeps: a
            # positive rating earns the "since you liked…" strip on the
            # redirect back here, and steers the drive's next card too.
            # Rating a suggestion doesn't move the anchor — the strip
            # refreshes in place with the rated film gone — and neither
            # does a poster-card rating (#45c)
            if target.id == movie.id and not request.form.get("from_card"):
                set_last_response(
                    current_app.redis,
                    current_user.id,
                    movie.id,
                    "rated",
                    positive=rating >= 3,
                )
            _enqueue_profile_recompute()
            if not _ladder_fetch():
                flash(f"Rated '{target_title}' {rating:g} out of 5 stars", "success")
        elif is_review:
            flash(f"Logged review for '{title}'", "success")
        else:
            flash(f"Logged '{title}' in your history", "success")
        if _ladder_fetch():
            return _ladder_state(current_user.id, target.id)
        return redirect(url_for("main.movie", movie_id=movie.id))

    # Watchlist toggle: adds only make sense for films with no local
    # copy (the funnel stage before the shopping list), but removal is
    # offered whenever the film is on the list — even after acquiring it

    watchlist_form = WatchlistForm()
    on_watchlist = (
        UserWatchlist.query.filter_by(
            user_id=int(current_user.id), movie_id=movie.id
        ).first()
        is not None
    )
    if watchlist_form.add_watchlist_submit.data and watchlist_form.validate_on_submit():
        # A movie_id in the form banks a film from the suggestion strip;
        # without one, the toggle adds THIS film. Banking doesn't touch
        # the last-response state, so the strip stays anchored and the
        # banked film simply drops out of it
        target_id = watchlist_form.movie_id.data or movie.id
        target = db.session.get(Movie, target_id) or movie
        if not UserWatchlist.query.filter_by(
            user_id=int(current_user.id), movie_id=target.id
        ).first():
            db.session.add(UserWatchlist(user_id=current_user.id, movie_id=target.id))
            db.session.commit()
        if _card_fetch():
            return jsonify({"on_watchlist": True})
        target_title = (
            f"{target.tmdb_title if target.tmdb_title else target.title} "
            f"({target.tmdb_release_date.strftime('%Y') if target.tmdb_title and target.tmdb_release_date else target.year})"
        )
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

    # Not-interested toggle (#45b): waves an unowned film off every
    # recommendation surface without fabricating a diary row — owned
    # films use the ladder's zero stars instead. Marking clears any
    # watchlist entry (the two contradict), and both directions nudge
    # the profile recompute since the weights changed

    not_interested_form = NotInterestedForm()
    refused = (
        UserMovieStatus.query.filter_by(
            user_id=int(current_user.id), movie_id=movie.id, kind="not_interested"
        ).first()
        is not None
    )
    if (
        not_interested_form.not_interested_submit.data
        and not_interested_form.validate_on_submit()
    ):
        if _mark_not_interested(current_user.id, movie.id):
            _enqueue_profile_recompute()
            flash(f"Got it — '{title}' won't be recommended", "info")
        else:
            flash(
                f"You've logged '{title}' — the lowest rating for a "
                f"seen film is 1 star",
                "warning",
            )
        return redirect(url_for("main.movie", movie_id=movie.id))
    if (
        not_interested_form.interested_submit.data
        and not_interested_form.validate_on_submit()
    ):
        UserMovieStatus.query.filter_by(
            user_id=int(current_user.id), movie_id=movie.id, kind="not_interested"
        ).delete()
        db.session.commit()
        _enqueue_profile_recompute()
        flash(f"'{title}' can be recommended again", "success")
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

    # The "since you liked…" strip renders when the session's last
    # positive rating was for THIS film — right after the log's
    # redirect lands back here (review_tmdb logs land here too)

    anchor_id, suggested_ids = suggestions_after_rating(current_user.id)
    suggestions = []
    if anchor_id == movie.id and suggested_ids:
        suggested_movies = {
            m.id: m for m in Movie.query.filter(Movie.id.in_(suggested_ids))
        }
        suggestions = [
            suggested_movies[movie_id]
            for movie_id in suggested_ids
            if movie_id in suggested_movies
        ]

    # The personal funnel badge state: "Seen" is any diary row of the
    # current user's (review is their latest); "Might interest you"
    # never shows on a seen film — its watch already feeds the taste
    # profile. Owned films badge when the nightly recompute ranked them
    # in the stored recommendations; unowned records score through the
    # coarse scorer against the profile-relative bar, like the TMDb
    # search results

    # The estimated rating (#45a): the shared score source — the stored
    # map, live-scoring a missing film with the same recipe and patching
    # the result back so every surface shows one number (Glenn's ask —
    # a low guess warns off a watchlist add as usefully as a high one
    # invites it), and the profile's calibration curve turns the score
    # into "you might rate this around ★★★★" — shown until the user's
    # own STARS exist, so a bare unrated watch still previews the guess

    estimated = None
    might_interest = False
    profile = stored_profile(current_app.redis, current_user.id)
    if (review is None or review.rating is None) and not refused:
        score = resolved_score(current_app.redis, current_user.id, movie, profile)
        if score is not None:
            estimated = estimated_rating(profile, score)

    # "Might interest you" keeps the stricter diary rule: any watch —
    # rated or not — already feeds the profile, so seen films never badge

    if review is None and not refused:
        if films:
            might_interest = movie.id in recommended_movie_ids(
                current_app.redis, current_user.id
            )
        elif profile:
            year = (
                movie.tmdb_release_date.year if movie.tmdb_release_date else movie.year
            )
            coarse = coarse_interest_score(
                profile, [genre.id for genre in movie.genres], year
            )
            might_interest = coarse > marker_bar(profile)

    # The ad-hoc Radarr hand-off (#66): admins can request an unowned
    # film for download; the badge reads from the hour-cached id set

    in_radarr = bool(
        current_user.admin
        and not films
        and movie.tmdb_id
        and radarr_configured()
        and movie.tmdb_id in radarr_tmdb_ids()
    )

    return render_template(
        "movie.html",
        title=title,
        movie=movie,
        cast=cast,
        directors=directors,
        genres=genres,
        awards=awards,
        review=review,
        films=films,
        radarr_form=RadarrForm(),
        radarr_available=radarr_configured(),
        in_radarr=in_radarr,
        features=features,
        movie_shopping_exclude_form=movie_shopping_exclude_form,
        movie_review_form=movie_review_form,
        transcode_form=transcode_form,
        tmdb_lookup_form=tmdb_lookup_form,
        criterion_form=criterion_form,
        streaming=streaming,
        watchlist_form=watchlist_form,
        on_watchlist=on_watchlist,
        might_interest=might_interest,
        estimated_rating=estimated,
        not_interested_form=not_interested_form,
        refused=refused,
        suggestions=suggestions,
        radarr_proxy_url=current_app.config["RADARR_PROXY_URL"],
        plex_playable=(
            bool(films)
            and current_user.plex_player_configured
            and remote_playback_configured()
        ),
    )


@bp.route("/movie/<int:movie_id>/play", methods=["POST"])
@login_required
def movie_play(movie_id):
    """Start this movie on the current user's own playback device
    (their Profile-page setting) via Plex Companion. Background posts
    (the play buttons) get JSON; a plain form post falls back to
    flash-and-redirect."""

    movie = Movie.query.filter_by(id=movie_id).first_or_404()
    ok, message = play_movie(movie, current_user)
    if request.headers.get("X-Requested-With") == "play":
        return jsonify({"ok": ok, "message": message}), 200 if ok else 502
    flash(message)
    return redirect(url_for("main.movie", movie_id=movie.id))


@bp.route("/people")
@login_required
def people():
    """Browse every credited person across the library's films.

    Cast and key crew roles both count (Glenn's #27 call: only key
    roles join the film-count ordering, so day players still register
    but grips don't outrank directors). Defaults to people appearing in
    multiple films, since the long tail is one-appearance day players;
    searching by name widens to everyone, and uncredited-only roles
    never count toward the filter (Glenn's spec from GitHub #13). Each
    person links to their filmography page.
    """

    page = request.args.get("page", 1, type=int)
    query_text = (request.args.get("q") or "").strip()
    minimum_films = 1 if query_text else 2

    # Cast by default (Glenn's call, Aug 2026): the acting long tail is
    # what browsing usually wants, with crew or everyone a click away.
    # Film counts follow the filter — a director's count under "cast"
    # is their acting appearances

    role = request.args.get("role", "cast")
    if role not in ("cast", "crew", "all"):
        role = "cast"

    pairs = _credited_film_pairs(role)
    film_count = db.func.count(db.distinct(pairs.c.movie_id)).label("film_count")
    people_query = (
        db.session.query(
            TMDBCredit.id,
            TMDBCredit.name,
            TMDBCredit.tmdb_profile_path,
            film_count,
        )
        .join(pairs, pairs.c.credit_id == TMDBCredit.id)
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

    role_param = role if role != "cast" else None
    return render_template(
        "people.html",
        title="People",
        people=people_page.items,
        roles=_dominant_roles([person.id for person in people_page.items]),
        pages=people_page,
        query_text=query_text,
        role=role,
        role_param=role_param,
        next_url=(
            url_for(
                "main.people",
                page=people_page.next_num,
                q=query_text or None,
                role=role_param,
            )
            if people_page.has_next
            else None
        ),
        prev_url=(
            url_for(
                "main.people",
                page=people_page.prev_num,
                q=query_text or None,
                role=role_param,
            )
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
        derived_paths = []

        from app.transcodes import derived_paths_for, purge_derived_paths

        try:
            files = File.query.filter(File.series_id == series_id).all()
            for file in files:
                if file.aws_untouched_key:
                    aws_untouched_keys.append(file.aws_untouched_key)
                derived_paths += derived_paths_for(file)
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
            if untouched_key_still_claimed(aws_untouched_key):
                continue
            current_app.request_queue.enqueue(
                "app.videos.aws_delete",
                args=(aws_untouched_key,),
                job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
            )

        purge_derived_paths(derived_paths)

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

    # The billed cast for the scroller, in aggregate billing order (#78).
    # Capped: a long-running series' aggregate cast can run to hundreds
    # of one-episode guest roles that would bloat the page for no gain

    cast = [
        {
            "id": role.credit_id,
            "name": role.starring.name,
            "profile_path": role.starring.tmdb_profile_path,
            "character": role.character,
        }
        for role in tv.cast.order_by(
            TVCast.billing_order.asc(), TVCast.episode_count.desc()
        )
        .limit(100)
        .all()
    ]

    # The movie page's meta line, in TV terms: run of years, size when
    # the run is complete (the apply stores counts only for Ended
    # shows), genres

    meta_bits = []
    if tv.tmdb_first_air_date:
        years = f"{tv.tmdb_first_air_date.year}"
        if (
            tv.tmdb_last_air_date
            and tv.tmdb_last_air_date.year != tv.tmdb_first_air_date.year
        ):
            years += f"–{tv.tmdb_last_air_date.year}"
        meta_bits.append(years)
    if tv.tmdb_number_of_seasons:
        meta_bits.append(
            f"{tv.tmdb_number_of_seasons} seasons, "
            f"{tv.tmdb_number_of_episodes} episodes"
        )
    genre_names = ", ".join(genre.name for genre in tv.genres)
    if genre_names:
        meta_bits.append(genre_names)

    return render_template(
        "tv.html",
        title=title,
        tv=tv,
        seasons=seasons,
        cast=cast,
        meta_line=" · ".join(meta_bits),
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

    # Episode metadata for the guide and the files table's title column
    # (#78 step 6) — withheld entirely for numbering-suspect series,
    # where a title is likelier to mislabel than to inform. File
    # editions outrank fetched titles in the template.

    from app.tv_validation import series_is_suspect

    episodes = {}
    episode_guide = []
    if not series_is_suspect(series_id):
        episodes = {
            row.episode: row
            for row in TVEpisode.query.filter_by(
                series_id=series_id, season=season
            ).all()
        }
        episode_guide = sorted(episodes.values(), key=lambda row: row.episode)

    owned_episodes = set()
    for file, _, _, _ in files:
        owned_episodes.update(
            range(file.episode, (file.last_episode or file.episode) + 1)
        )

    return render_template(
        "season.html",
        title=title,
        tv=tv,
        season=season,
        files=files,
        episodes=episodes,
        episode_guide=episode_guide,
        owned_episodes=owned_episodes,
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

        # The file's transcoded copies go with it (#19): paths noted
        # before the delete (the rows cascade away with the File),
        # removed only after the commit — same posture as the AWS key

        from app.transcodes import derived_paths_for, purge_derived_paths

        derived_paths = derived_paths_for(file)

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

        if aws_untouched_key and not untouched_key_still_claimed(aws_untouched_key):
            current_app.request_queue.enqueue(
                "app.videos.aws_delete",
                args=(aws_untouched_key,),
                job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
            )

        purge_derived_paths(derived_paths)

        flash(f"Deleted '{file.basename}' and removed from database.", "success")

        if file.movie_id:
            return redirect(url_for("main.movie_files", movie_id=file.movie_id))

        elif file.series_id and file.season:
            return redirect(
                url_for("main.season", series_id=file.series_id, season=file.season)
            )

        else:
            return redirect(url_for("main.index"))

    # The per-file triage link (#72) only exists while this file has
    # pending possibly-forced tracks (and only admins can act on them)

    pending_subtitle_triage = bool(
        current_user.admin and forced_subtitle_candidates(file_id=file.id)
    )

    return render_template(
        "file.html",
        file=file,
        title=title,
        movie=movie,
        tv=tv,
        audio_tracks=audio_tracks,
        subtitle_tracks=subtitle_tracks,
        pending_subtitle_triage=pending_subtitle_triage,
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


@bp.route("/about")
def about():
    """Show general information about the Fitzflix application."""

    return render_template("about.html")


# Tie-break order for a person's dominant role: a director-actor
# reads as Director, a bit-part-everything reads as Actor before the
# narrower crafts

ROLE_PRECEDENCE = (
    "Director",
    "Actor",
    "Cinematographer",
    "Composer",
    "Writer",
    "Editor",
)


def _credited_film_pairs(role="all"):
    """(credit_id, movie_id) pairs the people surfaces count: credited
    cast rows, key crew roles, or their deduplicated union — movies and
    TV series both (#78 step 6). A TV series rides the movie_id column
    as its NEGATED id, keeping the distinct-count space collision-free
    without a discriminator column; nothing joins these ids back to a
    table, they are only ever counted.

    The search paths always count everything ("all"); the /people page
    passes its cast/crew filter through so work counts reflect the
    selected credit type.
    """

    cast_pairs = db.session.query(
        MovieCast.credit_id.label("credit_id"),
        MovieCast.movie_id.label("movie_id"),
    ).filter(
        db.or_(
            MovieCast.character == None,
            db.not_(MovieCast.character.like("%(uncredited)%")),
        )
    )
    tv_cast_pairs = db.session.query(
        TVCast.credit_id.label("credit_id"),
        (-TVCast.tv_id).label("movie_id"),
    ).filter(
        db.or_(
            TVCast.character == None,
            db.not_(TVCast.character.like("%(uncredited)%")),
        )
    )
    crew_pairs = db.session.query(
        MovieCrew.credit_id.label("credit_id"),
        MovieCrew.movie_id.label("movie_id"),
    ).filter(MovieCrew.job.in_(list(CREW_ROLE_LABELS)))
    tv_crew_pairs = db.session.query(
        TVCrew.credit_id.label("credit_id"),
        (-TVCrew.tv_id).label("movie_id"),
    ).filter(TVCrew.job.in_(list(CREW_ROLE_LABELS)))
    if role == "cast":
        return cast_pairs.union(tv_cast_pairs).subquery()
    if role == "crew":
        return crew_pairs.union(tv_crew_pairs).subquery()
    return cast_pairs.union(tv_cast_pairs, crew_pairs, tv_crew_pairs).subquery()


def _dominant_roles(credit_ids):
    """Each person's dominant credited role — the key role covering the
    most distinct library works (films and series both), ties broken by
    ROLE_PRECEDENCE."""

    if not credit_ids:
        return {}
    counts = {}
    for credit_id, tally in (
        db.session.query(TVCast.credit_id, db.func.count(db.distinct(TVCast.tv_id)))
        .filter(TVCast.credit_id.in_(credit_ids))
        .filter(
            db.or_(
                TVCast.character == None,
                db.not_(TVCast.character.like("%(uncredited)%")),
            )
        )
        .group_by(TVCast.credit_id)
    ):
        role_counts = counts.setdefault(credit_id, {})
        role_counts["Actor"] = role_counts.get("Actor", 0) + tally
    for credit_id, job, tally in (
        db.session.query(
            TVCrew.credit_id, TVCrew.job, db.func.count(db.distinct(TVCrew.tv_id))
        )
        .filter(TVCrew.credit_id.in_(credit_ids))
        .filter(TVCrew.job.in_(list(CREW_ROLE_LABELS)))
        .group_by(TVCrew.credit_id, TVCrew.job)
    ):
        label = CREW_ROLE_LABELS[job]
        role_counts = counts.setdefault(credit_id, {})
        role_counts[label] = role_counts.get(label, 0) + tally
    for credit_id, tally in (
        db.session.query(
            MovieCast.credit_id, db.func.count(db.distinct(MovieCast.movie_id))
        )
        .filter(MovieCast.credit_id.in_(credit_ids))
        .filter(
            db.or_(
                MovieCast.character == None,
                db.not_(MovieCast.character.like("%(uncredited)%")),
            )
        )
        .group_by(MovieCast.credit_id)
    ):
        role_counts = counts.setdefault(credit_id, {})
        role_counts["Actor"] = role_counts.get("Actor", 0) + tally
    for credit_id, job, tally in (
        db.session.query(
            MovieCrew.credit_id,
            MovieCrew.job,
            db.func.count(db.distinct(MovieCrew.movie_id)),
        )
        .filter(MovieCrew.credit_id.in_(credit_ids))
        .filter(MovieCrew.job.in_(list(CREW_ROLE_LABELS)))
        .group_by(MovieCrew.credit_id, MovieCrew.job)
    ):
        label = CREW_ROLE_LABELS[job]
        role_counts = counts.setdefault(credit_id, {})
        role_counts[label] = role_counts.get(label, 0) + tally
    return {
        credit_id: max(
            role_counts,
            key=lambda role: (role_counts[role], -ROLE_PRECEDENCE.index(role)),
        )
        for credit_id, role_counts in counts.items()
    }
