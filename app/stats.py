"""Compute the viewing statistics of 1 user from the diary (#264).

The source is the diary rows with a watch date, as on the History page.
A rating row with no date is not a viewing. The statistics use only
data that Fitzflix stores: the diary, the TMDB runtimes, the TMDB
credits, and the production countries. A user has some hundreds of
rows. Thus, the page computes everything on each request, in Python,
from some queries.
"""

from collections import Counter
from datetime import date

from app import db
from app.models import (
    Movie,
    MovieCast,
    MovieCrew,
    TMDBCredit,
    TMDBProductionCountry,
    UserMovieReview,
    movie_production_countries,
)

# The number of rows in each ranked list.

TOP = 10

# A cast credit counts when its billing is in the first CAST_BILLING
# places. Thus, a film counts for its leads, not for each walk-on part.

CAST_BILLING = 10

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct")
MONTHS += ("Nov", "Dec")

# The half-star steps of the rating ladder, from 0.5 to 5.

RATING_STEPS = [step / 2 for step in range(1, 11)]


def star_label(rating):
    """Return a rating as ladder text, for example 3.5 as 3½."""

    whole = int(rating)
    half = "½" if rating - whole else ""
    return f"{whole or ''}{half}" or "0"


def _viewings(user_id):
    """Return the dated diary rows of the user with their film facts."""

    return (
        db.session.query(
            UserMovieReview.movie_id,
            UserMovieReview.date_watched,
            UserMovieReview.rating,
            UserMovieReview.rewatch,
            Movie.title,
            Movie.year,
            Movie.tmdb_runtime,
        )
        .join(Movie, Movie.id == UserMovieReview.movie_id)
        .filter(
            UserMovieReview.user_id == user_id,
            UserMovieReview.date_watched.isnot(None),
        )
        .all()
    )


def watched_years(user_id):
    """Return the years with at least 1 viewing, newest first."""

    return sorted({row.date_watched.year for row in _viewings(user_id)}, reverse=True)


def _ranked(counter, names, films):
    """Return the TOP entries of counter as dicts, most viewings first.

    A tie goes to the name in alphabetical order. Thus, the order is
    the same on each load."""

    ordered = sorted(counter.items(), key=lambda item: (-item[1], names[item[0]]))
    return [
        {
            "id": key,
            "name": names[key],
            "viewings": count,
            "films": len(films[key]),
        }
        for key, count in ordered[:TOP]
    ]


def _people(rows, query):
    """Count the viewings for each person of query, with their films.

    query gives (movie id, credit id, name). A person counts 1 time for
    each viewing of a film, even with 2 credits on it."""

    per_movie = {}
    names = {}
    for movie_id, credit_id, name in query:
        per_movie.setdefault(movie_id, set()).add(credit_id)
        names[credit_id] = name
    counter = Counter()
    films = {}
    for row in rows:
        for credit_id in per_movie.get(row.movie_id, ()):
            counter[credit_id] += 1
            films.setdefault(credit_id, set()).add(row.movie_id)
    return _ranked(counter, names, films)


