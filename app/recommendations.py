"""Content-based film recommendations.

The engine builds a per-user taste profile from that user's own diary
rows — likes and chosen watches weigh above star ratings, and the
household shopping-cart priority is never consulted, because it mixes
in watchers who aren't Fitzflix users — then scores every film that
has a local full-feature file against the profile. Results land in
Redis on a nightly recompute; every recommendation carries its top
contributing features so the landing page can say why it was picked.

There is no collaborative filtering (two users) and no ML dependency:
a profile is a dictionary of per-feature affinities built from
centered ratings with Bayesian shrinkage toward zero, and scoring is
a weighted sum of soft per-class averages.
"""

import json
import random

from datetime import datetime

from flask import current_app
from werkzeug.local import LocalProxy

from app import db, get_app
from app.models import (
    File,
    Movie,
    MovieAward,
    MovieCast,
    MovieCopref,
    MovieCrew,
    TMDBCredit,
    TMDBGenre,
    TMDBKeyword,
    UserMovieReview,
    UserWatchlist,
    movie_genres,
    movie_keywords,
)

# This process's app instance, resolved lazily so importing this module
# from a process that already has an application doesn't build a second one

app = LocalProxy(get_app)


# How strongly each feature class steers scoring; `flask recs evaluate`
# reports how alternates fare before these are changed

FEATURE_CLASS_WEIGHTS = {
    "genre": 1.0,
    "director": 1.5,
    "actor": 1.0,
    "cinematographer": 1.0,
    "composer": 0.8,
    "writer": 1.0,
    "editor": 0.6,
    "keyword": 0.8,
    "decade": 0.6,
    "language": 0.4,
}

# Crew roles are separate feature classes — a lumped "crew" class would
# let role signals dilute each other. Labels double as the explanation
# text ("shot by Roger Deakins")

CREW_ROLE_JOBS = {
    "director": (("Director",), "directed by {name}"),
    "cinematographer": (
        ("Director of Photography", "Cinematography"),
        "shot by {name}",
    ),
    "composer": (("Original Music Composer", "Music"), "scored by {name}"),
    "writer": (("Screenplay", "Writer"), "written by {name}"),
    "editor": (("Editor",), "edited by {name}"),
}

# Bayesian shrinkage per class: affinity = sum(weights) / (count + k),
# so a feature seen once can't dominate one the user has rated often.
# Sparse classes (people) get lighter shrinkage than broad ones

FEATURE_CLASS_SHRINKAGE = {
    "genre": 5.0,
    "director": 2.0,
    "actor": 2.0,
    "cinematographer": 3.0,
    "composer": 3.0,
    "writer": 3.0,
    "editor": 3.0,
    "keyword": 3.0,
    "decade": 5.0,
    "language": 5.0,
}

# Cast rows that count as "starring" for taste purposes

TOP_BILLING_CUTOFF = 8

# Diary-row sentiment: a like outranks any centered star rating, a bare
# watch is a mild positive (the user chose it), rewatches add a little
# more, and ratings center on the user's own mean so an average score
# contributes nothing

LIKE_WEIGHT = 1.0
BARE_WATCH_WEIGHT = 0.3
REWATCH_WEIGHT = 0.25
REWATCH_CAP = 2
RATING_SPREAD = 2.5

# A watchlist add is interest, not approval: weaker than choosing to
# watch, but a real signal about taste

WATCHLIST_WEIGHT = 0.2

# Redis keys written by the nightly recompute

RECS_KEY = "fitzflix:recs:{user_id}"
PROFILE_KEY = "fitzflix:recs:profile:{user_id}"

# Deep enough that the landing page's no-repeat partition (12 films a
# day, one per quality tier) cycles the whole set roughly monthly —
# the library pool measured 2,800+ positive-scoring films, so depth
# costs nothing but Redis bytes

STORED_RECOMMENDATIONS = 400

