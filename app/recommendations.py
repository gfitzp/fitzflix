"""Make content-based film recommendations.

The engine builds a taste profile for each user from the own diary rows
of that user. Likes and chosen watches weigh more than star ratings.
The engine never reads the household shopping-cart priority. That
priority includes watchers that are not Fitzflix users. Then the
engine scores each film that has a local full-feature file against the
profile. A nightly recompute writes the results to Redis. Each
recommendation carries its top contributing features. Thus, the
landing page can say why the engine picked it.

There is no collaborative filtering (there are 2 users) and no ML
dependency. A profile is a dictionary of per-feature affinities. The
engine builds it from centered ratings with Bayesian shrinkage toward
zero. The score is a weighted sum of soft per-class averages.
"""

import bisect
import json
import random

from datetime import date, datetime

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
    UserMovieStatus,
    UserWatchlist,
    movie_genres,
    movie_keywords,
)

# The app instance of this process. Fitzflix resolves it lazily. Thus,
# a process that already has an application does not build a second
# one when it imports this module.

app = LocalProxy(get_app)


# The strength of each feature class in the score. Run `flask recs
# evaluate` to measure alternatives before you change these.

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

# Each crew role is a separate feature class. In a combined "crew"
# class, the role signals would dilute each other. The labels are also
# the explanation text ("shot by Roger Deakins").

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

# Bayesian shrinkage for each class: affinity = sum(weights) / (count +
# k). Thus, a feature seen 1 time cannot dominate a feature that the
# user rated many times. Sparse classes (people) get lighter shrinkage
# than broad classes.

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

# The cast rows that count as "starring" for the taste profile.

TOP_BILLING_CUTOFF = 8

# The sentiment of a diary row. A like outranks each centered star
# rating. A bare watch is a mild positive (the user chose it). A rewatch
# adds a small amount more. Ratings center on the own mean of the user.
# Thus, an average score contributes nothing.

LIKE_WEIGHT = 1.0
BARE_WATCH_WEIGHT = 0.3
REWATCH_WEIGHT = 0.25
REWATCH_CAP = 2
RATING_SPREAD = 2.5

# A watchlist add is interest, not approval. It is weaker than a choice
# to watch, but it is a real signal about taste.

WATCHLIST_WEIGHT = 0.2

# "Not interested" is the mirror of the watchlist. It is a mild
# negative for a film that the user refused without a watch. It never
# adds to a real diary verdict. The verdict already carries the
# sentiment (#45b).

NOT_INTERESTED_WEIGHT = -0.3

# The Redis keys that the nightly recompute writes.

RECS_KEY = "fitzflix:recs:{user_id}"
PROFILE_KEY = "fitzflix:recs:profile:{user_id}"

# The complete score map: the full-recipe engine score of each
# scoreable unlogged film. Thus, each surface can show an estimated
# rating with 1 Redis read. The ranking above keeps only the positive
# cut.

SCORES_KEY = "fitzflix:recs:scores:{user_id}"

# Films scored live between recomputes (records created after the last
# nightly run) patch into the map through this overlay hash. Thus, each
# surface reads 1 number for each film. The source of the number is not
# important. The nightly rebuild covers those films fully and deletes
# the overlay. The TTL is garbage collection for a recompute that stops.
# Fitzflix recomputes a lost patch on demand.

PATCH_SCORES_KEY = "fitzflix:recs:scores:patch:{user_id}"
PATCH_SCORES_TTL = 60 * 60 * 48

# The TMDB-keyed lane of the shared source: scores for films with no
# local record. Fitzflix computes them from their cached enriched
# payloads and holds them in their own overlay. The award prior is the
# exception. Award rows are local. Nothing goes into the database for
# these films. The nightly recompute deletes the overlay. Thus, the
# estimates derive again from the new profile. The TTL is garbage
# collection.

TMDB_PATCH_SCORES_KEY = "fitzflix:recs:scores:tmdb:{user_id}"

# The no-repeat partition of the landing page shows 12 films a day, 1
# for each quality tier. This depth lets that partition cycle the full
# set about each month. The library pool measured more than 2,800
# positive-scoring films. Thus, the depth costs only Redis bytes.

STORED_RECOMMENDATIONS = 400

# The "Might interest you" markers: the maximum number of films that a
# filmography page marks, and the absolute minimum that a film must
# pass. The last is the percentile of the own candidate library of the
# user. That percentile sets the real bar. A saturated profile scores
# almost each film highly on
# raw affinity. Thus, the badge means "much above your typical film",
# not "matches a liked genre".

MARKER_LIMIT = 5
MARKER_THRESHOLD = 0.05
MARKER_BASELINE_PERCENTILE = 0.9


