"""The Recommendations page's shelves (#235).

Each shelf is keyed by one to four shared criteria — a genre, a TMDB
keyword, a decade, an award won, a person in a taste-tracked role —
and is captioned by two ANCHOR films the user has expressed interest
in (rated, liked, watched, or watchlisted) that carry every one of
them. The shelf's suggestions are films the user has never logged or
waved off that carry the same criteria and can actually be watched
tonight: owned locally, or streaming on one of the user's services
(record-backed films only, answered from the availability cache the
nightly refresh keeps full — never a render-time TMDB call).

Unlike the landing rails, nothing here is frozen for the day: every
reload draws a fresh set of shelves (weighted toward the user's
strongest interests, but random), and acting on a suggestion — rating
it, watchlisting it, waving it off — replaces just that film with the
next candidate matching the shelf's criteria, through the tile
endpoint. The page replaced the old /rate drive: rating suggestions
in place is how the taste profile deepens now.
"""

import random
import re

from flask import current_app

from app import db
from app.models import (
    Movie,
    MovieAward,
    MovieCast,
    MovieCrew,
    UserWatchlist,
    movie_genres,
    movie_keywords,
)
from app.recommendations import (
    CREW_ROLE_JOBS,
    TOP_BILLING_CUTOFF,
    collect_features,
    local_candidates,
    scoreable_records,
    stored_scores,
    user_movie_weights,
)
from app.streaming import (
    batch_title_availability,
    streaming_matches,
    user_provider_ids,
)

# The feature classes a shelf may be keyed on: everything the taste
# engine tracks except language (an "EN-language films" shelf keyed on
# the library's default tells the user nothing), plus the award class
# added below, which the engine keeps as a prior rather than a feature

SHELF_CLASSES = ("genre", "keyword", "decade", "actor") + tuple(CREW_ROLE_JOBS)

# How many shelves a page load aims for, how many films each shows,
# and the floor below which a criteria set isn't worth a shelf

SHELF_COUNT = 5
SHELF_SIZE = 6
MIN_SHELF_FILMS = 4
MAX_CRITERIA = 4

# No class may key more than this many of one load's shelves, so a
# cast-heavy profile still mixes genres, keywords, and awards in

MAX_SHELVES_PER_CLASS = 2

# The interest bar: a film anchors a shelf when the diary weights say
# the user expressed interest in it — a watchlist add (0.2) is the
# mildest signal that counts, so bare watches and positive ratings
# clear it and dislikes never do

ANCHOR_MIN_WEIGHT = 0.2

# Seed features rank by their holders' summed interest weights shrunk
# toward zero — the same Bayesian move the profile makes — so one
# loved film can't put its every keyword ahead of a genre the user
# has liked a dozen times

SEED_SHRINKAGE = 3.0

# Wikidata award ids (Q-ids) ride in criteria keys and back through
# the tile endpoint's query string; anything else is malformed

AWARD_ID_PATTERN = re.compile(r"^[A-Za-z0-9]{1,32}$")

# TMDB's technical bookkeeping keywords: real taste features to the
# engine, nonsense as a shelf headline ("Based on your interest in
# “duringcreditsstinger” films" — the first live render, Aug 2026)

KEYWORD_DENYLIST = {"aftercreditsstinger", "duringcreditsstinger"}


def _shelf_label(cls, label):
    """The shelf wording for one feature: collect_features' labels
    recast so "Based on your interest in {label}" reads as a sentence
    ("Drama films", "films directed by Sidney Lumet", "the 1970s")."""

    if cls == "genre":
        return f"{label} films"
    if cls == "keyword":
        return f"“{label}” films"
    if cls == "decade":
        return f"the {label}"
    if cls == "actor":
        return f"films starring {label}"
    return f"films {label}"


def shelf_features(movie_ids):
    """{movie_id: [(class, key, label)]} in the shelves' own feature
    space: the engine's features narrowed to the shelf classes, with
    labels recast for shelf sentences, plus an award feature per
    distinct award the film has WON (nominations don't key shelves —
    the issue asks for awards won, and "nominee" shelves would drown
    the winners)."""

    features = {}
    for movie_id, rows in collect_features(movie_ids).items():
        features[movie_id] = [
            (cls, key, _shelf_label(cls, label))
            for cls, key, label in rows
            if cls in SHELF_CLASSES
            and not (cls == "keyword" and label in KEYWORD_DENYLIST)
        ]
    if not movie_ids:
        return features

    seen = set()
    for movie_id, award_id, name in (
        db.session.query(
            MovieAward.movie_id, MovieAward.award_id, MovieAward.award_name
        )
        .filter(MovieAward.movie_id.in_(movie_ids))
        .filter(MovieAward.win == True)
        .filter(MovieAward.award_id.isnot(None), MovieAward.award_name.isnot(None))
    ):
        # A film can win the same award in several rows (year variants);
        # a feature counts once per film, like the engine's dedupe
        if (movie_id, award_id) in seen or not AWARD_ID_PATTERN.match(award_id):
            continue
        seen.add((movie_id, award_id))
        features.setdefault(movie_id, []).append(
            ("award", f"award:{award_id}", f"{name} winners")
        )
    return features