# "Might interest you" markers: how many films a filmography page marks
# at most, the absolute floor a film must clear, and the percentile of
# the user's own candidate library that sets the real bar — a saturated
# profile scores almost every film highly on raw affinity, so the badge
# means "notably above your typical film", not "matches a liked genre"

MARKER_LIMIT = 5
MARKER_THRESHOLD = 0.05
MARKER_BASELINE_PERCENTILE = 0.9


def marker_bar(profile):
    """The coarse score a film must beat to earn a marker: the stored
    baseline percentile of the user's own candidate library, floored at
    the absolute threshold (new or sparse profiles fall back there)."""

    if not profile:
        return MARKER_THRESHOLD
    return max(MARKER_THRESHOLD, profile.get("marker_bar") or 0.0)


def collect_features(movie_ids):
    """(class, key, label) feature tuples per movie id, bulk-queried so
    profile builds and scoring runs never walk per-movie relationships.

    Keys are stable and portable: genre/actor/director keys embed TMDb
    ids, so the filmography markers can score cached TMDb payloads
    against the same profile.
    """

    features = {movie_id: [] for movie_id in movie_ids}
    if not movie_ids:
        return features

    for movie_id, year, language in db.session.query(
        Movie.id, Movie.year, Movie.tmdb_original_language
    ).filter(Movie.id.in_(movie_ids)):
        decade = year // 10 * 10
        features[movie_id].append(("decade", f"decade:{decade}", f"{decade}s"))
        if language:
            features[movie_id].append(
                ("language", f"language:{language}", f"{language.upper()}-language")
            )

    for movie_id, genre_id, name in (
        db.session.query(movie_genres.c.movie_id, TMDBGenre.id, TMDBGenre.name)
        .join(TMDBGenre, TMDBGenre.id == movie_genres.c.genre_id)
        .filter(movie_genres.c.movie_id.in_(movie_ids))
    ):
        features[movie_id].append(("genre", f"genre:{genre_id}", name))

    for movie_id, keyword_id, name in (
        db.session.query(movie_keywords.c.movie_id, TMDBKeyword.id, TMDBKeyword.name)
        .join(TMDBKeyword, TMDBKeyword.id == movie_keywords.c.keyword_id)
        .filter(movie_keywords.c.movie_id.in_(movie_ids))
    ):
        features[movie_id].append(("keyword", f"keyword:{keyword_id}", name))

    job_to_class = {
        job: cls for cls, (jobs, _) in CREW_ROLE_JOBS.items() for job in jobs
    }
    for movie_id, credit_id, name, job in (
        db.session.query(
            MovieCrew.movie_id, MovieCrew.credit_id, TMDBCredit.name, MovieCrew.job
        )
        .join(TMDBCredit, TMDBCredit.id == MovieCrew.credit_id)
        .filter(
            MovieCrew.movie_id.in_(movie_ids), MovieCrew.job.in_(list(job_to_class))
        )
    ):
        cls = job_to_class[job]
        label = CREW_ROLE_JOBS[cls][1].format(name=name)
        features[movie_id].append((cls, f"{cls}:{credit_id}", label))

    for movie_id, credit_id, name in (
        db.session.query(MovieCast.movie_id, MovieCast.credit_id, TMDBCredit.name)
        .join(TMDBCredit, TMDBCredit.id == MovieCast.credit_id)
        .filter(
            MovieCast.movie_id.in_(movie_ids),
            MovieCast.billing_order < TOP_BILLING_CUTOFF,
        )
    ):
        features[movie_id].append(("actor", f"actor:{credit_id}", name))

    # An actor can appear twice on one film under different characters;
    # a feature counts once per film

    for movie_id, rows in features.items():
        seen = set()
        unique = []
        for row in rows:
            if row[1] not in seen:
                seen.add(row[1])
                unique.append(row)
        features[movie_id] = unique

    return features


