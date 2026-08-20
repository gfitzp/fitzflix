"""Local search (#17's slice f): the library search page, the
navbar type-ahead JSON, and the TMDb lookup page."""

import traceback


from flask import (
    current_app,
    jsonify,
    render_template,
    url_for,
    request,
)

# flask.Markup was removed in Flask 2.4; import from its actual home
from flask_login import current_user, login_required

from app import db
from app.models import (
    File,
    Movie,
    RefQuality,
    TMDBCredit,
    TVSeries,
    UserMovieReview,
    UserMovieStatus,
    UserWatchlist,
    tmdb_get,
    tv_file_rank,
)
from app.main import bp
from app.main.helpers import _upgrade_threshold
from app.main.library import _credited_film_pairs, _dominant_roles
from app.recommendations import (
    coarse_interest_score,
    marker_bar,
    recommended_movie_ids,
    stored_profile,
)
from app.streaming import (
    rental_matches,
    streaming_matches,
    title_availability,
    user_provider_ids,
)


def _movie_search_results(wildcard, limit=50):
    """Movies whose titles match, each with its best owned copy.

    Only films with a local main-feature file appear: review-only
    records (a diary entry for an unowned film) belong to the TMDb
    search, not the library search."""

    upgrade_threshold = _upgrade_threshold()

    # Match quality outranks the alphabet: exact titles first, then
    # prefixes, then substrings — otherwise a short query like "Up"
    # fills the result cap with alphabetically-earlier titles that
    # merely CONTAIN it, and the film actually named Up never shows

    match_rank = db.case(
        (
            db.or_(
                Movie.title.ilike(wildcard),
                Movie.tmdb_title.ilike(wildcard),
            ),
            0,
        ),
        (
            db.or_(
                Movie.title.ilike(f"{wildcard}%"),
                Movie.tmdb_title.ilike(f"{wildcard}%"),
            ),
            1,
        ),
        else_=2,
    )

    results = []
    movies = (
        Movie.query.filter(
            db.or_(
                Movie.title.ilike(f"%{wildcard}%"),
                Movie.tmdb_title.ilike(f"%{wildcard}%"),
            )
        )
        .filter(Movie.files.any(File.feature_type_id.is_(None)))
        .order_by(match_rank, Movie.title.asc(), Movie.year.asc())
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

    # Exact, then prefix, then substring — same ranking as the movie
    # search, for the same buried-exact-match reason

    match_rank = db.case(
        (
            db.or_(
                TVSeries.title.ilike(wildcard),
                TVSeries.tmdb_name.ilike(wildcard),
            ),
            0,
        ),
        (
            db.or_(
                TVSeries.title.ilike(f"{wildcard}%"),
                TVSeries.tmdb_name.ilike(f"{wildcard}%"),
            ),
            1,
        ),
        else_=2,
    )
    series_list = (
        TVSeries.query.filter(
            db.or_(
                TVSeries.title.ilike(f"%{wildcard}%"),
                TVSeries.tmdb_name.ilike(f"%{wildcard}%"),
            )
        )
        .order_by(match_rank, TVSeries.title.asc())
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
    """Credited people whose names match, with their library film counts
    and dominant role.

    Mirrors the People page's rules: cast plus key crew roles count,
    uncredited-only roles never do, and matches order by film count
    with the surname tie-break.
    """

    pairs = _credited_film_pairs()
    film_count = db.func.count(db.distinct(pairs.c.movie_id)).label("film_count")
    matches = (
        db.session.query(
            TMDBCredit.id,
            TMDBCredit.name,
            TMDBCredit.tmdb_profile_path,
            film_count,
        )
        .join(pairs, pairs.c.credit_id == TMDBCredit.id)
        .filter(TMDBCredit.name.ilike(f"%{wildcard}%"))
        .group_by(TMDBCredit.id, TMDBCredit.name, TMDBCredit.tmdb_profile_path)
        .order_by(
            # An exact full-name match surfaces first; among partial
            # matches, film count stays the better signal (no prefix
            # tier here — "Ford Beebe" shouldn't outrank Harrison Ford
            # on a "Ford" search)
            db.case((TMDBCredit.name.ilike(wildcard), 0), else_=1),
            film_count.desc(),
            db.func.substring_index(TMDBCredit.name, " ", -1).asc(),
            TMDBCredit.name.asc(),
        )
        .limit(limit)
        .all()
    )
    roles = _dominant_roles([person.id for person in matches])
    return [
        {
            "id": person.id,
            "name": person.name,
            "tmdb_profile_path": person.tmdb_profile_path,
            "film_count": person.film_count,
            "role": roles.get(person.id),
        }
        for person in matches
    ]


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

        # The personal funnel badges: "Might interest you" (in the
        # stored recommendations — the library rail's own set) →
        # "On your watchlist" → "Seen". Watchlist coexists with either
        # neighbor, but a seen film already feeds the taste profile, so
        # seen and might-interest are exclusive. All three are about
        # the CURRENT user — their diary, their list, their profile

        if movie_results:
            rec_ids = recommended_movie_ids(current_app.redis, current_user.id)
            result_ids = [result["movie"].id for result in movie_results]
            ratings = {}
            for movie_id, rating in (
                db.session.query(UserMovieReview.movie_id, UserMovieReview.rating)
                .filter(UserMovieReview.user_id == int(current_user.id))
                .filter(UserMovieReview.movie_id.in_(result_ids))
                .order_by(UserMovieReview.date_watched.asc())
            ):
                # Later rows win, but a bare rewatch doesn't erase a rating
                if rating is not None or movie_id not in ratings:
                    ratings[movie_id] = rating
            watchlisted = {
                movie_id
                for (movie_id,) in db.session.query(UserWatchlist.movie_id)
                .filter(UserWatchlist.user_id == int(current_user.id))
                .filter(UserWatchlist.movie_id.in_(result_ids))
            }
            for result in movie_results:
                movie_id = result["movie"].id
                result["seen"] = movie_id in ratings
                result["rating"] = ratings.get(movie_id)
                result["watchlisted"] = movie_id in watchlisted
                result["might_interest"] = (
                    movie_id in rec_ids and movie_id not in ratings
                )

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
                    "title": person["name"],
                    "detail": (
                        (f"{person['role']} · " if person["role"] else "")
                        + f"{person['film_count']} film"
                        + ("s" if person["film_count"] != 1 else "")
                    ),
                    "url": url_for("main.movie_library", credit=person["id"]),
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
                            "genre_ids": result.get("genre_ids") or [],
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

        # The personal funnel badges. "Seen" and "On your watchlist"
        # hang off any local record, file or not (a review-only record
        # remembers a logged unowned film). "Might interest you" scores
        # unowned matches through the coarse scorer minus the person
        # term (a bare search result has no person context) and badges
        # owned matches ranked in the stored recommendations — and
        # never shows on a seen film, whose watch already feeds the
        # taste profile

        record_ids = {}
        movie_tmdb_ids = [m["tmdb_id"] for m in movie_matches if m["tmdb_id"]]
        if movie_tmdb_ids:
            record_ids = dict(
                db.session.query(Movie.tmdb_id, Movie.id).filter(
                    Movie.tmdb_id.in_(movie_tmdb_ids)
                )
            )
        seen_ids = set()
        watchlisted_ids = set()
        refused_ids = set()
        if record_ids:
            seen_ids = {
                movie_id
                for (movie_id,) in db.session.query(UserMovieReview.movie_id)
                .filter(UserMovieReview.user_id == int(current_user.id))
                .filter(UserMovieReview.movie_id.in_(list(record_ids.values())))
            }
            watchlisted_ids = {
                movie_id
                for (movie_id,) in db.session.query(UserWatchlist.movie_id)
                .filter(UserWatchlist.user_id == int(current_user.id))
                .filter(UserWatchlist.movie_id.in_(list(record_ids.values())))
            }
            refused_ids = {
                movie_id
                for (movie_id,) in db.session.query(UserMovieStatus.movie_id)
                .filter(UserMovieStatus.user_id == int(current_user.id))
                .filter(UserMovieStatus.kind == "not_interested")
                .filter(UserMovieStatus.movie_id.in_(list(record_ids.values())))
            }

        profile = stored_profile(current_app.redis, current_user.id)
        rec_ids = recommended_movie_ids(current_app.redis, current_user.id)
        bar = marker_bar(profile) if profile else None
        for match in movie_matches:
            record_id = record_ids.get(match["tmdb_id"])
            # The row's star ladder posts to the movie route when any
            # record exists (file or not), the TMDb log route otherwise
            match["record_id"] = record_id
            match["seen"] = record_id in seen_ids
            match["watchlisted"] = record_id in watchlisted_ids
            if match["seen"] or record_id in refused_ids:
                continue
            if match["library_id"] is not None:
                if match["library_id"] in rec_ids:
                    match["might_interest"] = True
                continue
            if profile is None:
                continue
            score = coarse_interest_score(profile, match["genre_ids"], match["year"])
            if score > bar:
                match["might_interest"] = True

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
