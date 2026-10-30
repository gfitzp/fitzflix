"""Get the film awards from Wikidata.

This module gets the award wins (P166) and the nominations (P1411) for
the films in the library. It matches the films through the IMDb id
(P345) or the TMDB id (P4947) that Fitzflix already stores. Wikidata
has both ids. Wikidata is the approved source. TMDB has no awards API.
The IMDb API needs a paid license. Access follows the guidelines of
Wikidata, as the Criterion spine lookup does. This module sends a
descriptive User-Agent with a contact address. It batches the VALUES
queries some hundreds of ids at a time. It pauses between requests.
Coverage is strong for the major ceremonies (Oscars, BAFTA, Cannes,
Golden Globes, Césars). Coverage is thin for the small festivals.
Thus, the pages show the awards that exist and do not claim that the
list is complete. Reads of Wikidata are approved. Writes to Wikidata
were declined.

Wikidata records the craft categories (Best Director, Best Actor, and
more) on person items, with a "for work" (P1686) qualifier that names
the film. Thus, each batch runs a film query and a craft query that
starts from the film, and writes the joined rows 1 time.
"""

import time
import traceback

from flask import current_app
from werkzeug.local import LocalProxy

from app import db, get_app
from app.criterion_catalog import _wikidata_sparql
from app.models import Movie, MovieAward

# This is the app instance of this process. Fitzflix resolves it lazily.
# Thus, a process that already has an application can import this module
# and not build a second application.

app = LocalProxy(get_app)

# The VALUES batches keep each request small. The pause between requests
# keeps the crawl polite. The whole library refreshes in about 40
# requests.

AWARDS_BATCH_SIZE = 200
AWARDS_BATCH_PAUSE_SECONDS = 1.0

# WDQS permits a client 30 error queries per minute. After that, the
# throttle escalates toward a ban. Thus, a failed batch waits longer
# than a good batch. A run of consecutive failures means that the
# service itself has a problem. Then stop, and let the weekly run
# repair the data.

AWARDS_ERROR_PAUSE_SECONDS = 10.0
AWARDS_MAX_CONSECUTIVE_FAILURES = 5

# One query shape serves the two id systems. {id_prop} is the Wikidata
# property that the external ids match (P345 = IMDb, P4947 = TMDB).

AWARDS_QUERY = """
SELECT ?ext ?award ?awardLabel ?kind (YEAR(?when) AS ?year) WHERE {{
  VALUES ?ext {{ {values} }}
  ?film wdt:{id_prop} ?ext .
  {{ ?film p:P166 ?statement . ?statement ps:P166 ?award . BIND("win" AS ?kind) }}
  UNION
  {{ ?film p:P1411 ?statement . ?statement ps:P1411 ?award . BIND("nomination" AS ?kind) }}
  OPTIONAL {{ ?statement pq:P585 ?when . }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
"""

# The craft query starts from the FILM. It walks backwards into each
# award statement whose "for work" qualifier names the film. The query
# never names the item that holds the statement (usually a person).
# Thus, the query stays selective enough for the 60-second limit of
# WDQS.

CRAFT_AWARDS_QUERY = """
SELECT ?ext ?award ?awardLabel ?kind (YEAR(?when) AS ?year) WHERE {{
  VALUES ?ext {{ {values} }}
  ?film wdt:{id_prop} ?ext .
  ?statement pq:P1686 ?film .
  {{ ?statement ps:P166 ?award . BIND("win" AS ?kind) }}
  UNION
  {{ ?statement ps:P1411 ?award . BIND("nomination" AS ?kind) }}
  OPTIONAL {{ ?statement pq:P585 ?when . }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
"""


def _parse_award(binding):
    """Return one binding as (award_id, name, win, year), or None.

    Return None if the binding is not usable. When the label service
    has no label, it echoes the QID back as the label. A bare QID badge
    tells the user nothing. Thus, this function drops those rows.
    """

    award_uri = binding.get("award", {}).get("value", "")
    award_id = award_uri.rsplit("/", 1)[-1]
    if not award_id.startswith("Q"):
        return None
    name = (binding.get("awardLabel", {}).get("value") or "").strip()
    if not name or name == award_id:
        return None
    win = binding.get("kind", {}).get("value") == "win"
    year = binding.get("year", {}).get("value", "")
    year = int(year) if year.isdigit() else None
    return (award_id, name, win, year)


def _award_rows(bindings, movie_ids_by_ext):
    """Return the bindings as {movie_id: {(award_id, name, win, year)}}.

    A set per film removes the duplicates. Wikidata sometimes has the
    same award statement more than one time. For example, a re-import
    can add the statement with and without a date qualifier. The unique
    constraint treats NULL years as distinct. Thus, this function must
    remove the duplicates. _fold_undated removes the undated twin of a
    dated row.
    """

    rows = {}
    for binding in bindings:
        ext = binding.get("ext", {}).get("value")
        movie_id = movie_ids_by_ext.get(ext)
        if movie_id is None:
            continue
        parsed = _parse_award(binding)
        if parsed is None:
            continue
        rows.setdefault(movie_id, set()).add(parsed)
    return {movie_id: _fold_undated(entries) for movie_id, entries in rows.items()}


