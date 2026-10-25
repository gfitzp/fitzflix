"""The personal funnel of a user over a set of films.

The funnel has 4 stages. An owned film has a main feature in the
library. A logged film has a diary row of the user. A watchlisted film
has a watchlist row of the user. A refused film has a not-interested
status of the user. The discovery shelves, the search pages, the
filmographies and the galleries read these sets. One rule decides the
"Might interest you" marker.
"""

from collections import namedtuple

from app import db
from app.models import File, Movie, UserMovieReview, UserMovieStatus, UserWatchlist

FilmSets = namedtuple("FilmSets", "owned logged watchlisted refused")

NOT_INTERESTED = "not_interested"


def user_film_sets(user, tmdb_ids):
    """Return the FilmSets of 1 user over the given films, keyed by TMDB id.

    These are the exclusion inputs that the discovery shelves share:
    the leaving shelf, the newly-added shelves and the library rail."""

    if not tmdb_ids:
        return FilmSets(set(), set(), set(), set())
    user_id = int(user.id)
    owned = {
        tmdb_id
        for (tmdb_id,) in db.session.query(Movie.tmdb_id)
        .filter(Movie.tmdb_id.in_(tmdb_ids))
        .filter(Movie.files.any(File.feature_type_id.is_(None)))
    }
    logged = {
        tmdb_id
        for (tmdb_id,) in db.session.query(Movie.tmdb_id)
        .join(UserMovieReview, UserMovieReview.movie_id == Movie.id)
        .filter(Movie.tmdb_id.in_(tmdb_ids))
        .filter(UserMovieReview.user_id == user_id)
    }
    watchlisted = {
        tmdb_id
        for (tmdb_id,) in db.session.query(Movie.tmdb_id)
        .join(UserWatchlist, UserWatchlist.movie_id == Movie.id)
        .filter(Movie.tmdb_id.in_(tmdb_ids))
        .filter(UserWatchlist.user_id == user_id)
    }
    refused = {
        tmdb_id
        for (tmdb_id,) in db.session.query(Movie.tmdb_id)
        .join(UserMovieStatus, UserMovieStatus.movie_id == Movie.id)
        .filter(Movie.tmdb_id.in_(tmdb_ids))
        .filter(UserMovieStatus.user_id == user_id)
        .filter(UserMovieStatus.kind == NOT_INTERESTED)
    }
    return FilmSets(owned, logged, watchlisted, refused)


def user_film_sets_by_id(user_id, movie_ids):
    """Return the FilmSets of 1 user over the given records, keyed by Movie id.

    The pages that hold Movie records (the search results, the
    filmographies, the Criterion collection and the galleries) read
    this twin of user_film_sets."""

    if not movie_ids:
        return FilmSets(set(), set(), set(), set())
    user_id = int(user_id)
    owned = {
        movie_id
        for (movie_id,) in db.session.query(Movie.id)
        .filter(Movie.id.in_(movie_ids))
        .filter(Movie.files.any(File.feature_type_id.is_(None)))
    }
    logged = {
        movie_id
        for (movie_id,) in db.session.query(UserMovieReview.movie_id)
        .filter(UserMovieReview.user_id == user_id)
        .filter(UserMovieReview.movie_id.in_(movie_ids))
    }
    watchlisted = {
        movie_id
        for (movie_id,) in db.session.query(UserWatchlist.movie_id)
        .filter(UserWatchlist.user_id == user_id)
        .filter(UserWatchlist.movie_id.in_(movie_ids))
    }
    refused = {
        movie_id
        for (movie_id,) in db.session.query(UserMovieStatus.movie_id)
        .filter(UserMovieStatus.user_id == user_id)
        .filter(UserMovieStatus.kind == NOT_INTERESTED)
        .filter(UserMovieStatus.movie_id.in_(movie_ids))
    }
    return FilmSets(owned, logged, watchlisted, refused)


def might_interest(seen, refused, recommended=False, score=None, bar=None):
    """Return True if the "Might interest you" marker shows for 1 film.

    A seen film already feeds the taste profile, and a refused film
    was declined. Neither gets the marker. A film in the stored
    recommendations gets it. Otherwise a film with a coarse score
    above the bar of the profile gets it."""

    if seen or refused:
        return False
    if recommended:
        return True
    return score is not None and bar is not None and score > bar