def user_movie_weights(user_id):
    """Per-movie sentiment weights from the user's own diary rows —
    never the household shopping-cart priority — plus a mild interest
    weight for unwatched films on their watchlist."""

    rows = (
        db.session.query(
            UserMovieReview.movie_id,
            db.func.count(UserMovieReview.id),
            db.func.max(UserMovieReview.rating),
            db.func.max(db.case((UserMovieReview.liked == True, 1), else_=0)),
        )
        .filter(UserMovieReview.user_id == int(user_id))
        .filter(UserMovieReview.movie_id.isnot(None))
        .group_by(UserMovieReview.movie_id)
        .all()
    )

    ratings = [rating for _, _, rating, _ in rows if rating is not None]
    mean_rating = sum(ratings) / len(ratings) if ratings else 0.0

    weights = {}
    for movie_id, viewings, rating, liked in rows:
        weight = 0.0
        if rating is not None:
            centered = (rating - mean_rating) / RATING_SPREAD
            weight += max(-1.0, min(1.0, centered))
        else:
            weight += BARE_WATCH_WEIGHT
        if liked:
            weight += LIKE_WEIGHT
        if viewings > 1:
            weight += REWATCH_WEIGHT * min(viewings - 1, REWATCH_CAP)
        weights[movie_id] = weight

    for (movie_id,) in db.session.query(UserWatchlist.movie_id).filter(
        UserWatchlist.user_id == int(user_id)
    ):
        if movie_id not in weights:
            weights[movie_id] = WATCHLIST_WEIGHT

    return weights


def build_profile(weights, features_by_movie):
    """The taste profile: per-feature affinities shrunk toward zero, from
    {movie_id: weight} and that user's movies' features."""

    sums, counts, labels, classes = {}, {}, {}, {}
    for movie_id, weight in weights.items():
        for cls, key, label in features_by_movie.get(movie_id, []):
            sums[key] = sums.get(key, 0.0) + weight
            counts[key] = counts.get(key, 0) + 1
            labels[key] = label
            classes[key] = cls

    affinities = {
        key: {
            "class": classes[key],
            "label": labels[key],
            "count": counts[key],
            "score": sums[key] / (counts[key] + FEATURE_CLASS_SHRINKAGE[classes[key]]),
        }
        for key in sums
    }
    return {"affinities": affinities, "movies": len(weights)}


def score_movie(features, profile, class_weights=None):
    """(score, contributions) for one film against a profile.

    Matched features are soft-averaged within their class so a film
    with thirty keywords can't outrank one with three good ones, then
    classes combine by weight. Contributions come back sorted for the
    landing page's "because" display.
    """

    class_weights = class_weights or FEATURE_CLASS_WEIGHTS
    affinities = profile["affinities"]

    matched = {}
    for cls, key, label in features:
        entry = affinities.get(key)
        if entry is not None:
            matched.setdefault(cls, []).append((entry["score"], label))

    score = 0.0
    contributions = []
    for cls, entries in matched.items():
        class_weight = class_weights.get(cls, 0.0)
        denominator = len(entries) + 1
        for affinity, label in entries:
            contribution = class_weight * affinity / denominator
            score += contribution
            contributions.append((contribution, label))

    contributions.sort(key=lambda pair: pair[0], reverse=True)
    return score, contributions


# The award prior: a capped quality bump from Wikidata wins and
# nominations, added AFTER a film already scores positive on taste —
# awards alone can't recommend a taste-mismatched film, and the cap
# keeps a festival darling's hundred citations from drowning the
# taste signal (top library scores run ~2-3)

AWARD_WIN_WEIGHT = 0.1
AWARD_NOMINATION_WEIGHT = 0.025
AWARD_PRIOR_CAP = 0.3


def movie_award_counts():
    """(wins, nominations) tallies per movie id, for the quality prior."""

    counts = {}
    for movie_id, win, tally in db.session.query(
        MovieAward.movie_id, MovieAward.win, db.func.count()
    ).group_by(MovieAward.movie_id, MovieAward.win):
        wins, nominations = counts.get(movie_id, (0, 0))
        if win:
            wins += tally
        else:
            nominations += tally
        counts[movie_id] = (wins, nominations)
    return counts


