"""Build the "since you liked" strip of taste-scored suggestions.

The strip appears after a positive rating. This module is the part of
the old /rate elicitation drive that remains. The Recommendations page
replaced the drive (#235). The module keeps the last-response marker of
the session. A positive rating unlocks the enjoyment picks on the movie
page of the rated film. The picks are unseen candidates that share
features with the anchor film. Fitzflix ranks them by the score of the
taste profile plus a class-weighted adjacency bonus.
"""

import json

from datetime import datetime, timedelta

from flask import current_app

from app import db
from app.models import UserMovieStatus, UserWatchlist
from app.recommendations import (
    FEATURE_CLASS_WEIGHTS,
    collect_features,
    local_candidates,
    score_movie,
    stored_profile,
)

LAST_KEY = "fitzflix:elicit:last:{user_id}"

# The last response unlocks the strip for 1 hour. This is long enough
# for a session. It is short enough that the next day starts with no
# strip.

LAST_TTL_SECONDS = 3600

# The "No Opinion" marks of the retired drive continue to exclude
# their films while the marks are recent. The marks expire after this
# number of years. The user can have seen the film in that time, or can
# remember a verdict. Only the "unseen" mark expires. The "not
# interested" mark is permanent.

UNSEEN_RESURFACE_YEARS = 2

# This weight sets how strongly the adjacency to a positively rated
# film moves similar films forward in the suggestion strip.

ADJACENCY_WEIGHT = 2.0

# After a positive rating, Fitzflix shows up to this number of
# taste-scored suggestions ("since you liked X").

SUGGESTION_COUNT = 3


def set_last_response(redis, user_id, movie_id, action, positive=False):
    """Store the last response of the session.

    The action is one of: rated, watchlist, not_interested. A positive
    rating also unlocks the suggestion strip on the movie page of that
    film."""

    redis.set(
        LAST_KEY.format(user_id=int(user_id)),
        json.dumps(
            {"movie_id": int(movie_id), "action": action, "positive": bool(positive)}
        ),
        ex=LAST_TTL_SECONDS,
    )


def last_response(redis, user_id):
    """Return the last response of the session, or None."""

    payload = redis.get(LAST_KEY.format(user_id=int(user_id)))
    return json.loads(payload) if payload else None


def elicitation_candidates(user_id):
    """Return the movie ids that the suggestion strip can offer.

    These are the local films with full features that the user has not
    logged. This function removes the watchlisted films, because the
    user declared them unseen but wanted. It also removes the films
    with a recent "No Opinion" mark. Older marks expire. The films
    marked not interested are already absent from local_candidates."""

    watchlisted = {
        movie_id
        for (movie_id,) in db.session.query(UserWatchlist.movie_id).filter(
            UserWatchlist.user_id == int(user_id)
        )
    }
    unseen_bar = datetime.now() - timedelta(days=UNSEEN_RESURFACE_YEARS * 365.25)
    unseen = {
        movie_id
        for (movie_id,) in db.session.query(UserMovieStatus.movie_id).filter(
            UserMovieStatus.user_id == int(user_id),
            UserMovieStatus.kind == "unseen",
            UserMovieStatus.date_added > unseen_bar,
        )
    }
    excluded = watchlisted | unseen
    return [
        movie_id for movie_id in local_candidates(user_id) if movie_id not in excluded
    ]


def adjacency_scores(candidates, features, anchor_features):
    """Return movie_id -> class-weighted feature overlap with the anchor film."""

    anchor_keys = {key for _, key, _ in anchor_features}
    scores = {}
    for movie_id in candidates:
        scores[movie_id] = sum(
            FEATURE_CLASS_WEIGHTS.get(cls, 0.0)
            for cls, key, _ in features.get(movie_id, [])
            if key in anchor_keys
        )
    return scores


def suggestions_after_rating(user_id, exclude=(), count=SUGGESTION_COUNT):
    """Return (anchor movie id, suggested movie ids) after a positive rating.

    Return (None, []) if there is no positive rating. These are enjoyment
    picks, not elicitation picks. They are unseen candidates that share
    features with the rated film. Fitzflix ranks them by the score of the
    taste profile plus the adjacency bonus. The signal of the new rating
    comes in through the adjacency term. The stored profile catches up
    in the background.
    """

    redis = current_app.redis
    last = last_response(redis, user_id)
    if not last or last.get("action") != "rated" or not last.get("positive"):
        return None, []
    profile = stored_profile(redis, user_id)
    if not profile:
        return None, []

    anchor_id = last["movie_id"]
    candidates = [
        movie_id
        for movie_id in elicitation_candidates(user_id)
        if movie_id not in set(exclude)
    ]
    if not candidates:
        return anchor_id, []

    features = collect_features(candidates + [anchor_id])
    adjacency = adjacency_scores(candidates, features, features.get(anchor_id, []))
    scored = []
    for movie_id in candidates:
        if adjacency.get(movie_id, 0.0) <= 0:
            continue
        taste, _ = score_movie(features.get(movie_id, []), profile)
        scored.append((taste + ADJACENCY_WEIGHT * adjacency[movie_id], movie_id))
    scored.sort(reverse=True)
    return anchor_id, [movie_id for _, movie_id in scored[:count]]