def marker_bar(profile):
    """Return the coarse score that a film must pass to get a marker.

    This is the stored baseline percentile of the own candidate library
    of the user. The minimum is the absolute threshold. A new or sparse
    profile uses that threshold."""

    if not profile:
        return MARKER_THRESHOLD
    return max(MARKER_THRESHOLD, profile.get("marker_bar") or 0.0)


def collect_features(movie_ids):
    """Return the (class, key, label) feature tuples for each movie id.

    This queries in bulk. Thus, profile builds and score runs never walk
    the relationships of each movie.

    The keys are stable and portable. Genre, actor, and director keys
    embed TMDB ids. Thus, the filmography markers can score cached TMDB
    payloads against the same profile.
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

    # An actor can appear 2 times on 1 film as different characters. A
    # feature counts 1 time for each film.

    for movie_id, rows in features.items():
        seen = set()
        unique = []
        for row in rows:
            if row[1] not in seen:
                seen.add(row[1])
                unique.append(row)
        features[movie_id] = unique

    return features


def latest_ratings(user_id):
    """Return the current star rating of each film.

    This is the rating of the most recent diary row. Reviews come
    before bare watches, newest first. The id breaks ties. This is the
    same row that the star widget of the movie page shows. Thus, a new
    rating replaces the old verdict. It does not compete with it (the
    rule of Glenn, 2026-08). A film whose latest row is unrated maps to
    None."""

    rows = (
        db.session.query(
            UserMovieReview.movie_id,
            UserMovieReview.rating,
            UserMovieReview.date_reviewed,
            UserMovieReview.id,
        )
        .filter(UserMovieReview.user_id == int(user_id))
        .filter(UserMovieReview.movie_id.isnot(None))
        .all()
    )
    latest = {}
    order = {}
    for movie_id, rating, date_reviewed, row_id in rows:
        key = (date_reviewed is not None, date_reviewed or datetime.min, row_id)
        if movie_id not in order or key > order[movie_id]:
            order[movie_id] = key
            latest[movie_id] = float(rating) if rating is not None else None
    return latest


def user_movie_weights(user_id):
    """Return the sentiment weight of each movie.

    The weights come from the own diary rows of the user. They never
    come from the household shopping-cart priority. Unwatched films on
    the watchlist of the user get a mild interest weight."""

    rows = (
        db.session.query(
            UserMovieReview.movie_id,
            db.func.count(UserMovieReview.id),
            db.func.max(db.case((UserMovieReview.liked == True, 1), else_=0)),
        )
        .filter(UserMovieReview.user_id == int(user_id))
        .filter(UserMovieReview.movie_id.isnot(None))
        .group_by(UserMovieReview.movie_id)
        .all()
    )

    current = latest_ratings(user_id)
    ratings = [rating for rating in current.values() if rating is not None]
    mean_rating = sum(ratings) / len(ratings) if ratings else 0.0

    weights = {}
    for movie_id, viewings, liked in rows:
        weight = 0.0
        rating = current.get(movie_id)
        if rating is None and liked:
            # A liked-only viewing counts as a 3-star verdict for the
            # profile (the rule of Glenn, 2026-08). Letterboxd permits a
            # heart with no stars. The interface shows the viewing as
            # unrated. Thus, the user can supply real stars later.
            rating = 3.0
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

    for (movie_id,) in db.session.query(UserMovieStatus.movie_id).filter(
        UserMovieStatus.user_id == int(user_id),
        UserMovieStatus.kind == "not_interested",
    ):
        if movie_id not in weights:
            weights[movie_id] = NOT_INTERESTED_WEIGHT

    return weights


def build_profile(weights, features_by_movie):
    """Return the taste profile.

    The profile holds the affinity of each feature, shrunk toward zero.
    The inputs are {movie_id: weight} and the features of the movies of
    that user."""

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
    """Return (score, contributions) for 1 film against a profile.

    The matched features get a soft average within their class. Thus, a
    film with 30 keywords cannot outrank a film with 3 good keywords.
    Then the classes combine by weight. The contributions come back
    sorted for the "because" display of the landing page.
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


# The award prior: a limited quality increase from Wikidata wins and
# nominations. Fitzflix adds it only AFTER a film scores positive on
# taste. Awards alone cannot recommend a film that does not match the
# taste. The limit prevents the 100 citations of a festival favorite
# from drowning the taste signal. The top library scores are about 2
# to 3.

AWARD_WIN_WEIGHT = 0.1
AWARD_NOMINATION_WEIGHT = 0.025
AWARD_PRIOR_CAP = 0.3


def movie_award_counts():
    """Return the (wins, nominations) counts for each movie id, for the
    quality prior."""

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
    """Return the limited score increase that the award record of a film
    gets."""

    return min(
        wins * AWARD_WIN_WEIGHT + nominations * AWARD_NOMINATION_WEIGHT,
        AWARD_PRIOR_CAP,
    )


