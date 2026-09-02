"""Build the shelves of the Recommendations page (#235).

Each shelf has a key of 1 to 4 shared criteria. A criterion is a
genre, a TMDB keyword, a decade, an award won, or a person in a role
that the taste engine tracks. Two ANCHOR films front each shelf. The
user rated or liked these films, and they have all the criteria.
"Because you liked" must never name a film without a verdict from the
user (Glenn, 2026-08-26). A watchlist add or an unrated watch still
feeds the scores of the engine. But those films cannot front a shelf.
Sometimes a shelf fronts a SINGLE anchor (#249). This occurs at most
1 time per load, for both kinds together. It applies only where the
pair rule blocks variety. The first case is a copref anchor whose
neighborhood pairs with no other anchor. The second case is a
specific feature (a person, a keyword, or an award, never a genre or
a decade) that exactly 1 rated-or-liked film has. The suggestions of
a shelf are films that the user never logged or dismissed. They have
the same criteria, and the user can watch them tonight. The film is
in the local library, or it streams on one of the services of the
user. Only films with records qualify. The answer comes from the
availability cache that the nightly refresh keeps full. There is
never a TMDB call at render time.

Unlike the landing rails, nothing here is frozen for the day. Each
reload draws a new set of shelves. The criteria shelves have weights
toward the strongest interests of the user. The copref anchors and
pairs are uniformly random. When the user acts on a suggestion (a
rating, a watchlist add, or a dismissal), the tile endpoint replaces
only that film with the next candidate that matches the criteria of
the shelf. This page replaced the old /rate drive. A rating of a
suggestion in place is now how the taste profile grows.
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

# These are the feature classes that can key a shelf. They are all the
# classes that the taste engine tracks, except language. An "EN-language
# films" shelf with the default language of the library tells the user
# nothing. The award class is added below. The engine keeps awards as a
# prior, not as a feature

SHELF_CLASSES = ("genre", "keyword", "decade", "actor") + tuple(CREW_ROLE_JOBS)

# These are the number of shelves that a page load tries to build, the
# number of films that each shelf shows (5 suggestions, because the
# anchor slot takes the first column of the row), and the minimum
# number of films for a criteria set to get a shelf

SHELF_COUNT = 10
SHELF_SIZE = 5
MIN_SHELF_FILMS = 4
MAX_CRITERIA = 4

# Copref-pair shelves (follow-up from Glenn, 2026-08-26): up to this
# many shelves of a load come from the MovieLens co-preference, not from
# shared features. They have 2 anchor films. The suggestions rank by
# their similarity to BOTH anchors. The anchors and the valid pairs
# draw uniformly at random (Glenn, 2026-08-28: the old top-weight pool
# plus the quality-weighted draw showed the same few favorites with
# dense neighborhoods on each reload). Since 2026-08-30, the pool is
# the WHOLE rated-or-liked interest set of the user. The cap is a cost
# limit, not a taste filter. The full-library pair scan measured
# approximately 26 ms at 302 anchors. Examine this again if the
# interest set grows past the cap.

COPREF_SHELF_COUNT = 3
COPREF_ANCHOR_POOL = 10_000

# Single-anchor shelves (#249): at most 1 per load across both kinds.
# One film is weaker evidence than 2 films. Thus, these shelves stay
# rare. A copref anchor without a pair competes in the pair draw with a
# damped weight. Pairs must still lead most loads. A criteria seed with
# 1 holder can key a shelf only if its class is specific. Then a single
# loved film is real evidence. The class is a person, a keyword, or an
# award won, never a genre or a decade

MAX_SINGLE_ANCHOR_SHELVES = 1
SINGLE_ANCHOR_DAMP = 0.5
SINGLE_SEED_CLASSES = frozenset(("keyword", "actor", "award") + tuple(CREW_ROLE_JOBS))

# No class can key more than this many shelves of 1 load. Thus, a
# profile with many actors still mixes in genres, keywords, and awards

MAX_SHELVES_PER_CLASS = 3

# This is the interest bar. In addition to the rated-or-liked
# requirement below, the diary weight of an anchor must be above this
# floor. Thus, a film that the user rated below their own mean never
# fronts a "Because you liked" shelf

ANCHOR_MIN_WEIGHT = 0.2

# The seed features rank by the sum of the interest weights of their
# holders, shrunk toward zero. This is the same Bayesian step that the
# profile makes. Thus, 1 loved film cannot put each of its keywords in
# front of a genre that the user liked 12 times

SEED_SHRINKAGE = 3.0

# The Wikidata award ids (Q-ids) go in the criteria keys and come back
# through the query string of the tile endpoint. All other values are
# malformed

AWARD_ID_PATTERN = re.compile(r"^[A-Za-z0-9]{1,32}$")

# These are the technical bookkeeping keywords of TMDB. They are real
# taste features for the engine. They make no sense as a shelf headline
# ("Based on your interest in “duringcreditsstinger” films" was the
# first live render, 2026-08)

KEYWORD_DENYLIST = {"aftercreditsstinger", "duringcreditsstinger"}

# Keyword display casing (Glenn, 2026-08-26): TMDB stores the keywords
# in lowercase. But "new york city", "fbi", and "world war ii" are not
# common nouns. The overview prose of the library knows the real
# casing. Thus, a keyword for a shelf resolves against the mid-sentence
# occurrences in that prose. Fitzflix adopts a casing that is not
# lowercase if it has a clear majority. Otherwise, it makes the first
# character uppercase ("Heist"). The resolutions stay in the Redis
# cache. Only the few keywords chosen as criteria resolve. The whole
# feature index never resolves.

KEYWORD_CASE_KEY = "fitzflix:kwcase:{name}"
KEYWORD_CASE_TTL = 30 * 86400
KEYWORD_CASE_MAJORITY = 0.6
KEYWORD_CASE_SAMPLE = 400


def keyword_display_name(name):
    """Return the keyword with its real-world capitalization.

    This function learns the casing from the overview text of the
    films that use the phrase in the middle of a sentence. "new york
    city" becomes "New York City", and "fbi" becomes "FBI". If the
    prose keeps the phrase in lowercase, this function makes the first
    character uppercase."""

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
            # A capital at the start of a sentence belongs to the
            # sentence, not to the phrase. Only the mid-sentence
            # occurrences vote
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
    """Return the shelf wording for 1 feature.

    This function changes the labels of collect_features. Thus, "Based
    on your interest in {label}" reads as a sentence ("Drama films",
    "films directed by Sidney Lumet", "the 1970s")."""

    if cls == "genre":
        return f"{label} films"
    if cls == "keyword":
        # This is the cheap default for the feature index. A keyword
        # that becomes a shelf criterion gets a new label through
        # keyword_display_name at assembly time
        return f"“{label[:1].upper()}{label[1:]}” films"
    if cls == "decade":
        return f"the {label}"
    if cls == "actor":
        return f"films starring {label}"
    return f"films {label}"


def shelf_features(movie_ids):
    """Return {movie_id: [(class, key, label)]} in the feature space of the shelves.

    These are the features of the engine, limited to the shelf classes.
    The labels are changed for the shelf sentences. There is also 1
    award feature for each different award that the film WON. A
    nomination does not key a shelf. The issue asks for awards won, and
    "nominee" shelves would hide the winners."""

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
        # A film can win the same award in several rows (year variants).
        # A feature counts 1 time per film, like the engine removes
        # duplicates
        if (movie_id, award_id) in seen or not AWARD_ID_PATTERN.match(award_id):
            continue
        seen.add((movie_id, award_id))
        features.setdefault(movie_id, []).append(
            ("award", f"award:{award_id}", f"{name} winners")
        )
    return features


def _rated_or_liked_ids(user_id):
    """Return the films with a real verdict from the user.

    A verdict is a star rating or a liked heart on some diary row. Only
    these films can anchor a shelf (rule from Glenn, 2026-08-26). A
    watchlist add or an unrated watch shows interest, and it still
    feeds the scores of the engine. But "Because you liked" must name a
    film that the user really rated or liked."""

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
    """Return the movie ids on the watchlist of the user.

    The user already wants these films. Thus, Fitzflix never suggests
    them. A watchlist add of a suggestion replaces that suggestion."""

    return {
        movie_id
        for (movie_id,) in db.session.query(UserWatchlist.movie_id).filter(
            UserWatchlist.user_id == int(user_id)
        )
    }


def eligible_films(user):
    """Return the movie ids that a shelf can suggest.

    These are the local full-feature films that the user did not log,
    dismiss, or put on the watchlist. They also include the films with
    records that stream now on one of the services of the user. The
    answer comes only from the availability cache. Thus, if Fitzflix
    did not fetch the availability of a film yet, it does not suggest
    the film until the refresh tonight covers it."""

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
            if tmdb_id and streaming_matches(
                availability.get(tmdb_id), provider_ids, tmdb_id=tmdb_id
            ):
                pool.append(movie_id)
    return pool


def _weighted_order(rng, weighted_items):
    """Return the items of [(weight, item)] in weighted-random order.

    The order prefers the heaviest items, without replacement
    (Efraimidis-Spirakis keys). This is the one sampler behind the seed
    order and the anchor picks. Thus, the reloads vary, but the strong
    interests still lead on most days."""

    keyed = sorted(
        (
            (rng.random() ** (1.0 / max(weight, 1e-6)), item)
            for weight, item in weighted_items
        ),
        reverse=True,
    )
    return [item for _, item in keyed]


def _top_heavy_sample(rng, ranked, count, decay=0.93):
    """Return `count` items from a best-first list, in the original rank order.

    The sample prefers the top geometrically. This is the rotate_daily
    recipe of the landing rails, with the rng of this page."""

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
    """Return up to `count` copref shelves.

    Each shelf has 2 rated-or-liked interest films of the user. Their
    MovieCopref neighbor lists together cover a sufficient number of
    eligible films. The suggestions rank by min(sim to A, sim to B). A
    film must be close to BOTH anchors. The criteria key
    `copref:{tmdbA}:{tmdbB}` lets the tile endpoint fill the slots
    again from the same joint list. This function samples the anchors
    uniformly from the whole interest set on each load. It draws the
    valid pairs uniformly. Thus, the reloads show new pairs, not the
    same few favorites with dense neighborhoods. A pair must share a
    TMDB genre. Films with no genre data pass. The shelves of 1 load
    never repeat a common-ground genre. Each shelf has its shared genre
    ids for the subtitle of the page. An anchor that qualifies for NO
    pair can front a single-anchor shelf (#249). Its key is
    `copref:{tmdbA}`. The rank is the similarity to that anchor alone.
    Its weight is damped in the draw, and MAX_SINGLE_ANCHOR_SHELVES
    caps it. This function changes `shown` in the same way as the
    criteria loop.
    """

    sampled = rng.sample(sorted(interest), min(len(interest), COPREF_ANCHOR_POOL))
    tmdb_of = dict(
        db.session.query(Movie.id, Movie.tmdb_id)
        .filter(Movie.id.in_(sampled or [0]))
        .filter(Movie.tmdb_id.isnot(None))
    )
    anchors = [movie_id for movie_id in sampled if movie_id in tmdb_of]
    sims = copref_anchor_sims([tmdb_of[movie_id] for movie_id in anchors])
    pool_by_tmdb = {
        tmdb_id: movie_id
        for movie_id, tmdb_id in db.session.query(Movie.id, Movie.tmdb_id)
        .filter(Movie.id.in_(list(pool) or [0]))
        .filter(Movie.tmdb_id.isnot(None))
    }

    # First, the neighbors of each anchor that Fitzflix can suggest.
    # Then, each pair with a joint list that is large enough for a
    # shelf. All pairs have the same weight. Thus, the draw is a uniform
    # shuffle of the valid pairs

    neighbors = {
        movie_id: set(sims.get(tmdb_of[movie_id], ())) & set(pool_by_tmdb)
        for movie_id in anchors
    }

    # The anchors must share a genre (Glenn, 2026-08-29). Two popular
    # films whose 100-neighbor lists overlap only by chance must not
    # front a shelf together. Films with no genre data on one side are
    # not blocked. After #251, that is a small set. The shared set also
    # feeds the subtitle of the shelf ("Their common ground: …") and the
    # per-load genre check below.

    genres_of = {}
    for movie_id, genre_id in db.session.query(
        movie_genres.c.movie_id, movie_genres.c.genre_id
    ).filter(movie_genres.c.movie_id.in_(anchors or [0])):
        genres_of.setdefault(movie_id, set()).add(genre_id)

    pairs = []
    paired = set()
    for i, first in enumerate(anchors):
        for second in anchors[i + 1 :]:
            genres_a = genres_of.get(first)
            genres_b = genres_of.get(second)
            if genres_a and genres_b and not (genres_a & genres_b):
                continue
            joint = neighbors[first] & neighbors[second]
            if len(joint) < MIN_SHELF_FILMS:
                continue
            pairs.append((1.0, (first, second, joint)))
            paired.update((first, second))

    # The anchors without a pair are the outlier tastes that the
    # intersection rule blocks. They enter the same draw alone, with a
    # damped weight. Thus, the pairs lead
    for first in anchors:
        if first in paired or len(neighbors[first]) < MIN_SHELF_FILMS:
            continue
        pairs.append((SINGLE_ANCHOR_DAMP, (first, None, neighbors[first])))

    shelves = []
    used_anchors = set()
    used_genres = set()
    singles = 0
    for first, second, joint in _weighted_order(rng, pairs):
        if len(shelves) >= count:
            break
        if first in used_anchors or second in used_anchors:
            continue
        if second is None and singles >= MAX_SINGLE_ANCHOR_SHELVES:
            continue

        # The copref shelves of 1 load never repeat a common-ground
        # genre (Glenn, 2026-08-30: 2 "Their common ground: Drama"
        # shelves on 1 page). If the shared genres of a candidate
        # overlap the genres of an earlier shelf, the candidate waits
        # for a different load

        if second is None:
            candidate_genres = genres_of.get(first, set())
        else:
            candidate_genres = genres_of.get(first, set()) & genres_of.get(
                second, set()
            )
        if candidate_genres & used_genres:
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
                "shared_genre_ids": sorted(candidate_genres),
            }
        )
        used_anchors.update(anchor_ids)
        used_genres.update(candidate_genres)
        shown.update(picks)
    return shelves


def build_shelves(user, count=SHELF_COUNT, rng=None):
    """Return the shelves of 1 page load for a user, newly drawn.

    Each shelf dict has its kind ("criteria" or "copref"), its criteria
    [(key, label), ...], its anchor movie ids, and its suggested movie
    ids. There are 2 anchors, or sometimes 1 (#249). The anchors come
    only from the films that the user rated or liked. The copref
    shelves draw first (COPREF_SHELF_COUNT caps them). The criteria
    shelves fill the rest. A seed is a single feature that at least 2
    anchor-eligible films have. One film is sufficient if the class is
    specific enough for a single-anchor shelf. A seed also needs a
    sufficient number of eligible candidates. The criteria of the
    winning seed extend greedily with the features that all anchors
    share, if a sufficient number of candidates have the whole set.
    Thus, the anchors and the suggestions always overlap on each
    criterion. A film never repeats across the shelves of 1 load.
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
    holders = {}  # feature key -> interest movie ids that have it
    carriers = {}  # feature key -> eligible movie ids that have it
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
        # A seed with 1 holder keys a single-anchor shelf (#249). This
        # is real evidence only for the specific classes. One loved film
        # says little about a genre or a decade. SEED_SHRINKAGE already
        # keeps these weak seeds in the draw
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
        # The single-anchor budget applies to both kinds
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

    # The build order is not the page order. The copref shelves draw
    # first only to claim their films. If they were pinned to the top,
    # each load would open in the same way (Glenn, 2026-08-26). Instead,
    # the kinds mix randomly down the page

    rng.shuffle(shelves)
    return shelves


def _criterion_label(key, label):
    """Return the final display label for 1 chosen criterion.

    A keyword replaces its cheap first-upper label with the casing from
    the overviews ("“New York City” films", "“FBI” films"). All other
    classes keep the index label."""

    if not key.startswith("keyword:"):
        return label
    row = db.session.get(TMDBKeyword, int(key.split(":", 1)[1]))
    if row is None:
        return label
    return f"“{keyword_display_name(row.name)}” films"


def parse_criteria(raw):
    """Return [(class, value)] from the comma-joined criteria keys.

    The keys come from the tile endpoint. Return None if a key is
    malformed. A copref key (`copref:{tmdbA}:{tmdbB}`, or
    `copref:{tmdbA}` for a single-anchor shelf) always stands alone. A
    copref shelf has exactly that 1 criterion. It parses to ("copref",
    (tmdbA, tmdbB)) or ("copref", (tmdbA,))."""

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
    """Return the movie ids that have 1 criterion, from a direct query.

    This is the cheap path of the tile endpoint. Thus, a replacement
    never builds the feature index of the whole page again."""

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
    """Return the next movie id for a slot of a copref shelf.

    This is the eligible film that is most similar to EVERY anchor. The
    anchors are both films of the pair, or the 1 film of a
    single-anchor shelf. The film must not be on the page already."""

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
    """Return the next movie id for a shelf slot.

    This is the eligible film with the best score that has all the
    criteria and is not on the page already. Return None if no
    candidate remains for the criteria set. That closes the slot. A
    copref shelf ranks by the joint similarity to its anchors, not by
    the engine score. This matches the order of the shelf."""

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
