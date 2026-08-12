"""Film awards from Wikidata.

Award wins (P166) and nominations (P1411) for the library's films,
matched through the IMDb (P345) or TMDb (P4947) ids Fitzflix already
stores — Wikidata carries both. Wikidata is the sanctioned source:
TMDb has no awards API, IMDb's is paid-license only. Access follows
Wikidata's guidelines like the Criterion spine lookup does: a
descriptive User-Agent with a contact address, batched VALUES queries
a few hundred ids at a time, and a pause between requests. Coverage
is strong for major ceremonies (Oscars, BAFTA, Cannes, Golden Globes,
Césars) and patchy for niche festivals, so surfaces display what
exists without implying completeness. Reading Wikidata is sanctioned;
writing to it was declined.
"""

import time
import traceback

import requests

from flask import current_app
from werkzeug.local import LocalProxy

from app import db, get_app
from app.models import Movie, MovieAward

# This process's app instance, resolved lazily so importing this module
# from a process that already has an application doesn't build a second one

app = LocalProxy(get_app)

# VALUES batches keep each request modest; the pause between requests
# keeps the crawl polite (the whole library refreshes in ~20 requests)

AWARDS_BATCH_SIZE = 200
AWARDS_BATCH_PAUSE_SECONDS = 1.0

# One query shape serves both id systems: {id_prop} is the Wikidata
# property the external ids match against (P345 = IMDb, P4947 = TMDb)

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


def _wikidata_sparql(query):
    """Run one SPARQL query against Wikidata, per its access guidelines."""

    contact = current_app.config["SERVER_EMAIL"] or "fitzflix"
    r = requests.get(
        current_app.config["WIKIDATA_SPARQL_URL"],
        params={"query": query},
        headers={
            "User-Agent": f"FitzflixBot/1.0 (mailto:{contact})",
            "Accept": "application/sparql-results+json",
            "Accept-Encoding": "gzip,deflate",
        },
        timeout=120,
    )
    r.raise_for_status()
    return r.json().get("results", {}).get("bindings", [])


def _award_rows(bindings, movie_ids_by_ext):
    """SPARQL bindings as {movie_id: {(award_id, name, win, year), ...}}.

    Deduplicated with a set per film: Wikidata sometimes carries the
    same award statement more than once (with and without a date
    qualifier on re-imports), and the unique constraint treats NULL
    years as distinct, so the dedupe has to happen here.
    """

    rows = {}
    for binding in bindings:
        ext = binding.get("ext", {}).get("value")
        movie_id = movie_ids_by_ext.get(ext)
        if movie_id is None:
            continue
        award_uri = binding.get("award", {}).get("value", "")
        award_id = award_uri.rsplit("/", 1)[-1]
        if not award_id.startswith("Q"):
            continue
        name = (binding.get("awardLabel", {}).get("value") or "").strip()
        # A label service miss echoes the QID back; a bare QID badge
        # tells the user nothing, so those rows drop
        if not name or name == award_id:
            continue
        win = binding.get("kind", {}).get("value") == "win"
        year = binding.get("year", {}).get("value", "")
        year = int(year) if year.isdigit() else None
        rows.setdefault(movie_id, set()).add((award_id, name, win, year))
    return rows


def _replace_awards(batch_movie_ids, rows):
    """Replace the batch's stored awards with the fresh rows.

    Every film in the batch is cleared, not just films with results:
    an empty result means Wikidata lists nothing for it now, and stale
    rows would linger forever otherwise (current-truth semantics).
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
    """Refresh every film's award rows from Wikidata, in polite batches.

    Films with an IMDb id match through P345; the remainder fall back
    to their TMDb id through P4947. A failed batch logs and moves on —
    the weekly cadence self-heals partial refreshes.
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
    for id_prop, mapping in (("P345", by_imdb), ("P4947", by_tmdb)):
        ext_ids = sorted(mapping)
        for start in range(0, len(ext_ids), AWARDS_BATCH_SIZE):
            batch = ext_ids[start : start + AWARDS_BATCH_SIZE]
            values = " ".join(f'"{ext}"' for ext in batch)
            try:
                bindings = _wikidata_sparql(
                    AWARDS_QUERY.format(values=values, id_prop=id_prop)
                )
            except Exception:
                current_app.logger.warning(traceback.format_exc())
                current_app.logger.warning(
                    f"Awards batch failed ({id_prop}, {len(batch)} ids), moving on"
                )
                continue
            rows = _award_rows(bindings, mapping)
            _replace_awards([mapping[ext] for ext in batch], rows)
            films += len(batch)
            awarded += len(rows)
            time.sleep(AWARDS_BATCH_PAUSE_SECONDS)

    return f"Refreshed awards for {films} films, {awarded} with award records"


def refresh_awards():
    """Weekly task wrapper: refresh all film awards inside an app context."""

    with app.app_context():
        result = refresh_movie_awards()
        current_app.logger.info(result)
        return result