def award_label(wins, nominations):
    """Return the because-chip text for an awarded film."""

    if wins:
        return f"won {wins} award{'s' if wins != 1 else ''}"
    return "award-nominated"


# The co-preference term. Content features cannot see some signals.
# The 32 million MovieLens ratings can. The value of a candidate is the
# weighted average of the own sentiment of the user over its K most
# similar diary films ("people who loved what you loved also loved
# this"). Thus, the term has the same range as the diary weights. A
# leave-one-out comparison chose the weight (2026-08). The value 2.0
# sits on a flat optimum. It cut the mean percentile from 0.324 to
# 0.266. It more than doubled hit@10. The laurel person-prior
# alternative measured flat. It was rejected.

COPREF_WEIGHT = 2.0
COPREF_NEIGHBORS = 20


def copref_anchor_sims(anchor_tmdb_ids):
    """Return {anchor tmdb: {other tmdb: similarity}} for the given
    anchors from the movie_copref table. It is empty if the table was
    never built."""

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
    """Return the neighbor list of each film for the co-preference score.

    The shape is {film tmdb: [(similarity, anchor tmdb, anchor weight),
    ...]}, most similar first. This is the input to _copref_value.
    Fitzflix builds it 1 time for each compute or evaluation. A new rank
    for each fold is cheap.
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
    """Return the co-preference term of 1 film from its sorted neighbor
    list. An optional anchor is excluded (leave-one-out purity)."""

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


# A chip appears only if the co-preference DOMINATES the pick (the term
# equals or exceeds the full taste score). Typical terms are about 2
# across most of the library. Thus, an absolute minimum alone would put
# a chip on each film and make noise.

COPREF_CHIP_THRESHOLD = 0.5


def _copref_top_anchor(entries):
    """Return the anchor with the largest effect in the top-K window, or
    None. The "liked by people who liked ..." chip names this film."""

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


# Estimated ratings (#45a): a quantile match of the engine score of a
# candidate onto the own star distribution of the user. The curve comes
# from the leave-one-out scores of the rated films of the user. Each
# film scores against a profile built without it. Thus, a film cannot
# improve its own estimate. The curve needs a sufficient number of
# rated films to have a meaning.

CALIBRATION_MIN_RATED = 20


def build_calibration(
    user_id, weights, features, entries_by_tmdb, tmdb_of, award_counts
):
    """Return the score-to-stars calibration curve for 1 user, or None.

    The shape is {"scores": [...], "stars": [...]}. Each list is sorted
    in ascending order. The fractional position of a candidate score in
    the LOO scores reads out at the same position in the sorted ratings.
    The scores follow the stored-recommendation recipe exactly (taste
    plus co-preference, plus the award prior if the taste is positive).
    Thus, the stored scores translate directly.
    """

    ratings = {
        movie_id: rating
        for movie_id, rating in latest_ratings(user_id).items()
        if rating is not None and features.get(movie_id)
    }
    if len(ratings) < CALIBRATION_MIN_RATED:
        return None

    scores = []
    for movie_id in ratings:
        remaining = {m: w for m, w in weights.items() if m != movie_id}
        profile = build_profile(remaining, features)
        taste, _ = score_movie(features[movie_id], profile)
        held_tmdb = tmdb_of.get(movie_id)
        total = taste + _copref_value(
            entries_by_tmdb.get(held_tmdb, []), excluded=held_tmdb
        )
        wins, nominations = award_counts.get(movie_id, (0, 0))
        if taste > 0:
            total += award_prior(wins, nominations)
        scores.append(round(total, 4))

    return {
        "scores": sorted(scores),
        "stars": sorted(float(rating) for rating in ratings.values()),
    }


def estimated_rating(profile, score):
    """Return the probable star rating of the user for a film with the
    score `score`, or None if no curve is stored.

    The rating comes from the stored calibration curve of the profile.
    It has full precision. Thus, the widget can fill partial stars.
    Submitted ratings stay whole. Only estimates are fractional."""

    calibration = (profile or {}).get("calibration") or {}
    scores = calibration.get("scores") or []
    stars = calibration.get("stars") or []
    if not scores or not stars:
        return None
    low = bisect.bisect_left(scores, score)
    high = bisect.bisect_right(scores, score)
    position = (low + high) / 2 / len(scores)
    index = position * (len(stars) - 1)
    lower = stars[int(index)]
    upper = stars[min(int(index) + 1, len(stars) - 1)]
    value = lower + (upper - lower) * (index - int(index))
    return max(0.5, min(5.0, round(value, 2)))


def _tmdb_copref(user_id, tmdb_id):
    """Return the co-preference for 1 film from its own side of the pair
    table.

    This intersects the stored neighbors of the film with the weighted
    films of the user. These are the same entries that
    compute_user_recommendations builds on the anchor side. This does
    not fetch the full neighbor list of each anchor. All keys are TMDB
    ids. Thus, films without a record carry the signal too."""

    if not tmdb_id:
        return 0.0
    neighbor_sims = dict(
        db.session.query(MovieCopref.tmdb_id_b, MovieCopref.similarity).filter(
            MovieCopref.tmdb_id_a == int(tmdb_id)
        )
    )
    if not neighbor_sims:
        return 0.0
    weights = user_movie_weights(user_id)
    weights_by_tmdb = {
        neighbor_id: weights[movie_id]
        for movie_id, neighbor_id in db.session.query(Movie.id, Movie.tmdb_id)
        .filter(Movie.id.in_(list(weights) or [0]))
        .filter(Movie.tmdb_id.in_(list(neighbor_sims)))
    }
    entries = sorted(
        (
            (neighbor_sims[neighbor_id], neighbor_id, weight)
            for neighbor_id, weight in weights_by_tmdb.items()
        ),
        key=lambda entry: -entry[0],
    )
    return _copref_value(entries)


def single_movie_score(user_id, movie, profile):
    """Return the live score of 1 film with the stored-recommendation
    recipe.

    The recipe is taste plus co-preference plus the award prior. Thus,
    a film outside the stored ranking (an unowned record, a candidate
    below the cut, a taste mismatch) can carry an estimated rating.
    This returns None until the TMDB data of the film arrives. A record
    in the middle of a refresh has only its decade for the score. That
    would read as a taste mismatch and give an incorrect low
    estimate."""

    if not profile or movie.tmdb_data_as_of is None:
        return None

    taste, _ = score_movie(collect_features([movie.id]).get(movie.id, []), profile)
    total = taste + _tmdb_copref(user_id, movie.tmdb_id)
    if taste > 0:
        wins, nominations = 0, 0
        for win, tally in (
            db.session.query(MovieAward.win, db.func.count())
            .filter(MovieAward.movie_id == movie.id)
            .group_by(MovieAward.win)
        ):
            if win:
                wins = tally
            else:
                nominations = tally
        total += award_prior(wins, nominations)
    return total


def not_interested_movie_ids(user_id):
    """Return the movie ids that the user refused. Each recommendation
    surface excludes them."""

    return {
        movie_id
        for (movie_id,) in db.session.query(UserMovieStatus.movie_id).filter(
            UserMovieStatus.user_id == int(user_id),
            UserMovieStatus.kind == "not_interested",
        )
    }


def local_candidates(user_id):
    """Return the movie ids with a local full-feature file, without the
    films that the user logged or refused.

    The landing page recommends only films that are on the shelf,
    unseen, and not refused."""

    seen = db.session.query(UserMovieReview.movie_id).filter(
        UserMovieReview.user_id == int(user_id),
        UserMovieReview.movie_id.isnot(None),
    )
    refused = db.session.query(UserMovieStatus.movie_id).filter(
        UserMovieStatus.user_id == int(user_id),
        UserMovieStatus.kind == "not_interested",
    )
    rows = (
        db.session.query(File.movie_id)
        .filter(File.movie_id.isnot(None), File.feature_type_id.is_(None))
        .filter(~File.movie_id.in_(seen))
        .filter(~File.movie_id.in_(refused))
        .distinct()
        .all()
    )
    return [movie_id for (movie_id,) in rows]


def scoreable_records(user_id):
    """Return the movie records without files that have refreshed TMDB
    data and that the user did not log or refuse.

    These are catalog and watchlist records. Their pages can show an
    estimated rating from the nightly score map."""

    seen = db.session.query(UserMovieReview.movie_id).filter(
        UserMovieReview.user_id == int(user_id),
        UserMovieReview.movie_id.isnot(None),
    )
    refused = db.session.query(UserMovieStatus.movie_id).filter(
        UserMovieStatus.user_id == int(user_id),
        UserMovieStatus.kind == "not_interested",
    )
    rows = (
        db.session.query(Movie.id)
        .filter(Movie.tmdb_data_as_of.isnot(None))
        .filter(~Movie.files.any(File.feature_type_id.is_(None)))
        .filter(~Movie.id.in_(seen))
        .filter(~Movie.id.in_(refused))
        .all()
    )
    return [movie_id for (movie_id,) in rows]


def compute_user_recommendations(user_id, limit=STORED_RECOMMENDATIONS):
    """Return (profile, ranked recommendations, score map) for 1 user, or
    (None, [], {}) for a user with no diary rows.

    The score map covers each scoreable unlogged film with the full
    recipe. This includes owned candidates AND records without files
    that have TMDB data. The map comes before the positives-only cut of
    the ranking. Thus, estimated ratings can render on each surface."""

    weights = user_movie_weights(user_id)
    if not weights:
        return None, [], {}

    candidates = local_candidates(user_id)
    extras = scoreable_records(user_id)
    scoreable = list(dict.fromkeys(candidates + extras))
    features = collect_features(list(set(scoreable) | set(weights)))
    profile = build_profile(weights, features)

    # The stored cut must survive the render-time exclusions. The
    # landing page moves watchlisted films out of the discovery pool
    # onto the top watchlist shelf. The watchlist has no limit by
    # design. Glenn queued 500 films on Netflix 1 time. Thus, the cut
    # grows by the number of watchlisted candidates. That keeps at
    # least `limit` films for discovery. The no-repeat cycle of the rail
    # stays at 1 month or longer.

    watchlisted = {
        movie_id
        for (movie_id,) in db.session.query(UserWatchlist.movie_id).filter(
            UserWatchlist.user_id == int(user_id)
        )
    }
    depth = limit + len(watchlisted & set(candidates))

    # The marker bar goes with the profile. It is the baseline
    # percentile of the coarse scores across the own candidates of this
    # user. A saturated profile rates almost each film highly. Thus,
    # "might interest you" has a meaning only relative to that
    # baseline.

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

    # Co-preference: the anchors are the own weighted films of the user.
    # They match into the similarity table by TMDB id. The candidates
    # collect their top-neighbor term in the same way that the
    # evaluation measures it.

    tmdb_of = dict(
        db.session.query(Movie.id, Movie.tmdb_id)
        .filter(Movie.id.in_(list(set(scoreable) | set(weights)) or [0]))
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

    # The estimated-rating curve goes with the stored profile. It comes
    # from the same weights, similarities, and prior rules that the
    # ranking below uses. Thus, the stored scores translate directly to
    # stars.

    profile["calibration"] = build_calibration(
        user_id, weights, features, entries_by_tmdb, tmdb_of, award_counts
    )

    candidate_set = set(candidates)
    scores_map = {}
    ranked = []
    for movie_id in scoreable:
        movie_features = features.get(movie_id, [])
        if not movie_features:
            continue
        score, contributions = score_movie(movie_features, profile)
        entries = entries_by_tmdb.get(tmdb_of.get(movie_id), [])
        copref = _copref_value(entries)
        total = score + copref
        # The award prior keeps its original taste gate. Awards only
        # decorate films that the profile itself already scores positive.
        wins, nominations = award_counts.get(movie_id, (0, 0))
        prior = award_prior(wins, nominations)
        if score > 0:
            total += prior
        scores_map[movie_id] = round(total, 4)
        if movie_id not in candidate_set or total <= 0:
            continue
        because = [
            label for contribution, label in contributions[:4] if contribution > 0
        ]
        if copref >= COPREF_CHIP_THRESHOLD and copref >= score:
            title = anchor_titles.get(_copref_top_anchor(entries))
            if title:
                because.insert(0, f"liked by people who liked {title}")
        if score > 0 and prior > 0:
            because.append(award_label(wins, nominations))
        ranked.append(
            {"movie_id": movie_id, "score": round(total, 4), "because": because}
        )

    ranked.sort(key=lambda rec: rec["score"], reverse=True)
    return profile, ranked[:depth], scores_map


# The "Watch it again" shelf: owned films that the user liked and that
# the user last watched a long time ago. This is the complement of the
# engine. The candidates of the engine exclude logged films by design.
# The sentiment uses the diary weights again. The staleness adds a
# bonus on top. The bonus saturates at the horizon. Rows without a date
# (drive ratings, watches before Fitzflix) count as the oldest. Glenn
# measured the 2-year bar before he chose it. 475 of his 556 liked
# owned films are beyond it. That is sufficient for a 12-card rotation.

REWATCH_STALENESS_YEARS = 2
REWATCH_STALENESS_WEIGHT = 0.5
REWATCH_STALENESS_HORIZON_YEARS = 10


def watch_again_shelf(user_id, today=None):
    """Return the ranked rewatch candidates for 1 user.

    These are owned films that the user liked (the liked flag, or a
    rating above the own mean of the user). The last recorded watch, if
    there is one, is at least the staleness bar in the past. The most
    liked and longest unseen films come first."""

    today = today or datetime.now()
    rows = (
        db.session.query(
            UserMovieReview.movie_id,
            db.func.max(UserMovieReview.date_watched),
            db.func.max(db.case((UserMovieReview.liked == True, 1), else_=0)),
        )
        .join(Movie, Movie.id == UserMovieReview.movie_id)
        .filter(UserMovieReview.user_id == int(user_id))
        .filter(Movie.files.any(File.feature_type_id.is_(None)))
        .group_by(UserMovieReview.movie_id)
        .all()
    )
    current = latest_ratings(user_id)
    ratings = [
        current[movie_id]
        for movie_id, _, _ in rows
        if current.get(movie_id) is not None
    ]
    mean_rating = sum(ratings) / len(ratings) if ratings else 0.0
    weights = user_movie_weights(user_id)

    items = []
    for movie_id, last_watched, liked in rows:
        rating = current.get(movie_id)
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
    """Return a daily walk through a ranked list, with no repeats.

    The ranking splits into `count` contiguous quality tiers. Each day
    shows 1 film from each tier. A continuous day counter is the index.
    Thus, each film appears exactly 1 time for each cycle. The cycle
    length is the tier size, about len/count days. Each day mixes all
    quality tiers. The full set refreshes before a film repeats. The
    result is deterministic for each day. A short list passes through
    whole.
    """

    if len(items) <= count:
        return list(items)
    picks = []
    for tier in range(count):
        # Balanced boundaries: the tier sizes differ by 1 at most. No
        # tier is empty. Thus, a short list still fills each slot.
        tier_items = items[
            tier * len(items) // count : (tier + 1) * len(items) // count
        ]
        picks.append(tier_items[day_index % len(tier_items)])
    return picks


def rotate_daily(items, count, seed, decay=0.93):
    """Return a selection of `count` items from a ranked list that
    changes each day.

    This is weighted sampling without replacement. It favors the top of
    the ranking geometrically. Thus, the quality holds while the middle
    rotates. The seed must embed the user and the calendar day. Then
    the page is stable across reloads but new across days. The
    selection comes back in the original rank order. The result is
    deterministic for a given seed.
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
    """Return a deterministic day-seeded shuffle of the picked cards of a
    shelf.

    The pickers return rows in quality order. Watchlist pins come first.
    Then come the tiers of the ranking, best first. That made the slot
    position a quality signal. The first cards were always the amber
    block and the top tier. Glenn asked to mix them. A shuffle with a
    user-and-day seed changes the arrangement daily. Reloads stay
    stable. The amber badge marks the watchlist cards in each position.
    The leaving shelf does not use this by design. Its watchlist-first
    order is urgency, not discovery.
    """

    rng = random.Random(seed)
    shuffled = list(items)
    rng.shuffle(shuffled)
    return shuffled


