"""The Recommendations page's shelves (#235).

Each shelf is keyed by one to four shared criteria — a genre, a TMDB
keyword, a decade, an award won, a person in a taste-tracked role —
and is fronted by two ANCHOR films the user rated or liked that carry
every one of them ("Because you liked" must never name a film the
user hasn't passed a verdict on — Glenn, Aug 26 2026; watchlist adds
and bare unrated watches still feed the engine's scores, but they
can't face a shelf). Occasionally — at most one shelf per load, both
kinds combined — a shelf fronts a SINGLE anchor instead (#249),
targeted exactly where the pair rule blocks variety: a copref anchor
whose neighborhood pairs with no other anchor's, or a specific
feature (a person, keyword, or award — never a genre or decade) held
by exactly one rated-or-liked film. The shelf's suggestions are films the user has never logged or
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
    TMDBKeyword,
    UserMovieReview,
    UserWatchlist,
    movie_genres,
    movie_keywords,
)
from app.recommendations import (
    CREW_ROLE_JOBS,
    TOP_BILLING_CUTOFF,
    collect_features,
    copref_anchor_sims,
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

# How many shelves a page load aims for, how many films each shows
# (five suggestions — the anchor slot takes the row's first column),
# and the floor below which a criteria set isn't worth a shelf

SHELF_COUNT = 10
SHELF_SIZE = 5
MIN_SHELF_FILMS = 4
MAX_CRITERIA = 4

# Copref-pair shelves (Glenn's Aug 26 follow-up): up to this many of a
# load's shelves come from MovieLens co-preference instead of shared
# features — two high-weight anchor films, suggestions ranked by how
# similar they are to BOTH. Pairs are drawn from the user's top-weight
# interest films; the cap keeps the pair search quadratic-but-tiny.

COPREF_SHELF_COUNT = 3
COPREF_ANCHOR_POOL = 30

# Single-anchor shelves (#249): at most one per load across both
# kinds — one film is weaker evidence than two, so these stay
# occasional. A pairless copref anchor competes in the pair draw with
# its quality damped (its own-similarity sums run higher than a
# pair's min-blend, and pairs should still lead most loads), and a
# one-holder criteria seed may key a shelf only when its class is
# specific enough that a single loved film is real evidence — a
# person, a keyword, an award won, never a genre or decade

MAX_SINGLE_ANCHOR_SHELVES = 1
SINGLE_ANCHOR_DAMP = 0.5
SINGLE_SEED_CLASSES = frozenset(("keyword", "actor", "award") + tuple(CREW_ROLE_JOBS))

# No class may key more than this many of one load's shelves, so a
# cast-heavy profile still mixes genres, keywords, and awards in

MAX_SHELVES_PER_CLASS = 3

# The interest bar: on top of the rated-or-liked requirement below, an
# anchor's diary weight must clear this floor, so a film the user
# rated below their own mean never fronts a "Because you liked" shelf

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

# Keyword display casing (Glenn, Aug 26 2026): TMDB stores keywords
# lowercase, but "new york city", "fbi", and "world war ii" are not
# common nouns. The library's own overview prose knows the real
# casing, so a shelf-bound keyword resolves against mid-sentence
# occurrences there — adopted when one non-lowercase casing carries a
# clear majority, first-char-upper otherwise ("Heist"). Resolutions
# cache in Redis; only the few keywords actually chosen as criteria
# ever resolve, never the whole feature index.

KEYWORD_CASE_KEY = "fitzflix:kwcase:{name}"
KEYWORD_CASE_TTL = 30 * 86400
KEYWORD_CASE_MAJORITY = 0.6
KEYWORD_CASE_SAMPLE = 400


def keyword_display_name(name):
    """The keyword with real-world capitalization, learned from the
    overview text of films that use the phrase mid-sentence — "new
    york city" → "New York City", "fbi" → "FBI" — falling back to a
    capitalized first character for phrases prose keeps lowercase."""

    redis = current_app.redis
    cache_key = KEYWORD_CASE_KEY.format(name=name)
    cached = redis.get(cache_key)
    if cached:
        return cached.decode() if isinstance(cached, bytes) else cached

    pattern = re.compile(
        r"(?<![A-Za-z])" + re.escape(name).replace(r"\ ", r"\s+") + r"(?![A-Za-z])",
        re.I,
    )
    needle = name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    rows = (
        db.session.query(Movie.tmdb_overview)
        .filter(Movie.tmdb_overview.like(f"%{needle}%", escape="\\"))
        .limit(KEYWORD_CASE_SAMPLE)
        .all()
    )
    counts = {}
    total = 0
    for (overview,) in rows:
        if not overview:
            continue
        for match in pattern.finditer(overview):
            # A sentence-start capital is the sentence's, not the
            # phrase's — only mid-sentence occurrences vote
            head = overview[: match.start()].rstrip()
            if not head or head[-1] in ".!?—“\"'(":
                continue
            casing = re.sub(r"\s+", " ", match.group(0))
            counts[casing] = counts.get(casing, 0) + 1
            total += 1

    resolved = f"{name[:1].upper()}{name[1:]}"
    if total:
        best = max(counts, key=counts.get)
        if best != name and counts[best] / total >= KEYWORD_CASE_MAJORITY:
            resolved = best
    redis.set(cache_key, resolved, ex=KEYWORD_CASE_TTL)
    return resolved


def _shelf_label(cls, label):
    """The shelf wording for one feature: collect_features' labels
    recast so "Based on your interest in {label}" reads as a sentence
    ("Drama films", "films directed by Sidney Lumet", "the 1970s")."""

    if cls == "genre":
        return f"{label} films"
    if cls == "keyword":
        # The cheap default for the feature index; a keyword that
        # actually becomes a shelf criterion re-labels through
        # keyword_display_name at assembly time
        return f"“{label[:1].upper()}{label[1:]}” films"
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


def _rated_or_liked_ids(user_id):
    """Films the user has passed an actual verdict on — stars or a
    liked heart on some diary row. Only these may anchor a shelf
    (Glenn's rule, Aug 26 2026): a watchlist add or a bare unrated
    watch expresses interest and still feeds the engine's scores, but
    "Because you liked" must name a film the user really rated or
    liked."""

    return {
        movie_id
        for (movie_id,) in db.session.query(UserMovieReview.movie_id)
        .filter(UserMovieReview.user_id == int(user_id))
        .filter(UserMovieReview.movie_id.isnot(None))
        .filter(
            db.or_(
                UserMovieReview.rating.isnot(None),
                UserMovieReview.liked == True,
            )
        )
    }


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


def _copref_shelves(interest, pool, rng, count, shown):
    """Up to `count` copref shelves: two of the user's top-weight
    interest films whose MovieCopref neighbor lists jointly cover
    enough eligible films. Suggestions rank by min(sim to A, sim to B)
    — a film must sit close to BOTH anchors — and the criteria key
    `copref:{tmdbA}:{tmdbB}` lets the tile endpoint refill slots from
    the same joint list. An anchor that qualifies for NO pair may
    front a single-anchor shelf instead (#249) — `copref:{tmdbA}`,
    ranked by similarity to it alone, damped in the draw and capped at
    MAX_SINGLE_ANCHOR_SHELVES. Mutates `shown` like the criteria loop
    does.
    """

    top = sorted(interest.items(), key=lambda kv: kv[1], reverse=True)[
        :COPREF_ANCHOR_POOL
    ]
    tmdb_of = dict(
        db.session.query(Movie.id, Movie.tmdb_id)
        .filter(Movie.id.in_([movie_id for movie_id, _ in top] or [0]))
        .filter(Movie.tmdb_id.isnot(None))
    )
    anchors = [movie_id for movie_id, _ in top if movie_id in tmdb_of]
    sims = copref_anchor_sims([tmdb_of[movie_id] for movie_id in anchors])
    pool_by_tmdb = {
        tmdb_id: movie_id
        for movie_id, tmdb_id in db.session.query(Movie.id, Movie.tmdb_id)
        .filter(Movie.id.in_(list(pool) or [0]))
        .filter(Movie.tmdb_id.isnot(None))
    }

    # Each anchor's neighbors that are actually suggestible, then every
    # pair with a joint list deep enough for a shelf, scored by the
    # quality of its top picks so strong pairs lead the weighted draw

    neighbors = {
        movie_id: set(sims.get(tmdb_of[movie_id], ())) & set(pool_by_tmdb)
        for movie_id in anchors
    }
    pairs = []
    paired = set()
    for i, first in enumerate(anchors):
        for second in anchors[i + 1 :]:
            joint = neighbors[first] & neighbors[second]
            if len(joint) < MIN_SHELF_FILMS:
                continue
            sims_a = sims[tmdb_of[first]]
            sims_b = sims[tmdb_of[second]]
            quality = sum(
                sorted((min(sims_a[t], sims_b[t]) for t in joint), reverse=True)[
                    :SHELF_SIZE
                ]
            )
            pairs.append((quality, (first, second, joint)))
            paired.update((first, second))

    # Pairless anchors — the outlier tastes the intersection rule
    # shuts out — enter the same draw solo, damped so pairs lead
    for first in anchors:
        if first in paired or len(neighbors[first]) < MIN_SHELF_FILMS:
            continue
        own = sims[tmdb_of[first]]
        quality = SINGLE_ANCHOR_DAMP * sum(
            sorted((own[t] for t in neighbors[first]), reverse=True)[:SHELF_SIZE]
        )
        pairs.append((quality, (first, None, neighbors[first])))

    shelves = []
    used_anchors = set()
    singles = 0
    for first, second, joint in _weighted_order(rng, pairs):
        if len(shelves) >= count:
            break
        if first in used_anchors or second in used_anchors:
            continue
        if second is None and singles >= MAX_SINGLE_ANCHOR_SHELVES:
            continue
        sims_a = sims[tmdb_of[first]]
        sims_b = sims[tmdb_of[second]] if second is not None else sims_a
        ranked = sorted(
            (t for t in joint if pool_by_tmdb[t] not in shown),
            key=lambda t: min(sims_a[t], sims_b[t]),
            reverse=True,
        )
        if len(ranked) < MIN_SHELF_FILMS:
            continue
        picks = [pool_by_tmdb[t] for t in _top_heavy_sample(rng, ranked, SHELF_SIZE)]
        if second is None:
            key = f"copref:{tmdb_of[first]}"
            anchor_ids = [first]
            singles += 1
        else:
            key = f"copref:{tmdb_of[first]}:{tmdb_of[second]}"
            anchor_ids = [first, second]
        shelves.append(
            {
                "kind": "copref",
                "criteria": [(key, "loved alongside these")],
                "anchor_ids": anchor_ids,
                "movie_ids": picks,
            }
        )
        used_anchors.update(anchor_ids)
        shown.update(picks)
    return shelves


def build_shelves(user, count=SHELF_COUNT, rng=None):
    """One page load's shelves for a user, freshly drawn.

    Each shelf dict carries its kind ("criteria" or "copref"), its
    criteria [(key, label), ...], its anchor movie ids — two, or
    occasionally one (#249) — and its suggested movie ids. Anchors
    come only from films the user rated or liked. Copref shelves draw
    first (capped at COPREF_SHELF_COUNT); criteria shelves fill the
    rest: seeds are single features held by at least two
    anchor-eligible films (or by one, when the class is specific
    enough to carry a single-anchor shelf) and enough eligible
    candidates, and the winning seed's criteria greedily extend with
    features every anchor shares, as long as enough candidates carry
    the whole set — so anchors and suggestions always overlap on
    every criterion. Films never repeat across one load's shelves.
    """

    rng = rng or random.Random()
    user_id = int(user.id)
    verdicts = _rated_or_liked_ids(user_id)
    interest = {
        movie_id: weight
        for movie_id, weight in user_movie_weights(user_id).items()
        if weight >= ANCHOR_MIN_WEIGHT and movie_id in verdicts
    }
    if not interest:
        return []
    pool = eligible_films(user)
    if len(pool) < MIN_SHELF_FILMS:
        return []

    shown = set()
    copref_shelves = _copref_shelves(
        interest, pool, rng, min(COPREF_SHELF_COUNT, count), shown
    )

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
        if len(carriers.get(key, ())) < MIN_SHELF_FILMS:
            continue
        # A one-holder seed keys a single-anchor shelf (#249): real
        # evidence only for specific classes — one loved film says
        # little about a genre or a decade. SEED_SHRINKAGE already
        # keeps these underdogs in the draw
        if len(holder_ids) < 2 and classes[key] not in SINGLE_SEED_CLASSES:
            continue
        score = sum(interest[movie_id] for movie_id in holder_ids) / (
            len(holder_ids) + SEED_SHRINKAGE
        )
        if score > 0:
            seed_scores[key] = score
    if not seed_scores:
        return copref_shelves

    scores = stored_scores(current_app.redis, user_id)
    shelves = list(copref_shelves)
    singles = sum(1 for shelf in copref_shelves if len(shelf["anchor_ids"]) == 1)
    shelves_per_class = {}
    for seed in _weighted_order(rng, [(s, k) for k, s in seed_scores.items()]):
        if len(shelves) >= count:
            break
        cls = classes[seed]
        if shelves_per_class.get(cls, 0) >= MAX_SHELVES_PER_CLASS:
            continue
        # The single-anchor budget spans both kinds
        if len(holders[seed]) < 2 and singles >= MAX_SINGLE_ANCHOR_SHELVES:
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
                "kind": "criteria",
                "criteria": [
                    (key, _criterion_label(key, labels[key])) for key in criteria
                ],
                "anchor_ids": anchors,
                "movie_ids": picks,
            }
        )
        shown.update(picks)
        shelves_per_class[cls] = shelves_per_class.get(cls, 0) + 1
        if len(anchors) == 1:
            singles += 1

    # Build order is not page order: copref shelves draw first only to
    # claim their films, and pinning them to the top would make every
    # load open the same way (Glenn, Aug 26 2026) — the kinds mix
    # randomly down the page instead

    rng.shuffle(shelves)
    return shelves


def _criterion_label(key, label):
    """The final display label for one chosen criterion: keywords swap
    their cheap first-upper label for the overview-derived casing
    ("“New York City” films", "“FBI” films"); every other class keeps
    the index label."""

    if not key.startswith("keyword:"):
        return label
    row = db.session.get(TMDBKeyword, int(key.split(":", 1)[1]))
    if row is None:
        return label
    return f"“{keyword_display_name(row.name)}” films"


def parse_criteria(raw):
    """[(class, value)] from the tile endpoint's comma-joined criteria
    keys, or None when any key is malformed. A copref key
    (`copref:{tmdbA}:{tmdbB}`, or `copref:{tmdbA}` for a
    single-anchor shelf) always stands alone — a copref shelf has
    exactly that one criterion — and parses to ("copref", (tmdbA,
    tmdbB)) or ("copref", (tmdbA,))."""

    keys = [key for key in (raw or "").split(",") if key]
    if not keys or len(keys) > MAX_CRITERIA:
        return None
    if any(key.startswith("copref:") for key in keys):
        if len(keys) != 1:
            return None
        parts = keys[0].split(":")
        if len(parts) not in (2, 3) or not all(p.isdigit() for p in parts[1:]):
            return None
        return [("copref", tuple(int(p) for p in parts[1:]))]
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


def _copref_replacement(user, anchors, exclude):
    """The next movie id for a copref shelf's slot: the eligible film
    most similar to EVERY anchor — both of the pair, or the one film
    of a single-anchor shelf — that isn't already showing."""

    sims = copref_anchor_sims(anchors)
    lists = [sims.get(tmdb_id, {}) for tmdb_id in anchors]
    shared = set(lists[0]).intersection(*(set(other) for other in lists[1:]))
    joint = {
        tmdb_id: min(neighbors[tmdb_id] for neighbors in lists) for tmdb_id in shared
    }
    if not joint:
        return None
    pool = set(eligible_films(user)) - set(exclude)
    pool_by_tmdb = {
        tmdb_id: movie_id
        for movie_id, tmdb_id in db.session.query(Movie.id, Movie.tmdb_id)
        .filter(Movie.id.in_(list(pool) or [0]))
        .filter(Movie.tmdb_id.in_(list(joint)))
    }
    if not pool_by_tmdb:
        return None
    return pool_by_tmdb[max(pool_by_tmdb, key=lambda tmdb_id: joint[tmdb_id])]


def replacement_film(user, criteria, exclude=()):
    """The next movie id for a shelf slot: the best-scored eligible
    film carrying every criterion that isn't already showing — or None
    when the criteria set is exhausted, which closes the slot. Copref
    shelves rank by joint similarity to their anchors instead of the
    engine score, matching the shelf's own ordering."""

    if criteria and criteria[0][0] == "copref":
        return _copref_replacement(user, criteria[0][1], exclude)

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
