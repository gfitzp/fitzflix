"""The "since you liked…" strip: taste-scored suggestions after a
positive rating.

What's left of the old /rate elicitation drive (retired for the
Recommendations page, #235): the session's last-response marker and
the enjoyment picks it unlocks on the just-rated film's movie page —
unseen candidates sharing features with the anchor, ranked by the
taste profile's score plus a class-weighted adjacency bonus.
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

# The last response unlocks the strip for an hour — long enough for
# a session, short enough that tomorrow starts fresh

LAST_TTL_SECONDS = 3600

# The retired drive's "No Opinion" marks still exclude their films
# while fresh, and wear off after this many years — the user may have
# seen the film (or remembered a verdict) since. Only "unseen"
# expires; "not interested" is permanent.

UNSEEN_RESURFACE_YEARS = 2

# How strongly a positive rating's adjacency pulls similar films
# forward in the suggestion strip

ADJACENCY_WEIGHT = 2.0

# After a positive rating, up to this many taste-scored suggestions
# appear ("since you liked X…")

SUGGESTION_COUNT = 3


def set_last_response(redis, user_id, movie_id, action, positive=False):
    """Remember the session's last response: action is one of rated /
    watchlist / not_interested, and a positive rating also unlocks the
    suggestion strip on that film's movie page."""

    redis.set(
        LAST_KEY.format(user_id=int(user_id)),
        json.dumps(
            {"movie_id": int(movie_id), "action": action, "positive": bool(positive)}
        ),
        ex=LAST_TTL_SECONDS,
    )


def last_response(redis, user_id):
    """The session's last response, or None."""

    payload = redis.get(LAST_KEY.format(user_id=int(user_id)))
    return json.loads(payload) if payload else None


def elicitation_candidates(user_id):
    """Movie ids the suggestion strip may offer: local full-feature
    films the user hasn't logged, minus watchlisted films (declared
    unseen-but-wanted) and films marked "No Opinion" within the
    resurface bar (older marks expire) — not-interested films are
    already out of local_candidates."""

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
    """movie_id -> feature overlap with the anchor film, class-weighted."""

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
    """(anchor movie id, suggested movie ids) after a positive rating,
    or (None, []).

    Enjoyment picks, not elicitation picks: unseen candidates that
    actually share features with the just-rated film, ranked by the
    taste profile's own score plus the adjacency bonus — the fresh
    rating's signal rides in through the adjacency term while the
    stored profile catches up in the background.
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