# The frozen shelves of the day (#204). A shelf must show the same
# films in the same slots all day. The pickers are deterministic for
# each day. But their INPUTS move during the day. A logged film makes
# the ranked list smaller and moves each tier boundary. That caused the
# cards to shuffle again. The keys are for each user, each shelf, and
# each calendar day. They expire on their own. Thus, the first render
# of the next day starts new from the live state.

SHELF_SNAPSHOT_KEY = "fitzflix:shelf:{shelf}:{user_id}:{day}"
SHELF_SNAPSHOT_TTL = 2 * 86400


def frozen_shelf(redis_client, user_id, shelf, eligible_ids, pick, day=None):
    """Return the stable ordered card ids of the day for 1 shelf.

    The first render of the day calls pick() for the baseline and
    stores a snapshot. Later renders replay the snapshot in slot order.
    A card can stop being eligible (watched, refused, acquired). Then
    the first id from eligible_ids that does not show yet replaces it
    IN ITS SLOT. Each other card keeps its position. The rule of Glenn:
    a replacement of a watched film during the day is fine, but the
    rest must never move. eligible_ids carries each id that can show
    now, in replacement-priority order. A slot with no remaining
    replacement closes.
    """

    day = day or date.today().isoformat()
    key = SHELF_SNAPSHOT_KEY.format(shelf=shelf, user_id=int(user_id), day=day)
    eligible = list(eligible_ids)
    eligible_set = set(eligible)
    snapshot = None
    stored = redis_client.get(key)
    if stored:
        try:
            snapshot = json.loads(stored)
        except (TypeError, ValueError):
            snapshot = None
    if snapshot is None:
        snapshot = list(pick())
        redis_client.set(key, json.dumps(snapshot), ex=SHELF_SNAPSHOT_TTL)
        return snapshot

    showing = set(snapshot)
    # Lazy on purpose: `showing` grows as replacements land, and the
    # generator's membership test reads the live set
    replacements = (card_id for card_id in eligible if card_id not in showing)
    cards = []
    changed = False
    for card_id in snapshot:
        if card_id in eligible_set:
            cards.append(card_id)
            continue
        changed = True
        replacement = next(replacements, None)
        if replacement is not None:
            cards.append(replacement)
            showing.add(replacement)
    if changed:
        redis_client.set(key, json.dumps(cards), ex=SHELF_SNAPSHOT_TTL)
    return cards


