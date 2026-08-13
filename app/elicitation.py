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
from app.models import UserMovieStatus, UserWatchlist
from app.recommendations import (
    FEATURE_CLASS_WEIGHTS,
    collect_features,
    local_candidates,
    score_movie,
    stored_profile,
)

SKIP_KEY = "fitzflix:elicit:skip:{user_id}"
LAST_KEY = "fitzflix:elicit:last:{user_id}"

# Skips rest a film for a week (the whole set's TTL refreshes on each
# skip) and stay in Redis — ephemeral by design. "Haven't seen" is
# permanent and lives in user_movie_status, so a cache flush can't
# forget it (#45b). The last response steers the next picks for an
# hour — long enough for a session, short enough that tomorrow starts
# fresh

SKIP_TTL_SECONDS = 7 * 86400
LAST_TTL_SECONDS = 3600

# How strongly the last response bends the ranking: a rating pulls
# similar films forward, "haven't seen" nudges the neighborhood away;
# watchlist adds and skips don't steer

ADJACENCY_WEIGHT = 2.0
UNSEEN_STEER_WEIGHT = -0.5

UP_NEXT_COUNT = 3

# After a positive rating, up to this many taste-scored suggestions
# appear ("since you liked X…") — enjoyment picks, unlike the drive's
# own information-value ranking

SUGGESTION_COUNT = 3


def _int_set(redis, key):
    """A Redis set's members as ints."""

    return {int(member) for member in redis.smembers(key)}


def mark_unseen(user_id, movie_id):
    """Record that the user has never seen this film — permanently out
    of the drive (they can always rate it from its movie page)."""

    exists = UserMovieStatus.query.filter_by(
        user_id=int(user_id), movie_id=int(movie_id), kind="unseen"
    ).first()
    if exists is None:
        db.session.add(
            UserMovieStatus(user_id=int(user_id), movie_id=int(movie_id), kind="unseen")
        )
        db.session.commit()


def mark_skipped(redis, user_id, movie_id):
    """Rest a film for a week; the set's TTL refreshes on every skip."""

    key = SKIP_KEY.format(user_id=int(user_id))
    redis.sadd(key, int(movie_id))
    redis.expire(key, SKIP_TTL_SECONDS)


def set_last_response(redis, user_id, movie_id, action, positive=False):
    """Remember the session's last response, which steers the next
    picks: action is one of rated / watchlist / unseen / skip, and a
    positive rating (or like) also unlocks the suggestion strip."""

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
    """Movie ids eligible for the drive: local full-feature films the
    user hasn't logged, minus watchlisted films (declared unseen-but-
    wanted), films marked "haven't seen", and recent skips —
    not-interested films are already out of local_candidates."""

    redis = current_app.redis
    watchlisted = {
        movie_id
        for (movie_id,) in db.session.query(UserWatchlist.movie_id).filter(
            UserWatchlist.user_id == int(user_id)
        )
    }
    unseen = {
        movie_id
        for (movie_id,) in db.session.query(UserMovieStatus.movie_id).filter(
            UserMovieStatus.user_id == int(user_id),
            UserMovieStatus.kind == "unseen",
        )
    }
    excluded = (
        watchlisted | unseen | _int_set(redis, SKIP_KEY.format(user_id=int(user_id)))
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


def next_films(user_id, count=1 + UP_NEXT_COUNT, exclude=()):
    """The next films to offer, most informative first, steered by the
    last response. Deterministic for a given state, so a reload shows
    the same card — every answer changes the state and the picks.
    `exclude` keeps the card from doubling as one of the suggestion
    strip's enjoyment picks."""

    candidates = [
        movie_id
        for movie_id in elicitation_candidates(user_id)
        if movie_id not in set(exclude)
    ]
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