def award_prior(wins, nominations):
    """The capped score bump a film's award record earns."""

    return min(
        wins * AWARD_WIN_WEIGHT + nominations * AWARD_NOMINATION_WEIGHT,
        AWARD_PRIOR_CAP,
    )


def award_label(wins, nominations):
    """The because-chip text for an awarded film."""

    if wins:
        return f"won {wins} award{'s' if wins != 1 else ''}"
    return "award-nominated"


# The co-preference term: what content features can't see, thirty-two
# million MovieLens ratings can. A candidate's value is the weighted
# average of the user's own sentiment over its K most-similar diary
# films ("people who loved what you loved also loved this"), so the
# term ranges with the diary weights themselves. The weight was chosen
# by leave-one-out bake-off (Aug 2026): 2.0 sits on a flat optimum that
# cut mean percentile from 0.324 to 0.266 and more than doubled hit@10;
# the laurel person-prior alternative measured flat and was rejected

COPREF_WEIGHT = 2.0
COPREF_NEIGHBORS = 20


def copref_anchor_sims(anchor_tmdb_ids):
    """{anchor tmdb: {other tmdb: similarity}} for the given anchors,
    from the movie_copref table (empty when the table's never built)."""

    anchors = [int(t) for t in anchor_tmdb_ids if t]
    if not anchors:
        return {}
    sims = {}
    for tmdb_a, tmdb_b, similarity in db.session.query(
        MovieCopref.tmdb_id_a, MovieCopref.tmdb_id_b, MovieCopref.similarity
    ).filter(MovieCopref.tmdb_id_a.in_(anchors)):
        sims.setdefault(tmdb_a, {})[tmdb_b] = similarity
    return sims


def copref_entries(weights_by_tmdb, sims_by_anchor):
    """Per-film neighbor lists for co-preference scoring.

    {film tmdb: [(similarity, anchor tmdb, anchor weight), ...]} sorted
    most-similar first — the input to _copref_value, built once per
    compute or evaluation and cheap to re-rank per fold.
    """

    buckets = {}
    for anchor, sims in sims_by_anchor.items():
        weight = weights_by_tmdb.get(anchor)
        if weight is None:
            continue
        for other, similarity in sims.items():
            buckets.setdefault(other, []).append((similarity, anchor, weight))
    for entries in buckets.values():
        entries.sort(key=lambda entry: -entry[0])
    return buckets


def _copref_value(entries, excluded=None):
    """One film's co-preference term from its sorted neighbor list,
    optionally excluding an anchor (leave-one-out purity)."""

    numerator = 0.0
    denominator = 0.0
    taken = 0
    for similarity, anchor, weight in entries:
        if anchor == excluded:
            continue
        numerator += similarity * weight
        denominator += similarity
        taken += 1
        if taken == COPREF_NEIGHBORS:
            break
    if denominator <= 0:
        return 0.0
    return COPREF_WEIGHT * numerator / denominator


# A chip only when the term meaningfully moved the score

COPREF_CHIP_THRESHOLD = 0.5


def _copref_top_anchor(entries):
    """The anchor doing the most lifting inside the top-K window — the
    film named by the "liked by people who liked …" chip — or None."""

    best = None
    best_value = 0.0
    taken = 0
    for similarity, anchor, weight in entries:
        value = similarity * weight
        if value > best_value:
            best_value = value
            best = anchor
        taken += 1
        if taken == COPREF_NEIGHBORS:
            break
    return best


def local_candidates(user_id):
    """Movie ids with a local full-feature file, minus films the user has
    already logged: the landing page only recommends what's on the shelf
    and unseen."""

    seen = db.session.query(UserMovieReview.movie_id).filter(
        UserMovieReview.user_id == int(user_id),
        UserMovieReview.movie_id.isnot(None),
    )
    rows = (
        db.session.query(File.movie_id)
        .filter(File.movie_id.isnot(None), File.feature_type_id.is_(None))
        .filter(~File.movie_id.in_(seen))
        .distinct()
        .all()
    )
    return [movie_id for (movie_id,) in rows]