def _watchlisted_ids(user_id):
    """The user's watchlisted movie ids — wanted already, so never
    suggested; watchlisting a suggestion is what swaps it out."""

    return {
        movie_id
        for (movie_id,) in db.session.query(UserWatchlist.movie_id).filter(
            UserWatchlist.user_id == int(user_id)
        )
    }


def eligible_films(user):
    """The movie ids a shelf may suggest: local full-feature films the
    user hasn't logged, waved off, or watchlisted, plus record-backed
    films currently streaming on one of the user's services — answered
    from the availability cache only, so a film whose availability
    hasn't been fetched yet simply isn't suggested until tonight's
    refresh covers it."""

    user_id = int(user.id)
    watchlisted = _watchlisted_ids(user_id)
    pool = [
        movie_id
        for movie_id in local_candidates(user_id)
        if movie_id not in watchlisted
    ]

    provider_ids = user_provider_ids(user)
    if provider_ids:
        extras = [
            movie_id
            for movie_id in scoreable_records(user_id)
            if movie_id not in watchlisted
        ]
        tmdb_of = dict(
            db.session.query(Movie.id, Movie.tmdb_id)
            .filter(Movie.id.in_(extras or [0]))
            .filter(Movie.tmdb_id.isnot(None))
        )
        availability, _ = batch_title_availability(tmdb_of.values(), fetch_limit=0)
        for movie_id in extras:
            tmdb_id = tmdb_of.get(movie_id)
            if tmdb_id and streaming_matches(availability.get(tmdb_id), provider_ids):
                pool.append(movie_id)
    return pool


def _weighted_order(rng, weighted_items):
    """[(weight, item)] in weighted-random order, heaviest-favored,
    without replacement (Efraimidis–Spirakis keys) — the one sampler
    behind seed order and anchor picks, so reloads vary while strong
    interests still lead most days."""

    keyed = sorted(
        (
            (rng.random() ** (1.0 / max(weight, 1e-6)), item)
            for weight, item in weighted_items
        ),
        reverse=True,
    )
    return [item for _, item in keyed]


def _top_heavy_sample(rng, ranked, count, decay=0.93):
    """`count` items from a best-first list, geometrically favoring the
    top (the landing rails' rotate_daily recipe, with this page's own
    rng), returned in original rank order."""

    if len(ranked) <= count:
        return list(ranked)
    pool = list(enumerate(ranked))
    selected = []
    while pool and len(selected) < count:
        weights = [decay**rank for rank, _ in pool]
        pick = rng.choices(range(len(pool)), weights=weights, k=1)[0]
        selected.append(pool.pop(pick))
    selected.sort(key=lambda pair: pair[0])
    return [item for _, item in selected]