def daily_shelf(
    redis_client,
    user_id,
    shelf,
    rows,
    shown,
    key,
    urgent=(),
    day=None,
    count=12,
    freeze=True,
):
    """Return the cards of the day for 1 landing shelf from the shared
    recipe (Glenn, 2026-08-30).

    The watchlist has its own top shelf. Thus, each shelf below it is
    only a discovery surface. All of them pick in the same way. No shelf
    manages its own pin lane.

    `rows` is the candidate list of the shelf, best first by the own
    taste ranking of the shelf. The source already excludes watchlisted
    films. `urgent` rows are the leaving-soon films of the watchlist
    shelf. They always show first, in the given order. They never
    shuffle. There, the position IS the urgency signal. The remaining
    slots walk the quality tiers of rotate_partition. Thus, each day
    mixes the full quality range. Nothing repeats until the pool cycles.
    Then shuffle_daily puts them into day-stable random slots.

    `key` extracts the page-wide card id of a row. This is a tmdb id if
    one exists. Thus, the page can recognize the same film across
    shelves. `shown` is the cross-shelf claim set of the page. This
    function changes it. One render never shows a film 2 times. Thus,
    the caller must pick its shelves in a day-stable order. A replayed
    snapshot claims its films exactly as the first render of the day
    did. With `freeze`, the first render of the day stores a snapshot
    of the result. Later renders replay it slot for slot through
    frozen_shelf. The ?minutes= view passes freeze=False. It picks live
    over the rows that fit. It is a transient planning view, not the
    shelf (#204).
    """

    day = day or date.today()

    def pick():
        """Return the baseline card ids of the day for this shelf."""

        lead = [row for row in urgent if key(row) not in shown][:count]
        pool = [row for row in rows if key(row) not in shown]
        sampled = rotate_partition(pool, count - len(lead), day.toordinal())
        mixed = shuffle_daily(sampled, f"mix:{shelf}:{int(user_id)}:{day.isoformat()}")
        return [key(row) for row in lead + mixed]

    by_key = {key(row): row for row in list(rows) + list(urgent)}
    if freeze:
        ids = frozen_shelf(
            redis_client,
            user_id,
            day=day.isoformat(),
            shelf=shelf,
            # The replacement priority is the same as the pick: urgent
            # rows, then the ranking. It never includes a film that a
            # different shelf shows.
            eligible_ids=[
                row_key
                for row_key in [key(row) for row in urgent] + [key(row) for row in rows]
                if row_key not in shown
            ],
            pick=pick,
        )
    else:
        ids = pick()
    shown.update(ids)
    return [by_key[row_key] for row_key in ids if row_key in by_key]