def compute_user_recommendations(user_id, limit=STORED_RECOMMENDATIONS):
    """(profile, ranked recommendations) for one user, or (None, []) for
    a user with no diary rows."""

    weights = user_movie_weights(user_id)
    if not weights:
        return None, []

    candidates = local_candidates(user_id)
    features = collect_features(list(set(candidates) | set(weights)))
    profile = build_profile(weights, features)

    # The stored cut must survive the render-time exclusions: the
    # landing page pulls watchlisted films out of the discovery pool
    # into the pin lane, and the watchlist is deliberately uncapped
    # (Glenn once queued 500 films on Netflix) — so the cut deepens by
    # the watchlisted candidates, keeping at least `limit` films for
    # discovery and the rail's no-repeat cycle at a month or longer

    watchlisted = {
        movie_id
        for (movie_id,) in db.session.query(UserWatchlist.movie_id).filter(
            UserWatchlist.user_id == int(user_id)
        )
    }
    depth = limit + len(watchlisted & set(candidates))

    # The marker bar rides along with the profile: the baseline
    # percentile of coarse scores across this user's own candidates. A
    # saturated profile rates almost everything highly, so "might
    # interest you" only means anything relative to that baseline

    baseline = []
    for movie_id in candidates:
        genre_ids = []
        year = None
        for cls, key, _ in features.get(movie_id, []):
            if cls == "genre":
                genre_ids.append(int(key.split(":", 1)[1]))
            elif cls == "decade":
                year = int(key.split(":", 1)[1])
        baseline.append(coarse_interest_score(profile, genre_ids, year))
    if baseline:
        baseline.sort()
        index = min(len(baseline) - 1, int(len(baseline) * MARKER_BASELINE_PERCENTILE))
        profile["marker_bar"] = round(baseline[index], 4)

    # Co-preference: anchors are the user's own weighted films, matched
    # into the similarity table by TMDb id; candidates collect their
    # top-neighbor term the same way the evaluation measures it

    tmdb_of = dict(
        db.session.query(Movie.id, Movie.tmdb_id)
        .filter(Movie.id.in_(list(set(candidates) | set(weights)) or [0]))
        .filter(Movie.tmdb_id.isnot(None))
    )
    weights_by_tmdb = {
        tmdb_of[movie_id]: weight
        for movie_id, weight in weights.items()
        if movie_id in tmdb_of
    }
    entries_by_tmdb = copref_entries(
        weights_by_tmdb, copref_anchor_sims(weights_by_tmdb)
    )
    anchor_titles = {}
    if entries_by_tmdb:
        anchor_titles = {
            tmdb_id: title or plain_title
            for tmdb_id, title, plain_title in db.session.query(
                Movie.tmdb_id, Movie.tmdb_title, Movie.title
            ).filter(Movie.tmdb_id.in_(list(weights_by_tmdb)))
        }

    award_counts = movie_award_counts()
    ranked = []
    for movie_id in candidates:
        movie_features = features.get(movie_id, [])
        if not movie_features:
            continue
        score, contributions = score_movie(movie_features, profile)
        entries = entries_by_tmdb.get(tmdb_of.get(movie_id), [])
        copref = _copref_value(entries)
        total = score + copref
        if total <= 0:
            continue
        because = [
            label for contribution, label in contributions[:4] if contribution > 0
        ]
        if copref >= COPREF_CHIP_THRESHOLD:
            title = anchor_titles.get(_copref_top_anchor(entries))
            if title:
                because.append(f"liked by people who liked {title}")
        # The award prior keeps its original taste gate: awards only
        # decorate films the profile itself already scores positive
        wins, nominations = award_counts.get(movie_id, (0, 0))
        prior = award_prior(wins, nominations)
        if score > 0 and prior > 0:
            total += prior
            because.append(award_label(wins, nominations))
        ranked.append(
            {"movie_id": movie_id, "score": round(total, 4), "because": because}
        )

    ranked.sort(key=lambda rec: rec["score"], reverse=True)
    return profile, ranked[:depth]


