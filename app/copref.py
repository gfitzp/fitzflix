"""Co-preference similarities from MovieLens, for the movie_copref table.

The taste engine's content features can't see tone or quality within a
genre; thirty-two million MovieLens ratings can. This module builds the
similarity table those signals live in: adjusted-cosine similarity
(each rating centered on its rater's global mean) between every pair of
library-matched films, shrunk by co-rater count so a handful of shared
fans can't fake a strong link. MovieLens is the sanctioned source — the
Netflix Prize dataset was withdrawn in 2010 and carries no external
ids, while GroupLens's ML-32M is research-licensed and maps straight to
TMDb ids via its links.csv.

The build is deliberately offline and rare: it reruns only when a new
MovieLens snapshot is adopted or the library grows far beyond the
matched set, via `flask recs copref --dataset <dir>` pointed at an
extracted ml-32m directory. numpy and scipy are imported inside the
build function and are NOT runtime dependencies — the engine reads the
finished table with plain queries, and the scoring stays arithmetic.
Dataset download: https://files.grouplens.org/datasets/movielens/
(ml-32m.zip; research/non-commercial license, no redistribution).
"""

import csv
import os

from flask import current_app

from app import db
from app.models import Movie, MovieCopref

# Similarity hygiene: pairs sharing fewer than CO_RATER_FLOOR raters are
# noise and drop; the shrinkage discounts thinly-shared pairs; sims
# below MIN_SIMILARITY aren't worth a row (scoring only uses positive
# neighbors)

CO_RATER_FLOOR = 10
CO_RATER_SHRINKAGE = 100.0
MIN_SIMILARITY = 0.02

INSERT_CHUNK = 10000


def build_copref_table(dataset_dir):
    """Rebuild movie_copref from an extracted MovieLens dataset.

    Covers every Movie record with a TMDb id that MovieLens knows —
    owned films, diary records, and the Criterion catalog alike — so
    future diary growth finds its similarities already stored. Replaces
    the table wholesale. Returns a summary string.
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

    ml_by_tmdb = {}
    with open(links_path, newline="") as handle:
        for row in csv.DictReader(handle):
            if row["tmdbId"].strip():
                ml_by_tmdb[int(row["tmdbId"])] = int(row["movieId"])

    tmdb_ids = [
        tmdb_id
        for (tmdb_id,) in db.session.query(Movie.tmdb_id)
        .filter(Movie.tmdb_id.isnot(None))
        .distinct()
        if tmdb_id in ml_by_tmdb
    ]
    ml_of_tmdb = {t: ml_by_tmdb[t] for t in tmdb_ids}
    ml_ids = sorted(set(ml_of_tmdb.values()))
    column_of_ml = {ml: index for index, ml in enumerate(ml_ids)}
    tmdb_of_column = {}
    for tmdb_id, ml in ml_of_tmdb.items():
        tmdb_of_column.setdefault(column_of_ml[ml], tmdb_id)

    # One streaming pass: per-user rating means over the whole dataset
    # (adjusted cosine centers on the rater's global habits), plus the
    # rows touching matched films

    user_sum = {}
    user_count = {}
    user_index = {}
    rows_user, rows_column, rows_rating = [], [], []
    with open(ratings_path, newline="") as handle:
        reader = csv.reader(handle)
        next(reader)
        for user_id, ml_id, rating, _ in reader:
            user_id = int(user_id)
            value = float(rating)
            user_sum[user_id] = user_sum.get(user_id, 0.0) + value
            user_count[user_id] = user_count.get(user_id, 0) + 1
            column = column_of_ml.get(int(ml_id))
            if column is not None:
                if user_id not in user_index:
                    user_index[user_id] = len(user_index)
                rows_user.append(user_index[user_id])
                rows_column.append(column)
                rows_rating.append(value)

    means = np.zeros(len(user_index), dtype=np.float32)
    for user_id, index in user_index.items():
        means[index] = user_sum[user_id] / user_count[user_id]
    users = np.array(rows_user, dtype=np.int32)
    columns = np.array(rows_column, dtype=np.int32)
    centered = np.array(rows_rating, dtype=np.float32) - means[users]

    shape = (len(user_index), len(ml_ids))
    matrix = sparse.csr_matrix((centered, (users, columns)), shape=shape)
    binary = sparse.csr_matrix(
        (np.ones(len(users), dtype=np.float32), (users, columns)), shape=shape
    )

    gram = (matrix.T @ matrix).toarray()
    co_counts = (binary.T @ binary).toarray()
    norms = np.sqrt(np.diag(gram))
    norms[norms == 0] = 1.0
    similarities = gram / np.outer(norms, norms)
    similarities *= co_counts / (co_counts + CO_RATER_SHRINKAGE)
    np.fill_diagonal(similarities, 0.0)
    similarities[co_counts < CO_RATER_FLOOR] = 0.0

    # Store positive, meaningful pairs in both directions so an
    # anchor-side lookup is one indexed query

    keep_a, keep_b = np.nonzero(similarities >= MIN_SIMILARITY)
    MovieCopref.query.delete(synchronize_session=False)
    batch = []
    stored = 0
    for a, b in zip(keep_a.tolist(), keep_b.tolist()):
        tmdb_a = tmdb_of_column.get(a)
        tmdb_b = tmdb_of_column.get(b)
        if tmdb_a is None or tmdb_b is None:
            continue
        batch.append(
            {
                "tmdb_id_a": tmdb_a,
                "tmdb_id_b": tmdb_b,
                "similarity": round(float(similarities[a, b]), 4),
            }
        )
        stored += 1
        if len(batch) >= INSERT_CHUNK:
            db.session.bulk_insert_mappings(MovieCopref, batch)
            batch = []
    if batch:
        db.session.bulk_insert_mappings(MovieCopref, batch)
    db.session.commit()

    summary = (
        f"Stored {stored} co-preference pairs over {len(ml_ids)} matched films "
        f"(from {len(rows_rating)} ratings by {len(user_index)} raters)"
    )
    current_app.logger.info(summary)
    return summary