def stored_recommendations(redis, user_id):
    """Return the stored payload of the nightly recompute for a user, or None."""

    payload = redis.get(RECS_KEY.format(user_id=int(user_id)))
    return json.loads(payload) if payload else None


def stored_scores(redis, user_id):
    """Return the score map, or {} before the first compute.

    The map is {movie_id: full-recipe score} over each scoreable
    unlogged film. The nightly base merges with the live-scored patch
    overlay. The base wins. A film in both got a new score overnight
    from newer inputs. JSON keys come back as strings. Thus, this
    converts them to int again."""

    scores = {}
    for movie_id, score in redis.hgetall(
        PATCH_SCORES_KEY.format(user_id=int(user_id))
    ).items():
        scores[int(movie_id)] = float(score)
    payload = redis.get(SCORES_KEY.format(user_id=int(user_id)))
    if payload:
        for movie_id, score in json.loads(payload).items():
            scores[int(movie_id)] = score
    return scores


def resolved_score(redis, user_id, movie, profile, scores=None):
    """Return the engine score of the film from the 1 shared source.

    If the stored map covers the film, this returns the stored score.
    If not, this runs the same recipe live. It patches the result back
    into the map. Thus, the next surface that asks (a tile batch, the
    movie page, the rate drive) reads the identical number. It does not
    recompute its own. This returns None for a film that cannot get a
    score yet (no profile, or TMDB data not arrived). A batch caller
    passes its `scores` map to skip the Redis read for each film."""

    if scores is None:
        scores = stored_scores(redis, user_id)
    if movie.id in scores:
        return scores[movie.id]
    score = single_movie_score(user_id, movie, profile)
    if score is not None:
        score = round(score, 4)
        key = PATCH_SCORES_KEY.format(user_id=int(user_id))
        redis.hset(key, str(movie.id), score)
        redis.expire(key, PATCH_SCORES_TTL)
    return score


