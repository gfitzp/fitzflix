"""Taste elicitation: the engine behind the /rate drive.

Netflix-onboarding-style: present library films the user hasn't
logged, gather ratings as ordinary diary rows, and let every response
steer what's offered next — a rating pulls taste-adjacent films
forward (shared directors, genres, decades), "haven't seen" steers
away from the same neighborhood, a watchlist add files the film as
unseen-but-wanted, and a skip rests the film for a week.

Candidates rank by INFORMATION VALUE: how much a rating would teach
the taste profile. A feature's value is its reach across the unrated
library damped by the diary evidence the profile already holds for
it, so an unrated director with a dozen films on the shelf surfaces
before yet another film from a genre the profile knows cold.
"""

import json
import math

from flask import current_app

from app import db
from app.models import UserWatchlist
from app.recommendations import (
    FEATURE_CLASS_WEIGHTS,
    collect_features,
    local_candidates,
    stored_profile,
)

UNSEEN_KEY = "fitzflix:elicit:unseen:{user_id}"
SKIP_KEY = "fitzflix:elicit:skip:{user_id}"
LAST_KEY = "fitzflix:elicit:last:{user_id}"

# Skips rest a film for a week (the whole set's TTL refreshes on each
# skip); "haven't seen" is permanent. The last response steers the
# next picks for an hour — long enough for a session, short enough
# that tomorrow starts fresh

SKIP_TTL_SECONDS = 7 * 86400
LAST_TTL_SECONDS = 3600

# How strongly the last response bends the ranking: a rating pulls
# similar films forward, "haven't seen" nudges the neighborhood away;
# watchlist adds and skips don't steer

ADJACENCY_WEIGHT = 2.0
UNSEEN_STEER_WEIGHT = -0.5

UP_NEXT_COUNT = 3


def _int_set(redis, key):
    """A Redis set's members as ints."""

    return {int(member) for member in redis.smembers(key)}


def mark_unseen(redis, user_id, movie_id):
    """Record that the user has never seen this film — permanently out
    of the drive (they can always rate it from its movie page)."""

    redis.sadd(UNSEEN_KEY.format(user_id=int(user_id)), int(movie_id))


def mark_skipped(redis, user_id, movie_id):
    """Rest a film for a week; the set's TTL refreshes on every skip."""

    key = SKIP_KEY.format(user_id=int(user_id))
    redis.sadd(key, int(movie_id))
    redis.expire(key, SKIP_TTL_SECONDS)


def set_last_response(redis, user_id, movie_id, action):
    """Remember the session's last response, which steers the next
    picks: action is one of rated / watchlist / unseen / skip."""

    redis.set(
        LAST_KEY.format(user_id=int(user_id)),
        json.dumps({"movie_id": int(movie_id), "action": action}),
        ex=LAST_TTL_SECONDS,
    )


def last_response(redis, user_id):
    """The session's last response, or None."""

    payload = redis.get(LAST_KEY.format(user_id=int(user_id)))
    return json.loads(payload) if payload else None


def elicitation_candidates(user_id):
    """Movie ids eligible for the drive: local full-feature films the
    user hasn't logged, minus watchlisted films (declared unseen-but-
    wanted), films marked "haven't seen", and recent skips."""

    redis = current_app.redis
    watchlisted = {
        movie_id
        for (movie_id,) in db.session.query(UserWatchlist.movie_id).filter(
            UserWatchlist.user_id == int(user_id)
        )
    }
    excluded = (
        watchlisted
        | _int_set(redis, UNSEEN_KEY.format(user_id=int(user_id)))
        | _int_set(redis, SKIP_KEY.format(user_id=int(user_id)))
    )
    return [
        movie_id for movie_id in local_candidates(user_id) if movie_id not in excluded
    ]


def information_scores(candidates, features, profile):
    """movie_id -> how much rating the film would teach the profile.

    Each feature contributes its class weight times the square root of
    its reach (how many candidate films carry it — damped, or broad
    genres would drown everything) divided by the diary evidence the
    profile already holds for it. Fresh features on well-represented
    people and genres score highest.
    """

    affinities = (profile or {}).get("affinities", {})
    reach = {}
    for movie_id in candidates:
        for _, key, _ in features.get(movie_id, []):
            reach[key] = reach.get(key, 0) + 1

    scores = {}
    for movie_id in candidates:
        total = 0.0
        for cls, key, _ in features.get(movie_id, []):
            weight = FEATURE_CLASS_WEIGHTS.get(cls, 0.0)
            evidence = (affinities.get(key) or {}).get("count", 0)
            total += weight * math.sqrt(reach.get(key, 0)) / (1.0 + evidence)
        scores[movie_id] = total
    return scores


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


def next_films(user_id, count=1 + UP_NEXT_COUNT):
    """The next films to offer, most informative first, steered by the
    last response. Deterministic for a given state, so a reload shows
    the same card — every answer changes the state and the picks."""

    candidates = elicitation_candidates(user_id)
    if not candidates:
        return []
    redis = current_app.redis
    profile = stored_profile(redis, user_id)
    last = last_response(redis, user_id)

    feature_ids = list(candidates)
    anchor_id = last["movie_id"] if last else None
    if anchor_id is not None and anchor_id not in candidates:
        feature_ids.append(anchor_id)
    features = collect_features(feature_ids)

    scores = information_scores(candidates, features, profile)
    if anchor_id is not None:
        direction = {
            "rated": ADJACENCY_WEIGHT,
            "unseen": UNSEEN_STEER_WEIGHT,
        }.get(last["action"], 0.0)
        if direction:
            adjacency = adjacency_scores(
                candidates, features, features.get(anchor_id, [])
            )
            for movie_id in candidates:
                scores[movie_id] += direction * adjacency.get(movie_id, 0.0)

    ranked = sorted(
        candidates, key=lambda movie_id: scores.get(movie_id, 0.0), reverse=True
    )
    return ranked[:count]