# The "Watch it again" shelf: owned films the user liked whose last
# watch is long past — the complement of the engine, whose candidates
# deliberately exclude logged films. Sentiment reuses the diary
# weights; staleness adds a bonus on top, saturating at the horizon.
# Date-less rows (drive ratings, pre-Fitzflix watches) count as the
# oldest. The 2-year bar was measured before choosing: 475 of Glenn's
# 556 liked owned films sit beyond it — plenty for a 12-card rotation

REWATCH_STALENESS_YEARS = 2
REWATCH_STALENESS_WEIGHT = 0.5
REWATCH_STALENESS_HORIZON_YEARS = 10


def watch_again_shelf(user_id, today=None):
    """Ranked rewatch candidates for one user: owned films they liked
    (the liked flag, or a rating above their own mean) whose last
    watch — if any is recorded — is at least the staleness bar ago.
    Best-loved-and-longest-unseen first."""

    today = today or datetime.now()
    rows = (
        db.session.query(
            UserMovieReview.movie_id,
            db.func.max(UserMovieReview.date_watched),
            db.func.max(UserMovieReview.rating),
            db.func.max(db.case((UserMovieReview.liked == True, 1), else_=0)),
        )
        .join(Movie, Movie.id == UserMovieReview.movie_id)
        .filter(UserMovieReview.user_id == int(user_id))
        .filter(Movie.files.any(File.feature_type_id.is_(None)))
        .group_by(UserMovieReview.movie_id)
        .all()
    )
    ratings = [float(rating) for _, _, rating, _ in rows if rating is not None]
    mean_rating = sum(ratings) / len(ratings) if ratings else 0.0
    weights = user_movie_weights(user_id)

    items = []
    for movie_id, last_watched, rating, liked in rows:
        positive = bool(liked) or (rating is not None and float(rating) > mean_rating)
        if not positive:
            continue
        if last_watched is None:
            years = float(REWATCH_STALENESS_HORIZON_YEARS)
        else:
            years = (today - last_watched).days / 365.25
        if years < REWATCH_STALENESS_YEARS:
            continue
        staleness = (
            min(years, REWATCH_STALENESS_HORIZON_YEARS)
            / REWATCH_STALENESS_HORIZON_YEARS
        )
        items.append(
            {
                "movie_id": movie_id,
                "last_watched": last_watched,
                "score": round(
                    weights.get(movie_id, 0.0) + REWATCH_STALENESS_WEIGHT * staleness,
                    4,
                ),
            }
        )
    items.sort(key=lambda item: item["score"], reverse=True)
    return items


def rotate_partition(items, count, day_index):
    """A no-repeat daily walk through a ranked list.

    The ranking splits into `count` contiguous quality tiers and each
    day shows one film from each tier, indexed by a continuous day
    counter — so every film appears exactly once per cycle (cycle
    length = tier size, about len/count days), every day mixes all
    quality tiers, and the whole set refreshes before anything
    repeats. Deterministic per day; short lists pass through whole.
    """

    if len(items) <= count:
        return list(items)
    picks = []
    for tier in range(count):
        # Balanced boundaries: tier sizes differ by at most one and no
        # tier is ever empty, so short lists still fill every slot
        tier_items = items[
            tier * len(items) // count : (tier + 1) * len(items) // count
        ]
        picks.append(tier_items[day_index % len(tier_items)])
    return picks