def resolved_tmdb_score(redis, user_id, tmdb_id, profile, scores=None):
    """Return the score for a film through the TMDB-keyed lane of the
    shared source. The film can be absent from the local database.

    A film with a local record answers through the movie-id lane. That
    is the full recipe against the stored map. A film without a record
    scores from its cached enriched TMDB payload in the same portable
    feature key space. It adds the tmdb-keyed co-preference. The award
    prior needs local rows. Thus, it is absent. A TMDB-keyed overlay
    holds the score. Thus, each surface reads 1 number, and the
    database does not grow. When the film gets a record (a watchlist
    add, a log, an import), the movie-id lane takes over. This returns
    None if the film cannot get a score: no profile, or TMDB not
    reachable with nothing cached."""

    movie = Movie.query.filter_by(tmdb_id=int(tmdb_id)).first()
    if movie is not None:
        return resolved_score(redis, user_id, movie, profile, scores=scores)
    if not profile:
        return None
    key = TMDB_PATCH_SCORES_KEY.format(user_id=int(user_id))
    cached = redis.hget(key, str(int(tmdb_id)))
    if cached is not None:
        return float(cached)

    # streaming_rail owns the enriched-payload cache and its feature
    # extraction. This imports it lazily because it imports this module.

    from app.streaming_rail import _payload_features, enriched_movie

    payload = enriched_movie(tmdb_id)
    if not payload:
        return None
    taste, _ = score_movie(_payload_features(payload), profile)
    score = round(taste + _tmdb_copref(user_id, int(tmdb_id)), 4)
    redis.hset(key, str(int(tmdb_id)), score)
    redis.expire(key, PATCH_SCORES_TTL)
    return score


