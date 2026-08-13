"""Co-preference similarities from MovieLens, for the movie_copref table.

The taste engine's content features can't see tone or quality within a
genre; thirty-two million MovieLens ratings can. This module builds the
similarity table those signals live in: adjusted-cosine similarity
(each rating centered on its rater's global mean) between films, shrunk
by co-rater count so a handful of shared fans can't fake a strong link.
MovieLens is the sanctioned source — the Netflix Prize dataset was
withdrawn in 2010 and carries no external ids, while GroupLens's ML-32M
is research-licensed and maps straight to TMDb ids via its links.csv.

The build covers the WHOLE MovieLens universe above a ratings floor,
not just the films Fitzflix currently knows (Glenn's call, Aug 2026):
each film keeps its strongest neighbors, so any film the library ever
adds finds its similarities already stored — the dataset itself need
not be kept. The build is offline and rare: it reruns only when a new
MovieLens snapshot is adopted, via `flask recs copref --dataset <dir>`
pointed at an extracted ml-32m directory. numpy and scipy are imported
inside the build function and are NOT runtime dependencies — the
engine reads the finished table with plain queries, and the scoring
stays arithmetic. Dataset download:
https://files.grouplens.org/datasets/movielens/ (ml-32m.zip;
research/non-commercial license, no redistribution).
"""

import csv
import os
from array import array

from flask import current_app

from app import db
from app.models import MovieCopref

# Similarity hygiene: films with fewer than MIN_RATERS raters have
# nothing reliable to say and stay out entirely (the shrinkage would
# crush them anyway); pairs sharing fewer than CO_RATER_FLOOR raters
# drop; sims below MIN_SIMILARITY aren't worth a row. Each film keeps
# its NEIGHBOR_LIMIT strongest neighbors, with the pair set symmetric —
# if A keeps B, B also points back at A — so anchor-side lookups see
# every film that considers the anchor close, and vice versa

MIN_RATERS = 50
CO_RATER_FLOOR = 10
CO_RATER_SHRINKAGE = 100.0
MIN_SIMILARITY = 0.02
NEIGHBOR_LIMIT = 100

BLOCK = 2000
INSERT_CHUNK = 10000


