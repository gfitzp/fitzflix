"""The library pages (the routes.py split).

These are the movie and TV browsing pages, the filmographies, the
Criterion spine catalog, the people page, and the per-title
movie/tv/season/file detail pages."""

import json
import os
import re
import time
import traceback


from datetime import date, datetime
from types import SimpleNamespace

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

# Flask 2.4 removed flask.Markup. Import it from its actual home.
from markupsafe import Markup
from flask_login import current_user, login_required
from flask_sqlalchemy.pagination import Pagination

from app import db, safe_job_id
from app.main.forms import (
    CriterionForm,
    FileDeleteForm,
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
    TMDBRemoveForm,
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
    PEOPLE_RANKING_KEY,
    TMDBGenre,
    TVCast,
    TVCrew,
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
from app.newly_added import poster_fold
from app.infuse_player import infuse_only_formats
from app.infuse_player import play_movie as infuse_play_movie
from app.plex_player import play_movie, remote_playback_configured
from app.main.helpers import (
    admin_required,
    _card_fetch,
    _enqueue_profile_recompute,
    _ladder_fetch,
    _ladder_state,
    _latest_review_row,
    _mark_not_interested,
    _quick_rating,
    _same_day_rerate,
    _upgrade_threshold,
    library_upgradable,
    tv_meta_line,
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
    lossy_audio_candidates,
)
from app.videos import (
    clear_not_interested,
    clear_watchlist,
    criterion_release_lookups,
    get_criterion_collection_from_wikidata,
    language_names,
    library_language_choices,
    resolve_language_code,
    star_rating_fields,
    track_metadata_scan,
    untouched_key_still_claimed,
)

# The crew jobs that count as key roles for search and filmographies.
# They are the same roles that the taste engine scores, labeled as
# nouns. Decided by Glenn: only these join the film-count ordering.
# Thus, grips and gaffers do not outrank directors.

CREW_ROLE_LABELS = {
    job: role.capitalize() for role, (jobs, _) in CREW_ROLE_JOBS.items() for job in jobs
}


# A multi-role credit line reads in the conventional closing-credit
# order (directed, written, shot, edited, scored), not in the TMDB
# payload order.

CLOSING_CREDIT_ORDER = ("Director", "Writer", "Cinematographer", "Editor", "Composer")


# A TV role where the person appears as themselves. TMDB writes
# these as "Self", "Self - Host", "Herself (archive footage)". The
# self-word starts the line. The pattern is word-bounded. Thus, a real
# character that only contains the letters (Harry Selfridge) survives.

SELF_ROLE = re.compile(r"(?:him|her|them)?sel(?:f|ves)\b", re.IGNORECASE)


def _tmdb_person_details(person_id):
    """Return the name, photo, and biographical fields of the person from TMDB.

    Fitzflix caches the result for 1 day. The result is None when there
    is no API key or TMDB does not answer with a name. The filmography
    treats None as an unknown person.
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
    """Return a date from a TMDB YYYY-MM-DD string, or None if it is absent or odd."""

    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _person_bio(details):
    """Return the formatted born/died lines and the biography of a person.

    The source is a TMDB person-details dict. The filmography header
    shows the result. The age computes against the death date when
    there is one.
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
    - credit: the id of an actor. Fitzflix filters the movie list to the
              films that the actor starred in.
    - q     : a substring. Fitzflix filters the movie list to the films
              that contain it.
    """

    page = request.args.get("page", 1, type=int)
    credit = request.args.get("credit", None, type=int)
    q = request.args.get("q", None, type=str)
    genre = request.args.get("genre", None, type=int)
    quality = request.args.get("quality", "0", type=str)

    # This subquery gets the best movie files.

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
        # Credit ids are TMDB person ids. Thus, the filmography is not
        # limited to people with local credit rows. The user can browse
        # each person that TMDB knows from any cast list. The day-cached
        # TMDB person lookup supplies the biography for each person. A
        # local credit row supplies the name and photo when Fitzflix
        # cannot reach TMDB.

        person = TMDBCredit.query.filter_by(id=int(credit)).first()
        details = _tmdb_person_details(int(credit)) or {}
        person_name = details.get("name") or (person.name if person else None)
        person_profile_path = details.get("profile_path") or (
            person.tmdb_profile_path if person else None
        )
        if person_name is None:
            abort(404)
        bio = _person_bio(details) if details else None

        # The filmography shows the full TMDB career of the person. It
        # does not matter if a film has a local record. The local rows
        # attach the best owned file through an outer join. The rank
        # condition must be in the join, not in the WHERE clause. If not,
        # the WHERE clause would filter away the file-less review-only
        # records. The full credit list comes from TMDB, cached for 1 day.

        best_file_ids = db.session.query(ranked_files.c.id).filter(
            ranked_files.c.rank == 1
        )

        def local_credit_rows(credit_table):
            """Return the local films of the person through a credit join table.

            Each film has its best owned file outer-joined."""

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

        # The best owned copy per movie. A movie can have several rank-1
        # editions. The filmography shows 1 entry per film.

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

        # The full TMDB credit list of the person, cached for 1 day

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

        # The day-cached payloads written before the crew credits joined
        # the filmography are bare cast lists.

        if isinstance(tmdb_credits, list):
            tmdb_credits = {"cast": tmdb_credits, "crew": []}

        # Merge. There is 1 row per film. The TMDB credits come first. They
        # are deduplicated by film, and the characters are combined. Then
        # come the local credits that TMDB did not list.

        local_by_tmdb_id = {
            entry["movie"].tmdb_id: entry
            for entry in local.values()
            if entry["movie"].tmdb_id is not None
        }
        rows = {}

        def credit_row(entry):
            """Return the merged filmography row for a TMDB credit entry.

            This function creates the row on first sight. The cast and
            crew credits for the same film share 1 row."""

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

        # "Might interest you" markers. The unowned films score at render
        # time. The score uses the cached credits payload and the stored
        # taste profile of the user (no TMDB calls, nothing persisted).
        # The owned unwatched films show a badge when the nightly
        # recompute ranked them in the stored recommendations. Thus, the
        # filmographies agree with the library rail and the search pages.

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

        # Streaming badges on the films without a local file, filtered to
        # the services of this user. Fitzflix reads the availability from
        # the cache that the nightly refresh keeps full. It never fetches
        # inline (2026-08). A career can span hundreds of films. Each
        # fetch shares the app-wide TMDB rate limiter. Thus, the page of
        # a prolific actor stalled 4 to 6 seconds behind 50 fetches. The
        # refresh cannot cover the films without a record. Those films
        # warm in the background for the next visit.

        streaming_attribution = False
        provider_ids = user_provider_ids(current_user)
        if provider_ids:
            availability_by_id, deferred = batch_title_availability(
                (
                    row["tmdb_id"]
                    for row in filmography
                    if row["tmdb_id"] and not row["quality"]
                ),
                fetch_limit=0,
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
                matches = streaming_matches(
                    availability, provider_ids, tmdb_id=row["tmdb_id"]
                )
                rentals = rental_matches(availability, provider_ids)
                if matches:
                    row["streaming"] = matches
                if rentals:
                    row["rentals"] = rentals
                if matches or rentals:
                    streaming_attribution = True

        # Television credits. This is the TMDB TV career of the person,
        # 1 row per series, cached for 1 day like the film list. Self
        # appearances are dropped. The talk-show and awards-night rows
        # would swamp the acting credits (the key-roles-only spirit).
        # Owned series link to their pages. TV has no review flow. Thus,
        # the unowned rows render without a link (the TMDB-row rule).

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
        title = f"Library movies matching '{q}'"
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
        # The genre links on the movie pages go here. The library is
        # filtered to the films that carry the TMDB genre. The quality
        # dropdown can combine with it.

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
        upgrade_threshold=_upgrade_threshold(),
    )


CRITERION_CHANNEL_PROVIDER_ID = 258


CRITERION_CATALOG_PER_PAGE = 120

# The cache life of the /people ranking. This is a backstop. The
# credit writers invalidate the cache directly.
PEOPLE_RANKING_SECONDS = 7 * 86400


def _page_window(current, last):
    """Return the page numbers for a pagination bar, with None as a gap.

    The shape is the same as the iter_pages output of Flask-SQLAlchemy
    on the people page. It has the first 2 and last 2 pages, a window
    around the current page, and ellipses between them.
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
    """Show the full Criterion Collection spine catalog, in and beyond the library.

    Each release from the Wikidata spine cache renders, not only the
    films of the library. The owned films keep their settled/amber
    verdicts. The releases that the library does not have render like
    TMDB search rows. Their row opens the log page. Thus, the user can
    add them to the watchlist. The small number of releases without a
    TMDB id in Wikidata list as plain spine rows. A Criterion Channel
    badge marks what streams there now.
    """

    filter_status = request.args.get("filter", "all")
    if filter_status not in ("all", "library", "settled"):
        filter_status = "all"
    page = max(request.args.get("page", 1, type=int) or 1, 1)

    # The whole spine catalog from the weekly Wikidata cache. The page
    # degrades to library-only rows if the cache is cold and Fitzflix
    # cannot reach Wikidata.

    try:
        releases = get_criterion_collection_from_wikidata()
    except Exception:
        current_app.logger.warning(traceback.format_exc())
        releases = []
    by_tmdb_id, by_title_year = criterion_release_lookups(releases)
    release_tmdb_ids = [
        release["tmdb_id"] for release in releases if release.get("tmdb_id")
    ]

    # Library rows. These are the best main-feature file per film, for
    # the films with Criterion metadata OR a release match by TMDB id.
    # A film whose record is older than its release was never marked.
    # But the catalog knows its spine.

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

    # A library row is SETTLED when the Criterion disc is owned AND the
    # local file matches the format of the release. Settled means the
    # Fitzflix library badge and nothing to do. The bar has a MAXIMUM at
    # the app-wide threshold. Decided by Glenn: an owned disc with a
    # Bluray-1080p file is settled here, even if Criterion released it
    # again in 2160p. That upgrade is the job of the shopping list, not
    # of this page. The threshold also covers the releases whose quality
    # was never recorded. Each other row shows its amber quality tier.
    # This means: go find the Criterion version.

    movie_ids = [movie.id for _, movie, _ in results]
    CriterionQuality = db.aliased(RefQuality)
    criterion_prefs = dict(
        db.session.query(Movie.id, CriterionQuality.preference)
        .join(CriterionQuality, CriterionQuality.id == Movie.criterion_quality_id)
        .filter(Movie.id.in_(movie_ids or [0]))
    )
    threshold = _upgrade_threshold()

    # Each library film consumes its catalog release. The match is by
    # TMDB id first, then by title+year. This is the matching order of
    # the import. Thus, the rest renders as beyond-the-library rows. A
    # film with a standalone release and a set membership consumes both
    # through its shared TMDB id.

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

    # The rest of the catalog. The standalone entries come before the
    # set entries in the cache. Thus, a film with both keeps its own
    # spine. The releases without a TMDB id render as plain spine rows.
    # Box-set CONTAINER items are redundant. Wikidata gives the spine to
    # the set item, and no TMDB id (TMDB has no set entries). The member
    # films arrive separately and show the set title. Thus, a row
    # without a TMDB id, whose spine belongs to a set, would only shadow
    # its own members ("#88 Ivan the Terrible" between the actual Parts
    # I–III).

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
        # Hand-excluded ids (Wikidata junk, see CatalogExclusion) do not
        # render, and Fitzflix creates no records for them.
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

    # The file-less local records (logged or watchlisted unowned films)
    # give their catalog rows the stored title, poster, and overview.
    # They also carry the funnel badges.

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

    # The personal funnel. It is per-user, like at each other place. A
    # seen film never shows the might-interest badge. An owned film
    # shows the badge when it is in the stored ranking. A catalog row
    # with a refreshed record scores through the coarse scorer against
    # the profile-relative bar. A row without a record has no genres to
    # score. It stays unmarked.

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

    # The catalog scorer reads the genres of each record. This is 1 query
    # for all of them, not a lazy load per film (956 queries per visit
    # before 2026-08).

    genre_ids_by_movie = {}
    if profile is not None and records:
        for movie_id, genre_id in db.session.query(
            movie_genres.c.movie_id, movie_genres.c.genre_id
        ).filter(
            movie_genres.c.movie_id.in_([record.id for record in records.values()])
        ):
            genre_ids_by_movie.setdefault(movie_id, []).append(genre_id)

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
            genre_ids = genre_ids_by_movie.get(record.id, [])
            if genre_ids:
                score = coarse_interest_score(profile, genre_ids, row["year"])
                row["might_interest"] = score > bar

    # One spine order across owned and unowned rows. The set members
    # sort at the spine of their set (year, then title inside the set).
    # The local rows without a spine keep their old place at the end.

    def sort_key(row):
        """Return the spine order key, with set members at the number of their set."""

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
    # page only. The availability comes from the cache that the nightly
    # refresh keeps full. It is never fetched inline (2026-08, a page of
    # misses cost 4 seconds behind the rate limiter). The catalog films
    # without a record warm in the background for the next visit.

    streaming_attribution = False
    availability_by_id, deferred = batch_title_availability(
        (row["tmdb_id"] for row in rows if row["tmdb_id"]),
        fetch_limit=0,
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
        # The rows with a local file skip the leaving/newly-added
        # annotations. The copy on the shelf stays.
        matches = streaming_matches(
            availability_by_id.get(row["tmdb_id"]),
            {CRITERION_CHANNEL_PROVIDER_ID},
            tmdb_id=None if row.get("quality") else row["tmdb_id"],
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

    # The form to review a movie. A user can review the same movie many
    # times (tastes change). Thus, this only adds one more review to the
    # UserMovieReview table for this film.

    # The date field starts BLANK. The default log has no date ("seen at
    # some time, unknown when"). Plex supplies real timestamps for the
    # watches that it sees. The field is there for the times when the
    # date is known.

    movie_review_form = MovieReviewForm()
    quick_present, quick_rating = _quick_rating()
    if (
        movie_review_form.review_submit.data or quick_present
    ) and movie_review_form.validate_on_submit():
        if quick_present and quick_rating is None:
            flash("That rating is not valid.", "warning")
            return redirect(url_for("main.movie", movie_id=movie.id))
        # The ladder is the only rating input. Log Watch without a tap
        # is a bare diary entry. 3 or more stars set the liked flag. The
        # date and the review text submit as they are in both cases. A
        # tap on a SUGGESTION card carries the movie_id of that film and
        # rates it (without a date). The strip stays anchored to this
        # page.

        rating = quick_rating
        target = movie
        if quick_present:
            form_movie_id = (request.form.get("movie_id") or "").strip()
            if form_movie_id.isdigit() and int(form_movie_id) != movie.id:
                target = db.session.get(Movie, int(form_movie_id)) or movie

        # ✕ is "not interested, never saw it". It is a status flag, never
        # a review. The film leaves each recommendation surface. A seen
        # film cannot get the flag (its floor is 1 star). A tap on a lit
        # ✕ removes the flag.

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
                # A poster-card post (#45c) never steers the drive. Only
                # the own page of a film (and the featured card) moves
                # the last-response state.
                if target.id == movie.id and not request.form.get("from_card"):
                    set_last_response(
                        current_app.redis, current_user.id, movie.id, "not_interested"
                    )
                _enqueue_profile_recompute()
                if not _ladder_fetch():
                    flash(f"Fitzflix will not recommend '{target_title}'", "info")
            elif not _ladder_fetch():
                flash(
                    f"You logged '{target_title}'. The lowest rating "
                    f"for a seen film is 1 star",
                    "warning",
                )
            if _ladder_fetch():
                return _ladder_state(current_user.id, target.id)
            return redirect(url_for("main.movie", movie_id=movie.id))

        # A tap on your current rating removes it. A bare drive-style
        # row (no watch date, no text) is deleted completely. A viewing
        # with real history only loses its stars.

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

        # A different star on a day that you already reviewed corrects
        # that review in place. Tastes change, but not 2 times in a day.
        # A form with text or a watch date is a real new log. It skips
        # this.

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

        # A bare submission (no rating or text) is a plain diary entry.
        # It is a watch, not a review. Thus, it has no review date. The
        # rewatch flag computes in the same way as for Plex watches. Any
        # earlier row for this user and film makes this a repeat viewing.

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
            # This is the same last-response state that the rating drive
            # keeps. A positive rating shows the "since you liked…" strip
            # on the redirect back here. It also steers the next card of
            # the drive. A rating of a suggestion does not move the
            # anchor. The strip refreshes in place without the rated
            # film. A poster-card rating (#45c) does not move the anchor
            # either.
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

    transcode_form = TranscodeForm()

    # The Movie Data forms are admin tools (#186 follow-up). The section
    # renders only for admins. Each branch rejects a stray post from a
    # different user. It does not process the post silently.

    # The form to detach a movie from TMDB completely, for a film that
    # TMDB has no entry for (#207). A record with no local files can
    # never be such a film. Fitzflix created it FROM a TMDB id, and it
    # mirrors that entry. Thus, a detach would leave a husk that nothing
    # can enrich again.

    tmdb_remove_form = TMDBRemoveForm()
    if tmdb_remove_form.remove_submit.data and tmdb_remove_form.validate_on_submit():
        if not current_user.admin:
            flash("Need to be an admin user to do that!", "danger")
            return redirect(url_for("main.movie", movie_id=movie.id))
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
    if tmdb_lookup_form.lookup_submit.data and tmdb_lookup_form.validate_on_submit():
        if not current_user.admin:
            flash("Need to be an admin user to do that!", "danger")
            return redirect(url_for("main.movie", movie_id=movie.id))

        # A record with no local files IS its TMDB entry. The id came
        # directly from TMDB at creation, and the diary rows depend on
        # it. Thus, the only operation is a refresh of the stored id.
        # Never point it to a different id. Fitzflix ignores a smuggled
        # id in the post.

        if not films and not features:
            if not movie.tmdb_id:
                flash(
                    f"'{title}' is not matched to TMDB. There is nothing "
                    f"to refresh",
                    "warning",
                )
                return redirect(url_for("main.movie", movie_id=movie.id))
            refresh_job = current_app.sql_queue.enqueue(
                "app.videos.refresh_tmdb_info",
                args=("Movies", movie.id, movie.tmdb_id),
                job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
                description=f"Refreshing TMDB data for '{movie.title} ({movie.year})'",
                at_front=True,
            )
            waited_seconds = 0
            while refresh_job.result == None and waited_seconds < 10:
                time.sleep(1)
                waited_seconds = waited_seconds + 1
            if refresh_job.result:
                flash(
                    f"Refreshed TMDB data for '{movie.title} ({movie.year})'",
                    "success",
                )
            else:
                flash(
                    Markup(
                        "Fitzflix is refreshing TMDB data for '{}' ({}). <a href='{}'>Reload this page</a>"
                    ).format(
                        movie.title,
                        movie.year,
                        url_for("main.movie", movie_id=movie.id),
                    ),
                    "info",
                )
            return redirect(url_for("main.movie", movie_id=movie.id))

        # A blank id asks TMDB to search by title, and takes the first
        # hit. On a film that is already matched, this silently points
        # the film at a different movie. Offer that only for a film with
        # nothing to lose. A detached film has something to lose: the
        # detachment itself. The detachment promised "not until you
        # enter one by hand".

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
        # previous removal. Only an actual id clears the flag. A blank
        # submit cannot get here on a detached record, because of the
        # guard.

        if tmdb_lookup_form.tmdb_id.data is not None:
            movie.tmdb_ignored = False
            db.session.commit()

        # Add a task to the fitzflix-sql queue to check TMDB and update the database.
        # Add it to the front of the queue, because the user added it interactively.

        refresh_job = current_app.sql_queue.enqueue(
            "app.videos.refresh_tmdb_info",
            args=("Movies", movie.id, tmdb_lookup_form.tmdb_id.data),
            job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
            description=f"Refreshing TMDB data for '{movie.title} ({movie.year})'",
            at_front=True,
        )

        # Check if the requested TMDB ID already exists in the database.
        # If it does, redirect to the info page of that movie, because this
        # movie gets the TMDB data of that movie.

        existing_tmdb_movie = Movie.query.filter_by(
            tmdb_id=tmdb_lookup_form.tmdb_id.data
        ).first()
        if existing_tmdb_movie:
            movie_id = existing_tmdb_movie.id

        else:
            movie_id = movie.id

        # Check the status of the refresh job each second. If the TMDB refresh
        # completes in 10 seconds, redirect to the updated page. If not, redirect
        # to the existing page and give the user a link to reload the page.

        waited_seconds = 0
        while refresh_job.result == None and waited_seconds < 10:
            time.sleep(1)
            waited_seconds = waited_seconds + 1

        if refresh_job.result:
            flash(f"Refreshed TMDB data for '{movie.title} ({movie.year})'", "success")

        else:
            flash(
                Markup(
                    "Fitzflix is refreshing TMDB data for '{}' ({}). <a href='{}'>Reload this page</a>"
                ).format(
                    movie.title, movie.year, url_for("main.movie", movie_id=movie_id)
                ),
                "info",
            )

        return redirect(url_for("main.movie", movie_id=movie_id))

    # The form to update the Criterion Collection information of a movie by hand
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
        if not current_user.admin:
            flash("Need to be an admin user to do that!", "danger")
            return redirect(url_for("main.movie", movie_id=movie.id))
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
    # here too).

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
    might_interest = False
    profile = stored_profile(current_app.redis, current_user.id)
    if (review is None or review.rating is None) and not refused:
        score = resolved_score(current_app.redis, current_user.id, movie, profile)
        if score is not None:
            estimated = estimated_rating(profile, score)

    # "Might interest you" keeps the stricter diary rule. Any watch, rated
    # or not, already feeds the profile. Thus, a seen film never shows the
    # badge.

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

    # The ad-hoc Radarr hand-off. An admin can request an unowned film
    # for download. The badge reads from the hour-cached id set.

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
        poster_fold=poster_fold(current_user, movie.tmdb_id, movie.id),
        cast=cast,
        directors=directors,
        genres=genres,
        # The US rating of the meta line. It is the same answer that the
        # popover card gives. Thus, the page and the popup read the same.
        certification=next(
            (
                c.certification
                for c in movie.certifications
                if c.country == "US" and c.certification
            ),
            None,
        ),
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
        tmdb_remove_form=tmdb_remove_form,
        criterion_form=criterion_form,
        streaming=streaming,
        watchlist_form=watchlist_form,
        on_watchlist=on_watchlist,
        might_interest=might_interest,
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
                        "message": "This page is stale — reload it and try again.",
                    }
                ),
                400,
            )
        flash("That page had gone stale — please try again.", "warning")
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