def stored_profile(redis, user_id):
    """Return the stored taste profile of the nightly recompute for a user, or None."""

    payload = redis.get(PROFILE_KEY.format(user_id=int(user_id)))
    return json.loads(payload) if payload else None


def recommended_movie_ids(redis, user_id):
    """Return the movie ids in the stored library recommendations of the
    user.

    A film in the set is one that the nightly recompute ranked among
    the top candidates of the user. This is the same set that feeds the
    library rail of the landing page. Thus, other surfaces can badge
    owned films as "might interest you" without a new score.
    """

    stored = stored_recommendations(redis, user_id)
    return {item["movie_id"] for item in (stored or {}).get("items", [])}


def coarse_interest_score(profile, genre_ids, year, person_affinity=0.0):
    """Return the coarse score for the might-interest markers.

    This can compute from each payload that carries genre ids and a
    year. It uses the soft average of the matched genre affinities, the
    release decade, and an optional affinity for a person in the film.
    This makes no TMDB calls. It stores nothing."""

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
    """Return the tmdb ids on a filmography page that get a "might
    interest you" marker.

    This scores only films without a local record. Owned films get the
    full engine. This uses only the cached credits payload: the genre
    ids, the release decade, and the affinity of the user for this
    person. The limit is the strongest few for each career page.
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
    """Rebuild the taste profile and the ranked recommendations of each
    reviewer into Redis.

    This is a nightly task. The landing page and the filmography
    markers read the results."""

    with app.app_context():
        computed_at = datetime.now().strftime("%Y-%m-%d %H:%M")
        user_ids = [
            user_id
            for (user_id,) in db.session.query(UserMovieReview.user_id)
            .filter(UserMovieReview.user_id.isnot(None))
            .distinct()
        ]
        for user_id in user_ids:
            profile, ranked, scores = compute_user_recommendations(user_id)
            if profile is None:
                continue
            current_app.redis.set(
                PROFILE_KEY.format(user_id=user_id), json.dumps(profile)
            )
            current_app.redis.set(
                RECS_KEY.format(user_id=user_id),
                json.dumps({"computed_at": computed_at, "items": ranked}),
            )
            # The new map replaces each live-scored patch. Delete both
            # overlays in the same pipeline. Thus, no read sees the new
            # base with old patches under it. The tmdb-lane scores derive
            # again from the new profile. Their cached payloads make that
            # cheap.
            pipeline = current_app.redis.pipeline()
            pipeline.set(SCORES_KEY.format(user_id=user_id), json.dumps(scores))
            pipeline.delete(PATCH_SCORES_KEY.format(user_id=user_id))
            pipeline.delete(TMDB_PATCH_SCORES_KEY.format(user_id=user_id))
            pipeline.execute()
            current_app.logger.info(
                f"Recommendations: stored {len(ranked)} films "
                f"({len(scores)} scored) for user {user_id}"
            )
        return True


def evaluate_user(user_id, class_weights=None, positive_threshold=0.5):
    """Return the leave-one-out ranking metrics for 1 user under the
    given (or current) class weights.

    This removes each film that the user clearly liked from the
    profile, 1 at a time. It ranks the film against each local
    candidate plus itself. A good weighting ranks the held-out film
    near the top. This returns None for a user without a sufficient
    number of positive films.
    """

    weights = user_movie_weights(user_id)
    positives = [
        movie_id for movie_id, weight in weights.items() if weight >= positive_threshold
    ]
    if len(positives) < 2:
        return None

    candidates = local_candidates(user_id)
    features = collect_features(list(set(candidates) | set(weights)))

    # The shipped co-preference term goes into the evaluation too. It is
    # leave-one-out pure. The held-out film of each fold is removed from
    # each neighbor list that it anchors.

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