def build_copref_table(dataset_dir):
    """Rebuild movie_copref from an extracted MovieLens dataset.

    Replaces the table wholesale. Returns a summary string.
    """

    try:
        import numpy as np
        from scipy import sparse
    except ImportError:
        return (
            "numpy and scipy are required to rebuild co-preference "
            "similarities: pip install numpy scipy (they are build-time "
            "tools only, not runtime dependencies)"
        )

    links_path = os.path.join(dataset_dir, "links.csv")
    ratings_path = os.path.join(dataset_dir, "ratings.csv")
    if not (os.path.exists(links_path) and os.path.exists(ratings_path)):
        return f"No links.csv/ratings.csv under {dataset_dir}"

    tmdb_by_ml = {}
    with open(links_path, newline="") as handle:
        for row in csv.DictReader(handle):
            if row["tmdbId"].strip():
                tmdb_by_ml[int(row["movieId"])] = int(row["tmdbId"])

    # First streaming pass: per-user and per-film tallies, to fix the
    # eligible film set and each rater's global mean

    user_sum = {}
    user_count = {}
    film_count = {}
    with open(ratings_path, newline="") as handle:
        reader = csv.reader(handle)
        next(reader)
        for user_id, ml_id, rating, _ in reader:
            user_id = int(user_id)
            ml_id = int(ml_id)
            user_sum[user_id] = user_sum.get(user_id, 0.0) + float(rating)
            user_count[user_id] = user_count.get(user_id, 0) + 1
            film_count[ml_id] = film_count.get(ml_id, 0) + 1

    # links.csv occasionally maps two MovieLens entries to one TMDb id
    # (splits and re-releases); the first (oldest) entry keeps the tmdb
    # identity so the pair rows stay unique

    seen_tmdb = set()
    ml_ids = []
    for ml_id in sorted(film_count):
        if film_count[ml_id] < MIN_RATERS:
            continue
        tmdb_id = tmdb_by_ml.get(ml_id)
        if tmdb_id is None or tmdb_id in seen_tmdb:
            continue
        seen_tmdb.add(tmdb_id)
        ml_ids.append(ml_id)
    column_of_ml = {ml: index for index, ml in enumerate(ml_ids)}
    tmdb_of_column = [tmdb_by_ml[ml] for ml in ml_ids]

    # Second pass: the centered rating triplets for eligible films, in
    # compact arrays (thirty-odd million python list entries would not
    # be kind to memory)

    rows_user = array("i")
    rows_column = array("i")
    rows_rating = array("f")
    user_index = {}
    with open(ratings_path, newline="") as handle:
        reader = csv.reader(handle)
        next(reader)
        for user_id, ml_id, rating, _ in reader:
            column = column_of_ml.get(int(ml_id))
            if column is None:
                continue
            user_id = int(user_id)
            if user_id not in user_index:
                user_index[user_id] = len(user_index)
            rows_user.append(user_index[user_id])
            rows_column.append(column)
            rows_rating.append(float(rating))

    means = np.zeros(len(user_index), dtype=np.float32)
    for user_id, index in user_index.items():
        means[index] = user_sum[user_id] / user_count[user_id]
    users = np.frombuffer(rows_user, dtype=np.int32)
    columns = np.frombuffer(rows_column, dtype=np.int32)
    centered = np.frombuffer(rows_rating, dtype=np.float32) - means[users]

    shape = (len(user_index), len(ml_ids))
    matrix = sparse.csr_matrix((centered, (users, columns)), shape=shape)
    binary = sparse.csr_matrix(
        (np.ones(len(users), dtype=np.float32), (users, columns)), shape=shape
    )
    norms = np.sqrt(np.asarray(matrix.multiply(matrix).sum(axis=0)).ravel())
    norms[norms == 0] = 1.0

    # Blocked similarity: a film count in the tens of thousands makes
    # the full matrix a memory hazard, so similarities compute in
    # column blocks and only each row's strongest neighbors survive

    pairs = {}
    for start in range(0, len(ml_ids), BLOCK):
        stop = min(start + BLOCK, len(ml_ids))
        gram = (matrix[:, start:stop].T @ matrix).toarray()
        co_counts = (binary[:, start:stop].T @ binary).toarray()
        sims = gram / np.outer(norms[start:stop], norms)
        sims *= co_counts / (co_counts + CO_RATER_SHRINKAGE)
        sims[co_counts < CO_RATER_FLOOR] = 0.0
        for offset in range(stop - start):
            row = sims[offset]
            row[start + offset] = 0.0
            keep = np.nonzero(row >= MIN_SIMILARITY)[0]
            if len(keep) > NEIGHBOR_LIMIT:
                keep = keep[np.argsort(-row[keep])[:NEIGHBOR_LIMIT]]
            a = start + offset
            for b in keep.tolist():
                value = round(float(row[b]), 4)
                pairs[(a, b)] = value
                pairs[(b, a)] = value

    MovieCopref.query.delete(synchronize_session=False)
    batch = []
    for (a, b), value in pairs.items():
        batch.append(
            {
                "tmdb_id_a": tmdb_of_column[a],
                "tmdb_id_b": tmdb_of_column[b],
                "similarity": value,
            }
        )
        if len(batch) >= INSERT_CHUNK:
            db.session.bulk_insert_mappings(MovieCopref, batch)
            batch = []
    if batch:
        db.session.bulk_insert_mappings(MovieCopref, batch)
    db.session.commit()

    summary = (
        f"Stored {len(pairs)} co-preference pairs over {len(ml_ids)} films "
        f"(from {len(centered)} ratings by {len(user_index)} raters)"
    )
    current_app.logger.info(summary)
    return summary