def _fold_undated(entries):
    """Return the award entries of 1 film without the redundant undated rows (#540).

    A statement with and without its date qualifier gave a dated row and
    an undated row. The set kept both, because the years differ. Thus,
    the film showed the badge 2 times. An undated row goes when a dated
    row has the same award and the same kind (win or nomination). The
    dated row keeps its own name."""

    dated = {(award_id, win) for award_id, _, win, year in entries if year is not None}
    return {
        entry
        for entry in entries
        if entry[3] is not None or (entry[0], entry[2]) not in dated
    }


def _replace_awards(batch_movie_ids, rows):
    """Replace the stored awards of the batch with the new rows.

    This clears each film in the batch, not only the films with results.
    An empty result means that Wikidata lists nothing for the film now.
    If this did not clear the film, stale rows would stay forever. The
    table shows the current truth.
    """

    MovieAward.query.filter(MovieAward.movie_id.in_(batch_movie_ids)).delete(
        synchronize_session=False
    )
    for movie_id, entries in rows.items():
        for award_id, name, win, year in entries:
            db.session.add(
                MovieAward(
                    movie_id=movie_id,
                    award_id=award_id,
                    award_name=name[:512],
                    win=win,
                    year=year,
                )
            )
    db.session.commit()


def refresh_movie_awards():
    """Refresh the award rows of each film from Wikidata, in polite batches.

    A film with an IMDb id matches through P345. The other films match
    through their TMDB id and P4947. Each batch runs the film query and
    the craft query, and _award_rows removes the duplicates of the 2.
    A batch with a failed query keeps its stored rows, and the loop
    continues. The weekly run repairs a partial refresh.
    """

    if not current_app.config["WIKIDATA_SPARQL_URL"]:
        return "WIKIDATA_SPARQL_URL is not configured, skipping awards refresh"

    by_imdb = {
        imdb_id: movie_id
        for movie_id, imdb_id in db.session.query(Movie.id, Movie.imdb_id).filter(
            Movie.imdb_id.isnot(None), Movie.imdb_id != ""
        )
    }
    by_tmdb = {
        str(tmdb_id): movie_id
        for movie_id, tmdb_id in db.session.query(Movie.id, Movie.tmdb_id)
        .filter(Movie.tmdb_id.isnot(None))
        .filter(db.or_(Movie.imdb_id.is_(None), Movie.imdb_id == ""))
    }

    films = 0
    awarded = 0
    consecutive_failures = 0
    for id_prop, mapping in (("P345", by_imdb), ("P4947", by_tmdb)):
        ext_ids = sorted(mapping)
        for start in range(0, len(ext_ids), AWARDS_BATCH_SIZE):
            batch = ext_ids[start : start + AWARDS_BATCH_SIZE]
            values = " ".join(f'"{ext}"' for ext in batch)
            try:
                bindings = [
                    binding
                    for query in (AWARDS_QUERY, CRAFT_AWARDS_QUERY)
                    for binding in _wikidata_sparql(
                        current_app.config["WIKIDATA_SPARQL_URL"],
                        query.format(values=values, id_prop=id_prop),
                        timeout=120,
                    )
                ]
            except Exception:
                current_app.logger.warning(traceback.format_exc())
                current_app.logger.warning(
                    f"Awards batch failed ({id_prop}, {len(batch)} ids), moving on"
                )
                consecutive_failures += 1
                if consecutive_failures >= AWARDS_MAX_CONSECUTIVE_FAILURES:
                    current_app.logger.warning(
                        f"Awards refresh aborted after {consecutive_failures} "
                        f"consecutive failed batches — Wikidata is having a "
                        f"bad day; the weekly run will pick it back up"
                    )
                    return (
                        f"Awards refresh aborted after {consecutive_failures} "
                        f"consecutive failures; refreshed {films} films"
                    )
                time.sleep(AWARDS_ERROR_PAUSE_SECONDS)
                continue
            consecutive_failures = 0
            rows = _award_rows(bindings, mapping)
            _replace_awards([mapping[ext] for ext in batch], rows)
            films += len(batch)
            awarded += len(rows)
            time.sleep(AWARDS_BATCH_PAUSE_SECONDS)

    return f"Refreshed awards for {films} films, {awarded} with award records"


def refresh_awards():
    """Refresh the award rows inside an app context, as the weekly task."""

    with app.app_context():
        result = refresh_movie_awards()
        current_app.logger.info(result)
        return result
