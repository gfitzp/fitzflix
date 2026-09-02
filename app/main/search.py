"""Local search, from the routes.py split.

This module holds the library search page, the type-ahead JSON of the
navbar, and the TMDB lookup page."""

import re
import traceback


from flask import (
    current_app,
    jsonify,
    render_template,
    url_for,
    request,
)

# Flask 2.4 removed flask.Markup. Import it from its real home.
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
from app.main.helpers import (
    _upgrade_threshold,
    library_upgradable,
    series_upgradable,
)
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


def _parse_query(q):
    """Split the modifier tokens out of a search query (#185).

    'jaws y:1975' becomes ('jaws', (1975, 1975)). 'y:1980-1989' gives a
    range. 'year:' is the long form. An unknown or malformed token
    ('y:83') stays in the text. Thus, the search uses it literally and
    does not guess."""

    years = None
    words = []
    for token in q.split():
        match = re.fullmatch(
            r"(?:y|year):(\d{4})(?:-(\d{4}))?", token, flags=re.IGNORECASE
        )
        if match:
            first = int(match.group(1))
            second = int(match.group(2)) if match.group(2) else first
            years = (min(first, second), max(first, second))
        else:
            words.append(token)
    return " ".join(words), years


def _movie_search_results(wildcard, limit=50, years=None):
    """Return the movies whose titles match, each with its best owned copy.

    Only films with a local main-feature file appear. A review-only
    record (a diary entry for an unowned film) belongs to the TMDB
    search, not to the library search."""

    upgrade_threshold = _upgrade_threshold()

    # The match quality outranks the alphabet. Exact titles come first,
    # then prefixes, then substrings. Otherwise a short query such as "Up"
    # fills the result cap with alphabetically earlier titles that only
    # CONTAIN it. Then the film named Up never shows.

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
    query = Movie.query.filter(
        db.or_(
            Movie.title.ilike(f"%{wildcard}%"),
            Movie.tmdb_title.ilike(f"%{wildcard}%"),
        )
    ).filter(Movie.files.any(File.feature_type_id.is_(None)))

    # A y: modifier (#185) matches each of the 2 years of a film. These
    # are the library identity year and the TMDB release year. The 2
    # years frequently differ by 1 for festival releases.

    if years:
        query = query.filter(
            db.or_(
                Movie.year.between(*years),
                db.extract("year", Movie.tmdb_release_date).between(*years),
            )
        )
    movies = (
        query.order_by(match_rank, Movie.title.asc(), Movie.year.asc())
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


def _tv_search_results(wildcard, limit=50, years=None):
    """Return the TV series whose titles match, with a summary per season.

    The summary of a season is the worst quality among its best (rank 1)
    episode files. A TV show is usually bought season by season. Thus, a
    series-wide "best quality" would hide the seasons that need an
    upgrade. In a store, the weakest link of each season is what counts.
    """

    # Exact, then prefix, then substring. This is the same ranking as the
    # movie search, for the same reason (a buried exact match).

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
    series_query = TVSeries.query.filter(
        db.or_(
            TVSeries.title.ilike(f"%{wildcard}%"),
            TVSeries.tmdb_name.ilike(f"%{wildcard}%"),
        )
    )

    # A y: modifier (#185) means the year of the series premiere. A
    # series with no TMDB first-air date drops out while the filter is
    # active.

    if years:
        series_query = series_query.filter(
            db.extract("year", TVSeries.tmdb_first_air_date).between(*years)
        )
    series_list = (
        series_query.order_by(match_rank, TVSeries.title.asc()).limit(limit).all()
    )
    if not series_list:
        return []

    upgrade_threshold = _upgrade_threshold()
    series_ids = [series.id for series in series_list]

    # This has the same shape as the TV library page. Rank the copies of
    # each episode, keep the best copy per episode, then take the worst
    # best-copy of each season.

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
                # A physical-media season (DVD, SD/720p Blu-ray) is often the
                # only release that will exist. Thus, it does not count as
                # upgradable.
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
    """Return the credited people whose names match.

    Each person comes with a library film count and a dominant role. The
    rules are the same as the People page. Cast and key crew roles
    count. Uncredited-only roles never count. The matches sort by film
    count, and the surname breaks ties.
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
            # An exact full-name match comes first. Among the partial
            # matches, the film count is the better signal. There is no
            # prefix tier here. "Ford Beebe" must not outrank Harrison
            # Ford on a "Ford" search.
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
    """Search the movies and TV series from one box, anywhere in the app."""

    q = (request.args.get("q") or "").strip()
    text, years = _parse_query(q)
    movie_results = []
    tv_results = []
    people_results = []

    if q:
        # Spaces become wildcards. Thus, the word order and the
        # punctuation are not important. A modifier-only query ('y:1983')
        # matches every title. That makes the year filter a browse. The
        # people results stay text-driven because a year means nothing
        # for a person.

        wildcard = text.replace(" ", "%")
        movie_results = _movie_search_results(wildcard, years=years)
        tv_results = _tv_search_results(wildcard, years=years)
        people_results = _people_search_results(wildcard) if text else []

        # The personal funnel badges are "Might interest you" (in the
        # stored recommendations, the set of the library rail), then "On
        # your watchlist", then "Seen". Watchlist can show with each
        # neighbor. A seen film already feeds the taste profile. Thus,
        # seen and might-interest are exclusive. All 3 badges are about
        # the CURRENT user. That is their diary, their list, and their
        # profile.

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
                # A later row wins, but a bare rewatch does not erase a rating.
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
        q_text=text,
        movie_results=movie_results,
        tv_results=tv_results,
        people_results=people_results,
    )


@bp.route("/search.json")
@login_required
def search_json():
    """Return the type-ahead suggestions for the global search box."""

    q = (request.args.get("q") or "").strip()
    results = []

    if len(q) >= 2:
        text, years = _parse_query(q)
        wildcard = text.replace(" ", "%")

        for result in _movie_search_results(wildcard, limit=5, years=years):
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

        for result in _tv_search_results(wildcard, limit=5, years=years):
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

        # People results stay text-driven. A year means nothing for a person.
        people = _people_search_results(wildcard, limit=5) if text else []
        for person in people:
            results.append(
                {
                    "type": "Person",
                    "title": person["name"],
                    "detail": (
                        (f"{person['role']} · " if person["role"] else "")
                        + f"{person['film_count']} title"
                        + ("s" if person["film_count"] != 1 else "")
                    ),
                    "url": url_for("main.movie_library", credit=person["id"]),
                }
            )

    return jsonify({"results": results})


@bp.route("/search/tmdb")
@login_required
def search_tmdb():
    """Look up a title on TMDB to see what exists outside the library."""

    q = (request.args.get("q") or "").strip()

    # scope=movies skips the TV search completely. The "Log a film" box of
    # the History page arrives this way (#215). The diary logs only
    # movies.

    movies_only = request.args.get("scope") == "movies"
    movie_matches = []
    tv_matches = []
    error = None
    streaming_attribution = False

    text, years = _parse_query(q)

    if q and not current_app.config["TMDB_API_KEY"]:
        error = "TMDB_API_KEY is not configured, so TMDB can't be searched."

    elif q and not text:
        # The TMDB search endpoints need a title. A year alone cannot
        # browse them the way it browses the local library.
        error = "Add a title to the year filter — TMDB can't be searched by year alone."

    elif q:
        # A single-year y: modifier (#185) goes with the year parameter of
        # the API. That also improves the TMDB ranking. The search
        # endpoints cannot express a range. The release-date filter below
        # enforces the range in each case.

        params = {"api_key": current_app.config["TMDB_API_KEY"], "query": text}
        try:
            searches = [
                ("/search/movie", movie_matches, "title", "release_date"),
            ]
            if not movies_only:
                searches.append(("/search/tv", tv_matches, "name", "first_air_date"))
            for url, bucket, title_key, date_key in searches:
                request_params = dict(params)
                if years and years[0] == years[1]:
                    year_param = (
                        "primary_release_year"
                        if url == "/search/movie"
                        else "first_air_date_year"
                    )
                    request_params[year_param] = years[0]
                r = tmdb_get(
                    current_app.config["TMDB_API_URL"] + url,
                    params=request_params,
                    timeout=10,
                )
                r.raise_for_status()
                for result in r.json().get("results") or []:
                    if len(bucket) >= 10:
                        break
                    year = (result.get(date_key) or "")[:4]
                    if years and not (
                        year.isdigit() and years[0] <= int(year) <= years[1]
                    ):
                        continue
                    bucket.append(
                        {
                            "tmdb_id": result.get("id"),
                            "title": result.get(title_key),
                            "year": year,
                            "overview": result.get("overview"),
                            "poster_path": result.get("poster_path"),
                            "genre_ids": result.get("genre_ids") or [],
                            "library_id": None,
                            "upgradable": None,
                        }
                    )

        except Exception:
            current_app.logger.warning(traceback.format_exc())
            error = "TMDB could not be reached; try again in a moment."

        # Mark the results that are already in the library, by TMDB id.
        # "In library" means that a local main-feature file exists. A
        # review-only record (a logged unowned film) does not count. Each
        # owned match also carries the verdict of the shopping list. Thus,
        # the badge here shows the same amber or green as all other
        # surfaces (#191), not a colorless "In library".

        if movie_matches:
            owned = {
                movie.tmdb_id: movie
                for movie in Movie.query.filter(
                    Movie.tmdb_id.in_([m["tmdb_id"] for m in movie_matches])
                )
                .filter(Movie.files.any(File.feature_type_id.is_(None)))
                .all()
            }
            for match in movie_matches:
                movie = owned.get(match["tmdb_id"])
                match["library_id"] = movie.id if movie else None
                match["upgradable"] = library_upgradable(movie) if movie else None

        if tv_matches:
            owned = dict(
                db.session.query(TVSeries.tmdb_id, TVSeries.id)
                .filter(TVSeries.tmdb_id.in_([m["tmdb_id"] for m in tv_matches]))
                .all()
            )
            # A series record with no files (all its episodes were deleted)
            # has nothing to badge. Thus, series_upgradable leaves it out
            # and the row renders bare.
            upgradable = series_upgradable(list(owned.values()))
            for match in tv_matches:
                match["library_id"] = owned.get(match["tmdb_id"])
                match["upgradable"] = upgradable.get(match["library_id"])

        # The personal funnel badges. "Seen" and "On your watchlist" come
        # from a local record, with or without a file (a review-only
        # record remembers a logged unowned film). "Might interest you"
        # scores an unowned match through the coarse scorer without the
        # person term (a bare search result has no person context). It
        # badges an owned match that is in the stored recommendations. It
        # never shows on a seen film. That watch already feeds the taste
        # profile.

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
            # The star ladder of the row posts to the movie route if a
            # record exists (with or without a file). Otherwise it posts to
            # the TMDB log route.
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

        # Streaming and rent/buy badges on unowned movie matches. Both use
        # only the services of this user (Fitzflix caches the lookups per
        # title for 1 day). The flag turns on the mandatory JustWatch
        # credit.

        provider_ids = user_provider_ids(current_user)
        if provider_ids:
            for match in movie_matches:
                if match["library_id"] is not None or match["tmdb_id"] is None:
                    continue
                availability = title_availability(match["tmdb_id"])
                matches = streaming_matches(
                    availability, provider_ids, tmdb_id=match["tmdb_id"]
                )
                rentals = rental_matches(availability, provider_ids)
                if matches:
                    match["streaming"] = matches
                if rentals:
                    match["rentals"] = rentals
                if matches or rentals:
                    streaming_attribution = True

    return render_template(
        "search_tmdb.html",
        title=f"TMDB results for '{q}'" if q else "TMDB search",
        q=q,
        movie_matches=movie_matches,
        tv_matches=tv_matches,
        error=error,
        streaming_attribution=streaming_attribution,
    )