def build_shelves(user, count=SHELF_COUNT, rng=None):
    """One page load's shelves for a user, freshly drawn.

    Each shelf dict carries its criteria [(key, label), ...], its two
    anchor movie ids, and its suggested movie ids. Seeds are single
    features held by at least two interest films and enough eligible
    candidates; the winning seed's criteria then greedily extend with
    features BOTH anchors share, as long as enough candidates carry
    the whole set — so anchors and suggestions always overlap on every
    criterion. Films never repeat across one load's shelves.
    """

    rng = rng or random.Random()
    user_id = int(user.id)
    interest = {
        movie_id: weight
        for movie_id, weight in user_movie_weights(user_id).items()
        if weight >= ANCHOR_MIN_WEIGHT
    }
    if len(interest) < 2:
        return []
    pool = eligible_films(user)
    if len(pool) < MIN_SHELF_FILMS:
        return []

    features = shelf_features(list(set(pool) | set(interest)))
    holders = {}  # feature key -> interest movie ids carrying it
    carriers = {}  # feature key -> eligible movie ids carrying it
    labels = {}
    classes = {}
    for movie_id in interest:
        for cls, key, label in features.get(movie_id, []):
            holders.setdefault(key, set()).add(movie_id)
            labels[key] = label
            classes[key] = cls
    for movie_id in pool:
        for cls, key, label in features.get(movie_id, []):
            carriers.setdefault(key, set()).add(movie_id)
            labels[key] = label
            classes[key] = cls

    seed_scores = {}
    for key, holder_ids in holders.items():
        if len(holder_ids) < 2 or len(carriers.get(key, ())) < MIN_SHELF_FILMS:
            continue
        score = sum(interest[movie_id] for movie_id in holder_ids) / (
            len(holder_ids) + SEED_SHRINKAGE
        )
        if score > 0:
            seed_scores[key] = score
    if not seed_scores:
        return []

    scores = stored_scores(current_app.redis, user_id)
    shelves = []
    shown = set()
    shelves_per_class = {}
    for seed in _weighted_order(rng, [(s, k) for k, s in seed_scores.items()]):
        if len(shelves) >= count:
            break
        cls = classes[seed]
        if shelves_per_class.get(cls, 0) >= MAX_SHELVES_PER_CLASS:
            continue
        candidates = carriers[seed] - shown
        if len(candidates) < MIN_SHELF_FILMS:
            continue

        anchors = _weighted_order(
            rng, [(interest[movie_id], movie_id) for movie_id in holders[seed]]
        )[:2]
        anchor_keys = set.intersection(
            *({key for _, key, _ in features.get(movie_id, [])} for movie_id in anchors)
        )

        criteria = [seed]
        extras = sorted(anchor_keys - {seed})
        rng.shuffle(extras)
        for extra in extras:
            if len(criteria) >= MAX_CRITERIA:
                break
            narrowed = candidates & carriers.get(extra, set())
            if len(narrowed) >= MIN_SHELF_FILMS:
                criteria.append(extra)
                candidates = narrowed

        ranked = sorted(
            candidates, key=lambda movie_id: scores.get(movie_id, 0.0), reverse=True
        )
        picks = _top_heavy_sample(rng, ranked, SHELF_SIZE)
        shelves.append(
            {
                "criteria": [(key, labels[key]) for key in criteria],
                "anchor_ids": anchors,
                "movie_ids": picks,
            }
        )
        shown.update(picks)
        shelves_per_class[cls] = shelves_per_class.get(cls, 0) + 1
    return shelves


def parse_criteria(raw):
    """[(class, value)] from the tile endpoint's comma-joined criteria
    keys, or None when any key is malformed."""

    keys = [key for key in (raw or "").split(",") if key]
    if not keys or len(keys) > MAX_CRITERIA:
        return None
    parsed = []
    for key in keys:
        cls, _, value = key.partition(":")
        if cls == "award":
            if not AWARD_ID_PATTERN.match(value):
                return None
        elif cls in SHELF_CLASSES:
            if not value.isdigit():
                return None
        else:
            return None
        parsed.append((cls, value))
    return parsed


def _criterion_movie_ids(cls, value):
    """The movie ids carrying one criterion, queried directly — the
    tile endpoint's cheap path, so a replacement never rebuilds the
    whole page's feature index."""

    if cls == "genre":
        rows = db.session.query(movie_genres.c.movie_id).filter(
            movie_genres.c.genre_id == int(value)
        )
    elif cls == "keyword":
        rows = db.session.query(movie_keywords.c.movie_id).filter(
            movie_keywords.c.keyword_id == int(value)
        )
    elif cls == "decade":
        rows = db.session.query(Movie.id).filter(
            Movie.year >= int(value), Movie.year < int(value) + 10
        )
    elif cls == "award":
        rows = db.session.query(MovieAward.movie_id).filter(
            MovieAward.award_id == value, MovieAward.win == True
        )
    elif cls == "actor":
        rows = db.session.query(MovieCast.movie_id).filter(
            MovieCast.credit_id == int(value),
            MovieCast.billing_order < TOP_BILLING_CUTOFF,
        )
    else:
        rows = db.session.query(MovieCrew.movie_id).filter(
            MovieCrew.credit_id == int(value),
            MovieCrew.job.in_(list(CREW_ROLE_JOBS[cls][0])),
        )
    return {movie_id for (movie_id,) in rows}


def replacement_film(user, criteria, exclude=()):
    """The next movie id for a shelf slot: the best-scored eligible
    film carrying every criterion that isn't already showing — or None
    when the criteria set is exhausted, which closes the slot."""

    matching = None
    for cls, value in criteria:
        ids = _criterion_movie_ids(cls, value)
        matching = ids if matching is None else matching & ids
        if not matching:
            return None
    candidates = (matching & set(eligible_films(user))) - set(exclude)
    if not candidates:
        return None
    scores = stored_scores(current_app.redis, int(user.id))
    return max(candidates, key=lambda movie_id: scores.get(movie_id, 0.0))