@bp.route("/people")
@login_required
def people():
    """Browse each credited person across the films of the library.

    Cast and key crew roles both count. Decided by Glenn: only the key
    roles join the film-count ordering. Thus, day players still
    register, but grips do not outrank directors. The default shows the
    people that appear in more than 1 film, because the long tail is
    one-appearance day players. A search by name widens the list to all
    people. Uncredited-only roles never count toward the filter (spec by
    Glenn, GitHub #13). Each person links to their filmography page.
    """

    page = request.args.get("page", 1, type=int)
    query_text = (request.args.get("q") or "").strip()
    minimum_films = 1 if query_text else 2

    # Cast by default (decided by Glenn, 2026-08). The acting long tail
    # is what a browse usually wants. Crew or all people are 1 click
    # away. The film counts follow the filter. The count of a director
    # under "cast" is their acting appearances.

    role = request.args.get("role", "cast")
    if role not in ("cast", "crew", "all"):
        role = "cast"

    # The browse path (no search) pages through the cached ranking.
    # Before 2026-08, the full aggregation over the cast and crew tables
    # ran 2 times per visit. That was 1 time for the page and 1 time for
    # the count of paginate, at half a second each. A search still
    # queries live.
    # The name filter narrows it. The single-title people that it admits
    # are not in the ranking.

    if query_text:
        people_page = _ranked_people_query(role, query_text, minimum_films).paginate(
            page=page, per_page=120, error_out=False
        )
    else:
        people_page = ListPagination(
            _ranked_people(role), page=page, per_page=120, error_out=False
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
@admin_required
def movie_files(movie_id):
    """Show all files for a particular movie, regardless of ranking.

    This is an admin page (#186 follow-up). Each row links into the
    file management pages. Those pages are admin tools."""

    movie = Movie.query.filter_by(id=movie_id).first_or_404()
    title = f"Files for \"{movie.tmdb_title if movie.tmdb_title else movie.title} ({movie.tmdb_release_date.strftime('%Y') if movie.tmdb_title else movie.year})\""

    # This subquery gets the ranking of each file of this movie.

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


@bp.route("/library/tv", methods=["GET", "POST"])
@login_required
def tv_library():
    """Show the worst quality in each season for each TV show in the library.

    ?q= narrows the listing inside the TV library only (#210). The
    series list holds only the series whose TITLE matches. The episodes
    whose TMDB titles match render as their own section below. This is
    the grammar of the main search page. A matched episode never pulls
    its whole series into the series list (Glenn: a search for
    "venture" must not show Bob's Burgers)."""

    # This subquery gets the number of episodes in each season, and the
    # worst quality of each season.

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

    # Run the season aggregate 1 time for the whole library and group the
    # rows by series. Do not run the ranked subquery again for each series.

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
                # A physical-media season (DVD, SD/720p Blu-ray) is often the
                # only release that will ever exist. Thus, it does not count as
                # upgradable.
                "upgradable": not physical and preference < upgrade_threshold,
            }
        )

    q = request.args.get("q", None, type=str)

    # The search box posts and redirects into ?q=, the grammar of the
    # movie library. Thus, a search is a URL that the user can bookmark.

    library_search_form = LibrarySearchForm()
    if library_search_form.validate_on_submit():
        return redirect(
            url_for("main.tv_library", q=library_search_form.search_query.data or None)
        )

    series_query = TVSeries.query.join(File, (File.series_id == TVSeries.id)).distinct()
    if q:
        # Spaces become wildcards, like in the search of the movie library

        wildcard = q.replace(" ", "%")
        series_query = series_query.filter(
            db.or_(
                TVSeries.title.ilike(f"%{wildcard}%"),
                TVSeries.tmdb_name.ilike(f"%{wildcard}%"),
            )
        )

    tv = []
    for series in series_query.order_by(
        db.func.regexp_replace(TVSeries.title, "^(The|A|An) ", "").asc()
    ).all():
        tv.append(
            {
                "id": series.id,
                "title": series.title,
                "tmdb_id": series.tmdb_id,
                "tmdb_name": series.tmdb_name,
                "first_air_year": (
                    series.tmdb_first_air_date.year
                    if series.tmdb_first_air_date
                    else None
                ),
                "tmdb_poster_path": series.tmdb_poster_path,
                "seasons": seasons_by_series.get(series.id, []),
            }
        )

    return render_template(
        "library_tv.html",
        title=f"TV library matches for '{q}'" if q else "TV Library",
        series=tv,
        q=q,
        library_search_form=library_search_form,
    )