def diary_stats(user_id, year=None):
    """Return the statistics of the user for 1 year, or for all time.

    The result is a dict. Totals holds the counts, the hours, and the
    average rating. Periods is the chart of viewings: each month of the
    year, or each year of all time. Ratings is the chart of the rating
    ladder. Decades is the chart of the release decades. The ranked lists
    are the directors, the cast, and the countries. Most watched is the
    films with 2 or more viewings."""

    rows = _viewings(user_id)
    if year:
        rows = [row for row in rows if row.date_watched.year == year]

    rated = [row.rating for row in rows if row.rating is not None]
    minutes = sum(row.tmdb_runtime or 0 for row in rows)
    totals = {
        "viewings": len(rows),
        "films": len({row.movie_id for row in rows}),
        "hours": round(minutes / 60),
        "first_watches": sum(1 for row in rows if row.rewatch is False),
        "rewatches": sum(1 for row in rows if row.rewatch is True),
        "rated": len(rated),
        "average": round(sum(rated) / len(rated), 1) if rated else None,
    }

    if year:
        by_month = Counter(row.date_watched.month for row in rows)
        periods = [
            {"label": MONTHS[month - 1], "title": f"{MONTHS[month - 1]} {year}"}
            | {"count": by_month[month]}
            for month in range(1, 13)
        ]
    else:
        by_year = Counter(row.date_watched.year for row in rows)
        first = min(by_year) if by_year else date.today().year
        periods = [
            {"label": f"’{str(y)[2:]}", "title": str(y), "count": by_year[y]}
            for y in range(first, date.today().year + 1)
        ]

    by_rating = Counter(rated)
    ratings = [
        {"label": star_label(step), "count": by_rating[step]} for step in RATING_STEPS
    ]

    movie_ids = {row.movie_id for row in rows}
    directors = _people(
        rows,
        db.session.query(MovieCrew.movie_id, TMDBCredit.id, TMDBCredit.name)
        .join(TMDBCredit, TMDBCredit.id == MovieCrew.credit_id)
        .filter(MovieCrew.movie_id.in_(movie_ids), MovieCrew.job == "Director"),
    )
    cast = _people(
        rows,
        db.session.query(MovieCast.movie_id, TMDBCredit.id, TMDBCredit.name)
        .join(TMDBCredit, TMDBCredit.id == MovieCast.credit_id)
        .filter(
            MovieCast.movie_id.in_(movie_ids),
            MovieCast.billing_order < CAST_BILLING,
        ),
    )

    countries_of = {}
    country_names = {}
    for movie_id, country_id, name in (
        db.session.query(
            movie_production_countries.c.movie_id,
            TMDBProductionCountry.id,
            TMDBProductionCountry.name,
        )
        .join(
            TMDBProductionCountry,
            TMDBProductionCountry.id == movie_production_countries.c.country_id,
        )
        .filter(movie_production_countries.c.movie_id.in_(movie_ids))
    ):
        countries_of.setdefault(movie_id, set()).add(country_id)
        country_names[country_id] = name
    country_counter = Counter()
    country_films = {}
    decade_counter = Counter()
    decade_films = {}
    for row in rows:
        for country_id in countries_of.get(row.movie_id, ()):
            country_counter[country_id] += 1
            country_films.setdefault(country_id, set()).add(row.movie_id)
        decade = row.year // 10 * 10
        decade_counter[decade] += 1
        decade_films.setdefault(decade, set()).add(row.movie_id)

    # The decades are a chart in time order. A decade with no viewing
    # between the first and the last keeps its empty column.

    decades = []
    if decade_counter:
        for decade in range(min(decade_counter), max(decade_counter) + 10, 10):
            decades.append(
                {
                    "label": f"{decade}s",
                    "count": decade_counter[decade],
                    "films": len(decade_films.get(decade, ())),
                }
            )

    film_counter = Counter(row.movie_id for row in rows)
    titles = {row.movie_id: (row.title, row.year) for row in rows}
    most_watched = [
        {"id": movie_id, "title": titles[movie_id][0], "year": titles[movie_id][1]}
        | {"viewings": count}
        for movie_id, count in sorted(
            film_counter.items(), key=lambda item: (-item[1], titles[item[0]])
        )
        if count >= 2
    ][:TOP]

    return {
        "year": year,
        "totals": totals,
        "periods": periods,
        "periods_max": max((p["count"] for p in periods), default=0),
        "ratings": ratings,
        "ratings_max": max((r["count"] for r in ratings), default=0),
        "directors": directors,
        "cast": cast,
        "countries": _ranked(country_counter, country_names, country_films),
        "decades": decades,
        "decades_max": max((d["count"] for d in decades), default=0),
        "most_watched": most_watched,
    }