def rotate_daily(items, count, seed, decay=0.93):
    """A day-varying selection of `count` items from a ranked list.

    Weighted sampling without replacement, geometrically favoring the
    top of the ranking so quality holds while the middle rotates; the
    seed should embed the user and the calendar day, keeping the page
    stable across reloads but fresh across days. The selection comes
    back in original rank order. Deterministic for a given seed.
    """

    if len(items) <= count:
        return list(items)
    rng = random.Random(seed)
    pool = list(enumerate(items))
    selected = []
    while pool and len(selected) < count:
        weights = [decay**rank for rank, _ in pool]
        pick = rng.choices(range(len(pool)), weights=weights, k=1)[0]
        selected.append(pool.pop(pick))
    selected.sort(key=lambda pair: pair[0])
    return [item for _, item in selected]


def shuffle_daily(items, seed):
    """A deterministic day-seeded shuffle of a shelf's picked cards.

    The pickers hand back quality-ordered rows — watchlist pins first,
    then the ranking's tiers best-first — which made slot position a
    quality signal: the first cards were always the amber block and the
    top tier (Glenn: mix them). Shuffling with a user+day seed varies
    the arrangement daily while reloads stay stable; the amber badge
    marks the watchlist cards wherever they land. The leaving shelf
    deliberately opts out — its watchlist-first order is urgency, not
    discovery.
    """

    rng = random.Random(seed)
    shuffled = list(items)
    rng.shuffle(shuffled)
    return shuffled


def stored_recommendations(redis, user_id):
    """The nightly recompute's stored payload for a user, or None."""

    payload = redis.get(RECS_KEY.format(user_id=int(user_id)))
    return json.loads(payload) if payload else None


def stored_profile(redis, user_id):
    """The nightly recompute's stored taste profile for a user, or None."""

    payload = redis.get(PROFILE_KEY.format(user_id=int(user_id)))
    return json.loads(payload) if payload else None


def recommended_movie_ids(redis, user_id):
    """The movie ids in the user's stored library recommendations.

    Membership means the nightly recompute ranked the film among the
    user's top candidates — the same set that feeds the landing page's
    library rail — so other surfaces can badge owned films as "might
    interest you" without rescoring anything.
    """

    stored = stored_recommendations(redis, user_id)
    return {item["movie_id"] for item in (stored or {}).get("items", [])}


def coarse_interest_score(profile, genre_ids, year, person_affinity=0.0):
    """The might-interest markers' coarse score, computable from any
    payload that carries genre ids and a year: matched genre affinities
    soft-averaged, the release decade, and an optional affinity for a
    person the film features. No TMDb calls, nothing stored."""

    affinities = profile.get("affinities", {}) if profile else {}
    score = person_affinity * FEATURE_CLASS_WEIGHTS["actor"]

    genre_scores = [
        affinities[f"genre:{genre_id}"]["score"]
        for genre_id in genre_ids or []
        if f"genre:{genre_id}" in affinities
    ]
    if genre_scores:
        score += (
            FEATURE_CLASS_WEIGHTS["genre"] * sum(genre_scores) / (len(genre_scores) + 1)
        )

    year = str(year or "")[:4]
    if year.isdigit():
        decade = int(year) // 10 * 10
        entry = affinities.get(f"decade:{decade}")
        if entry:
            score += FEATURE_CLASS_WEIGHTS["decade"] * entry["score"] / 2

    return score


def credit_interest_markers(profile, credit_id, filmography_rows):
    """The tmdb ids on a filmography page worth a "might interest you"
    marker.

    Scores only films without a local record (owned films get the full
    engine), using nothing beyond the cached credits payload: genre
    ids, release decade, and the user's affinity for this person —
    capped at the strongest few per career page.
    """

    if not profile:
        return set()
    affinities = profile.get("affinities", {})
    person = max(
        (affinities.get(f"{cls}:{int(credit_id)}") or {}).get("score", 0.0)
        for cls in ("actor", *CREW_ROLE_JOBS)
    )

    bar = marker_bar(profile)
    scored = []
    for row in filmography_rows:
        if row.get("movie") is not None or row.get("tmdb_id") is None:
            continue
        score = coarse_interest_score(
            profile, row.get("genre_ids"), row.get("year"), person_affinity=person
        )
        if score > bar:
            scored.append((score, row["tmdb_id"]))

    scored.sort(reverse=True)
    return {tmdb_id for _, tmdb_id in scored[:MARKER_LIMIT]}