def restore_cost_estimate(files, bulk=False):
    """Estimate the AWS cost to restore and download the archived files.

    This function uses the exact size of the archived object when it is
    recorded. If not, it uses the size of the localized copy with a
    1.25x factor, because the archived original is usually larger than
    the localized copy.
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

    # The series management forms (transcode, restore, delete, TMDB)
    # are admin tools, like the Movie Data section of the movie page
    # (#186 follow-up). The template hides them. Each branch rejects a
    # stray post from a different user.

    # The form to request a transcode of all the files of this TV series

    transcode_form = TranscodeForm()
    if transcode_form.transcode_all.data and transcode_form.validate_on_submit():
        if not current_user.admin:
            flash("Need to be an admin user to do that!", "danger")
            return redirect(url_for("main.tv", series_id=tv.id))

        # This subquery gets the best files of this TV series.

        ranked_files = (
            db.session.query(
                File.id,
                tv_file_rank(),
            )
            .join(TVSeries, (TVSeries.id == File.series_id))
            .join(RefQuality, (RefQuality.id == File.quality_id))
            .subquery()
        )

        # Get the details of all the best files of this TV series.

        files = (
            File.query.join(ranked_files, (ranked_files.c.id == File.id))
            .filter(File.series_id == series_id)
            .filter(ranked_files.c.rank == 1)
            .order_by(File.season.asc(), File.episode.asc())
            .all()
        )

        # Enqueue a transcode task for each best file of this TV show.

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

    # The form to request a restore of each archived file of this series
    # from AWS Glacier. The hourly SQS poll downloads each file when it is
    # ready. A restore costs real money. Thus, show an estimate and
    # require the password of the user before the request.

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
        if not current_user.admin:
            flash("Need to be an admin user to do that!", "danger")
            return redirect(url_for("main.tv", series_id=tv.id))
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
        if not current_user.admin:
            flash("Need to be an admin user to do that!", "danger")
            return redirect(url_for("main.tv", series_id=tv.id))
        aws_untouched_keys = []
        derived_paths = []
        leftover_dirs = set()

        from app.transcodes import derived_paths_for, purge_derived_paths

        try:
            files = File.query.filter(File.series_id == series_id).all()
            for file in files:
                if file.aws_untouched_key:
                    aws_untouched_keys.append(file.aws_untouched_key)
                derived_paths += derived_paths_for(file)
                leftover_dirs.add(
                    os.path.join(current_app.config["LIBRARY_DIR"], file.dirname)
                )
                file.delete_local_file(delete_directory_tree=True)
                db.session.delete(file)

            db.session.delete(tv)
            db.session.commit()

        except Exception:
            db.session.rollback()
            flash(f"Unable to delete TV series '{title}'!", "danger")
            return redirect(url_for("main.tv", series_id=series_id))

        # Delete the AWS copies only after the database delete is committed.
        # Thus, a failed commit cannot leave database records whose backups
        # are gone.

        for aws_untouched_key in aws_untouched_keys:
            if untouched_key_still_claimed(aws_untouched_key):
                continue
            current_app.request_queue.enqueue(
                "app.videos.aws_delete",
                args=(aws_untouched_key,),
                job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
            )

        purge_derived_paths(derived_paths)

        # The folders themselves. delete_local_file removes only EMPTY
        # directories. Poster art sits beside the episodes (placed by
        # Fitzflix for movies, by Sonarr or by hand for TV). Thus, that
        # art used to leave husks for the weekly sweep. An explicit
        # delete clears the junk-only folders immediately.

        from app.maintenance import clear_leftover_directory

        for leftover_dir in sorted(leftover_dirs):
            clear_leftover_directory(leftover_dir)

        flash(
            f"Deleted TV series '{title}' and its files from the database.", "success"
        )
        return redirect(url_for("main.tv_library"))

    # The form to detach a series from TMDB completely (#207). It is for
    # a series that TMDB has no entry for, or an id that TMDB deleted
    # later.

    tmdb_remove_form = TMDBRemoveForm()
    if tmdb_remove_form.remove_submit.data and tmdb_remove_form.validate_on_submit():
        if not current_user.admin:
            flash("Need to be an admin user to do that!", "danger")
            return redirect(url_for("main.tv", series_id=tv.id))
        tv.tmdb_tv_clear()
        db.session.commit()
        flash(
            f"Removed the TMDB ID from '{tv.title}'. Fitzflix will not look it "
            f"up again until you enter one by hand",
            "success",
        )
        return redirect(url_for("main.tv", series_id=tv.id))

    # The form to update the information of a TV series with the latest TMDB data

    tmdb_lookup_form = TMDBLookupForm()
    if tmdb_lookup_form.lookup_submit.data and tmdb_lookup_form.validate_on_submit():
        if not current_user.admin:
            flash("Need to be an admin user to do that!", "danger")
            return redirect(url_for("main.tv", series_id=tv.id))

        # A blank id asks TMDB to search by title, and takes the first
        # hit. On a series that is already matched, this silently points
        # the series at a different show. That is the merge reported in
        # #207. A detached series is guarded too. A blank refresh must
        # not silently undo the detachment with a title search.

        if tmdb_lookup_form.tmdb_id.data is None and (
            tv.tmdb_id is not None or tv.tmdb_ignored
        ):
            flash(
                "Enter a TMDB ID to refresh this series, or use "
                "'Remove TMDB ID' to detach it from TMDB",
                "warning",
            )
            return redirect(url_for("main.tv", series_id=tv.id))

        # An id entered by hand is an intentional new match. It undoes a
        # previous removal. Only an actual id clears the flag. A blank
        # submit cannot get here on a detached record, because of the
        # guard.

        if tmdb_lookup_form.tmdb_id.data is not None:
            tv.tmdb_ignored = False
            db.session.commit()

        # Add a task to the fitzflix-sql queue to check TMDB and update the database.
        # Add it to the front of the queue, because the user added it interactively.

        refresh_job = current_app.sql_queue.enqueue(
            "app.videos.refresh_tmdb_info",
            args=("TV Shows", tv.id, tmdb_lookup_form.tmdb_id.data),
            job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
            description=f"Refreshing TMDB data for '{tv.title}'",
            at_front=True,
        )

        # Check if the requested TMDB ID already exists in the database.
        # If it does, redirect to the info page of that show, because this
        # TV series gets the TMDB data of that show.

        existing_tmdb_tv = TVSeries.query.filter_by(
            tmdb_id=tmdb_lookup_form.tmdb_id.data
        ).first()
        if existing_tmdb_tv:
            tv_id = existing_tmdb_tv.id

        else:
            tv_id = tv.id

        # Check the status of the refresh job each second. If the TMDB refresh
        # completes in 10 seconds, redirect to the updated page. If not, redirect
        # to the existing page and give the user a link to reload the page.

        waited_seconds = 0
        while refresh_job.result == None and waited_seconds < 10:
            time.sleep(1)
            waited_seconds = waited_seconds + 1

        if refresh_job.result:
            flash(f"Refreshed TMDB data for '{tv.title}'", "success")

        else:
            flash(
                Markup(
                    "Fitzflix is refreshing TMDB data for '{}'. <a href='{}'>Reload this page</a>"
                ).format(tv.title, url_for("main.tv", series_id=tv_id)),
                "info",
            )

        return redirect(url_for("main.tv", series_id=tv_id))

    # The billed cast for the scroller, in aggregate billing order. The
    # list has a maximum. The aggregate cast of a long-running series
    # can have hundreds of one-episode guest roles. They would bloat the
    # page for no gain.

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

    # The meta line of the movie page, in TV terms. It shows the run of
    # years, the size when the run is complete (the apply stores counts
    # only for Ended shows), and the genres. The shared helper that the
    # popover card reads too builds it.

    return render_template(
        "tv.html",
        title=title,
        tv=tv,
        seasons=seasons,
        cast=cast,
        meta_line=tv_meta_line(
            tv.tmdb_first_air_date.year if tv.tmdb_first_air_date else None,
            tv.tmdb_last_air_date.year if tv.tmdb_last_air_date else None,
            tv.tmdb_number_of_seasons,
            tv.tmdb_number_of_episodes,
            [genre.name for genre in tv.genres],
        ),
        transcode_form=transcode_form,
        series_restore_form=series_restore_form,
        series_restore_estimate=series_restore_estimate,
        tmdb_lookup_form=tmdb_lookup_form,
        tmdb_remove_form=tmdb_remove_form,
        series_delete_form=series_delete_form,
    )


@bp.route("/tv/<int:series_id>/<int:season>", methods=["GET", "POST"])
@login_required
def season(series_id, season):
    """Show all files for a season of a TV show, regardless of ranking.

    The int converters make a non-numeric series or season in the URL
    a 404, and not a ValueError further down.
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

    # This subquery gets the ranking of each file of this season.

    ranked_files = (
        db.session.query(
            File.id,
            tv_file_rank(),
        )
        .join(TVSeries, (TVSeries.id == File.series_id))
        .join(RefQuality, (RefQuality.id == File.quality_id))
        .subquery()
    )

    # This query gets all the files of this season.

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

    # The form to request a restore of the best archived files of this
    # season from AWS Glacier. The hourly SQS poll downloads each file
    # when it is ready. A restore costs real money. Thus, show an estimate
    # and require the password of the user before the request.

    restorable = [
        file for file, _, _, rank in files if rank == 1 and file.aws_untouched_key
    ]
    season_restore_estimate = restore_cost_estimate(restorable, bulk=True)

    season_restore_form = SeasonRestoreForm()
    if (
        season_restore_form.season_restore_submit.data
        and season_restore_form.validate_on_submit()
    ):
        # The restore is an admin tool, like on the series page (#186
        # follow-up). The season page itself stays open to all users
        # for its episode listing.
        if not current_user.admin:
            flash("Need to be an admin user to do that!", "danger")
            return redirect(url_for("main.season", series_id=series_id, season=season))
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
@admin_required
def file(file_id):
    """Show the details for a particular video file.

    This is an admin page (#186 follow-up). All on it (track edits,
    remux, transcode, upload/download, delete) is library management.
    Thus, the whole page is gated, and not each form."""

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

    # When the file is not in the local library, only a restore from AWS or
    # a delete are useful. The template disables the other forms. Their
    # submit handlers below refuse stale submissions.

    file_exists_locally = os.path.isfile(
        os.path.join(current_app.config["LIBRARY_DIR"], file.file_path)
    )

    # The video file can belong to a movie or to a TV show. Find which one
    # from its movie_id or series_id. Then get the information of the
    # related movie or TV series.

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

    # The form to scan the metadata of the file again

    metadata_scan_form = TrackMetadataScanForm()

    if metadata_scan_form.scan_submit.data and metadata_scan_form.validate_on_submit():
        if not file_exists_locally:
            flash(f"'{file.basename}' is not present locally.", "warning")
            return redirect(url_for("main.file", file_id=file.id))

        if track_metadata_scan(file.id):
            flash(f"Rescanned track metadata for '{file.basename}'", "info")
        else:
            flash(
                f"A different task is processing '{file.basename}'. "
                f"Try again when it finishes.",
                "warning",
            )
        return redirect(url_for("main.file", file_id=file.id))

    # The form to edit the attributes of the file

    mkvpropedit_form = MKVPropEditForm()

    # The choices and defaults are strings, because WTForms coerces the
    # submitted values to strings. An int here would fail the "valid
    # choice" validation.

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
        # No audio tracks. Remove the field completely. Thus, its empty
        # radio group cannot fail validation and block subtitle-only
        # property edits. The template already does not render it,
        # through {% if audio_tracks %}.

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
        # no audio tracks. None tells the task that there is no default to set.

        default_audio_track = (
            mkvpropedit_form.default_audio.data if audio_tracks else None
        )

        # The per-track language boxes go with the flag edits. Thus, 1
        # mkvpropedit pass and 1 scan cover both. They are rendered by
        # hand, not as form fields (there is 1 per track, like the
        # subtitle triage checkboxes). Only the tracks whose code changed
        # are sent. An untouched form leaves them as they are.

        track_languages = {}
        unresolved = []
        for prefix, tracks in (("a", audio_tracks), ("s", subtitle_tracks)):
            for track in tracks:
                submitted = request.form.get(f"language_{prefix}{track.track}", "")
                if not submitted.strip():
                    continue

                language = resolve_language_code(submitted)
                if not language:
                    unresolved.append(submitted.strip())

                # The comparison uses resolved codes. Thus, a box can
                # still hold a stored ISO 639-2/T spelling ("deu"). That
                # box does not read as a request to rewrite the track to
                # the bibliographic spelling.

                elif language != (
                    resolve_language_code(track.language) or track.language
                ):
                    track_languages[f"{prefix}{track.track}"] = language

        if unresolved:
            flash(
                f"Unrecognized language "
                f"{'entries' if len(unresolved) > 1 else 'entry'} "
                f"{', '.join(repr(entry) for entry in unresolved)}. "
                f"No properties were changed.",
                "danger",
            )
            return redirect(url_for("main.file", file_id=file.id))

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
                    track_languages or None,
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
                f"Unable to update MKV properties for '{file.basename}' because it is not an MKV file!",
                "danger",
            )

        return redirect(url_for("main.file", file_id=file.id))

    mkvpropedit_form.process()

    # The form to remux the file without some tracks

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
                f"Unable to remux '{file.basename}' because it is not an MKV file!",
                "danger",
            )

        return redirect(url_for("main.file", file_id=file.id))

    mkvmerge_form.process()

    # The form to request a transcode of this file

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

    # The form to request an upload of this file to AWS S3 storage

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

    # The form to delete and purge the file from the database

    delete_form = FileDeleteForm()
    if delete_form.delete_submit.data and delete_form.validate_on_submit():
        aws_untouched_key = file.aws_untouched_key
        leftover_dir = os.path.join(current_app.config["LIBRARY_DIR"], file.dirname)

        # The transcoded copies of the file go with it. Fitzflix notes
        # the paths before the delete (the rows cascade away with the
        # File). It removes them only after the commit. This is the same
        # posture as for the AWS key.

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

        # Delete the AWS copy only after the database delete is committed.
        # Thus, a failed commit cannot leave a database record whose backup is
        # gone.

        if aws_untouched_key and not untouched_key_still_claimed(aws_untouched_key):
            current_app.request_queue.enqueue(
                "app.videos.aws_delete",
                args=(aws_untouched_key,),
                job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
            )

        purge_derived_paths(derived_paths)

        # Clear the folder if only placed poster art and OS metadata
        # remain. A delete of the last file of a movie used to leave its
        # poster.jpg husk for the weekly sweep.

        from app.maintenance import clear_leftover_directory

        clear_leftover_directory(leftover_dir)

        flash(f"Deleted '{file.basename}' and removed from database.", "success")

        if file.movie_id:
            return redirect(url_for("main.movie_files", movie_id=file.movie_id))

        elif file.series_id and file.season:
            return redirect(
                url_for("main.season", series_id=file.series_id, season=file.season)
            )

        else:
            return redirect(url_for("main.index"))

    # The per-file triage link exists only while this file has pending
    # possibly-forced tracks (and only admins can act on them). The
    # candidate track ids go with it. Thus, the Tracks table can badge
    # the rows that the triage page asks about. The import-time marker
    # (forced left unknown) covers only the older, cruder heuristic.
    # Without the ids, a candidate recorded as plainly unforced would
    # have no badge and no way in.

    triage_candidates = forced_subtitle_candidates(file_id=file.id)
    possibly_forced_track_ids = {
        candidate["track"].id
        for entry in triage_candidates
        for candidate in entry["tracks"]
    }
    pending_subtitle_triage = bool(current_user.admin and triage_candidates)
    pending_lossy_triage = bool(
        current_user.admin and lossy_audio_candidates(file_id=file.id)
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
        pending_lossy_triage=pending_lossy_triage,
        possibly_forced_track_ids=possibly_forced_track_ids,
        metadata_scan_form=metadata_scan_form,
        mkvpropedit_form=mkvpropedit_form,
        # The dropdowns offer the own languages of the collection.
        # language_names covers the stored spellings too. Thus, a track
        # recorded as "deu" still reads as German.
        languages=(
            library_language_choices()
            if file.container == "Matroska" and (audio_tracks or subtitle_tracks)
            else ()
        ),
        language_names=language_names(),
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


# The tie-break order for the dominant role of a person. A
# director-actor reads as Director. A bit-part-everything reads as
# Actor before the narrower crafts.

ROLE_PRECEDENCE = (
    "Director",
    "Actor",
    "Cinematographer",
    "Composer",
    "Writer",
    "Editor",
)


def _ranked_people_query(role, query_text, minimum_films):
    """Return the people with their work counts under the role filter.

    A name search can narrow the list. The people with the most works
    come first. Ties break on the surname. TMDB has no structured sort
    name. Thus, the last whitespace-separated token stands in for it.
    This is wrong for "Jr." suffixes and multi-word surnames, but
    acceptable as a tie-break."""

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
    return people_query.having(film_count >= minimum_films).order_by(
        film_count.desc(),
        db.func.substring_index(TMDBCredit.name, " ", -1).asc(),
        TMDBCredit.name.asc(),
    )


def _ranked_people(role):
    """Return each person credited on more than 1 work under the role filter.

    The rows are [id, name, profile path, count], in page order. Redis
    caches them until the next credit write (see
    invalidate_people_ranking) or for PEOPLE_RANKING_SECONDS."""

    key = PEOPLE_RANKING_KEY.format(role=role)
    cached = current_app.redis.get(key)
    if cached:
        return json.loads(cached)
    ranked = [
        [credit_id, name, profile_path, count]
        for credit_id, name, profile_path, count in _ranked_people_query(role, None, 2)
    ]
    current_app.redis.set(key, json.dumps(ranked), ex=PEOPLE_RANKING_SECONDS)
    return ranked


class ListPagination(Pagination):
    """The Pagination of Flask-SQLAlchemy over an in-memory ranking.

    The people template reads .items, .page, .total, and iter_pages()
    in the same way as it did from the query."""

    def __init__(self, rows, **kwargs):
        self._rows = rows
        # The default of Query.paginate, not the 100-row maximum of the base class
        kwargs.setdefault("max_per_page", None)
        super().__init__(**kwargs)

    def _query_items(self):
        """Return the rows of this page as the attribute bags of the template."""

        offset = self._query_offset
        return [
            SimpleNamespace(
                id=credit_id,
                name=name,
                tmdb_profile_path=profile_path,
                film_count=count,
            )
            for credit_id, name, profile_path, count in self._rows[
                offset : offset + self.per_page
            ]
        ]

    def _query_count(self):
        """Return the length of the whole ranking."""

        return len(self._rows)


def _credited_film_pairs(role="all"):
    """Return the (credit_id, movie_id) pairs that the people surfaces count.

    The pairs are the credited cast rows, the key crew roles, or their
    union without duplicates. Movies and TV series both count. A TV
    series goes in the movie_id column as its NEGATED id. Thus, the
    distinct-count space has no collisions without a discriminator
    column. Nothing joins these ids back to a table. They are only
    counted.

    The search paths always count all ("all"). The /people page passes
    its cast/crew filter through. Thus, the work counts show the
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
    """Return the dominant credited role of each person.

    That is the key role that covers the most distinct library works
    (films and series both). ROLE_PRECEDENCE breaks ties."""

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