def recompute_recommendations():
    """Nightly task: rebuild every reviewer's taste profile and ranked
    recommendations into Redis for the landing page and the filmography
    markers."""

    with app.app_context():
        computed_at = datetime.now().strftime("%Y-%m-%d %H:%M")
        user_ids = [
            user_id
            for (user_id,) in db.session.query(UserMovieReview.user_id)
            .filter(UserMovieReview.user_id.isnot(None))
            .distinct()
        ]
        for user_id in user_ids:
            profile, ranked = compute_user_recommendations(user_id)
            if profile is None:
                continue
            current_app.redis.set(
                PROFILE_KEY.format(user_id=user_id), json.dumps(profile)
            )
            current_app.redis.set(
                RECS_KEY.format(user_id=user_id),
                json.dumps({"computed_at": computed_at, "items": ranked}),
            )
            current_app.logger.info(
                f"Recommendations: stored {len(ranked)} films for user {user_id}"
            )
        return True


def evaluate_user(user_id, class_weights=None, positive_threshold=0.5):
    """Leave-one-out ranking metrics for one user under the given (or
    current) class weights.

    Each film the user clearly liked is removed from the profile in
    turn and ranked against every local candidate plus itself; a good
    weighting ranks the held-out film near the top. Returns None for a
    user without enough positive films to measure.
    """

    weights = user_movie_weights(user_id)
    positives = [
        movie_id for movie_id, weight in weights.items() if weight >= positive_threshold
    ]
    if len(positives) < 2:
        return None

    candidates = local_candidates(user_id)
    features = collect_features(list(set(candidates) | set(weights)))

    # The shipped co-preference term rides in the evaluation too,
    # leave-one-out pure: each fold's held-out film is dropped from
    # every neighbor list it anchors

    tmdb_of = dict(
        db.session.query(Movie.id, Movie.tmdb_id)
        .filter(Movie.id.in_(list(set(candidates) | set(weights)) or [0]))
        .filter(Movie.tmdb_id.isnot(None))
    )
    weights_by_tmdb = {
        tmdb_of[movie_id]: weight
        for movie_id, weight in weights.items()
        if movie_id in tmdb_of
    }
    entries_by_tmdb = copref_entries(
        weights_by_tmdb, copref_anchor_sims(weights_by_tmdb)
    )

    percentiles = []
    hits_at_10 = 0
    hits_at_25 = 0
    for held_out in positives:
        if not features.get(held_out):
            continue
        remaining = {
            movie_id: weight
            for movie_id, weight in weights.items()
            if movie_id != held_out
        }
        profile = build_profile(remaining, features)
        held_tmdb = tmdb_of.get(held_out)

        held_score, _ = score_movie(features[held_out], profile, class_weights)
        held_score += _copref_value(
            entries_by_tmdb.get(held_tmdb, []), excluded=held_tmdb
        )
        rank = 1
        for movie_id in candidates:
            score, _ = score_movie(features.get(movie_id, []), profile, class_weights)
            score += _copref_value(
                entries_by_tmdb.get(tmdb_of.get(movie_id), []), excluded=held_tmdb
            )
            if score > held_score:
                rank += 1
        total = len(candidates) + 1

        percentiles.append((rank - 1) / max(total - 1, 1))
        if rank <= 10:
            hits_at_10 += 1
        if rank <= 25:
            hits_at_25 += 1

    if not percentiles:
        return None
    measured = len(percentiles)
    return {
        "positives": measured,
        "mean_percentile": sum(percentiles) / measured,
        "hit_at_10": hits_at_10 / measured,
        "hit_at_25": hits_at_25 / measured,
    }
