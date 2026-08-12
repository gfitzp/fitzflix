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

Two passes fill the movie_award table. The film pass reads award
statements off film items, but Wikidata records craft categories
(Best Director, Best Actor, Best Cinematography…) on PERSON items
with a "for work" (P1686) qualifier naming the film — On the
Waterfront's item knows Best Picture while Kazan's item holds the
Best Director win — so a second pass reads the library's credited
people (matched by TMDb person id, P4985) and attributes their
for-work awards back to the films. Person awards without a for-work
qualifier (career honors, knighthoods) are ignored.
"""

import time
import traceback

import requests

from flask import current_app
from werkzeug.local import LocalProxy

from app import db, get_app
from app.models import Movie, MovieAward, MovieCast, MovieCrew
from app.recommendations import CREW_ROLE_JOBS

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

# The person pass matches people by TMDb person id (P4985) and only
# keeps statements carrying a "for work" qualifier; the work's own
# external ids come back in the same query so no second lookup runs

PERSON_AWARDS_QUERY = """
SELECT ?award ?awardLabel ?kind (YEAR(?when) AS ?year) ?workImdb ?workTmdb WHERE {{
  VALUES ?ext {{ {values} }}
  ?person wdt:P4985 ?ext .
  {{ ?person p:P166 ?statement . ?statement ps:P166 ?award . BIND("win" AS ?kind) }}
  UNION
  {{ ?person p:P1411 ?statement . ?statement ps:P1411 ?award . BIND("nomination" AS ?kind) }}
  ?statement pq:P1686 ?work .
  OPTIONAL {{ ?statement pq:P585 ?when . }}
  OPTIONAL {{ ?work wdt:P345 ?workImdb . }}
  OPTIONAL {{ ?work wdt:P4947 ?workTmdb . }}
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


def _parse_award(binding):
    """One binding as (award_id, name, win, year), or None if unusable.

    A label service miss echoes the QID back as the label; a bare QID
    badge tells the user nothing, so those rows drop.
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
        parsed = _parse_award(binding)
        if parsed is None:
            continue
        rows.setdefault(movie_id, set()).add(parsed)
    return rows


def _person_award_rows(bindings, work_by_imdb, work_by_tmdb):
    """Person-pass bindings as {movie_id: {(award_id, name, win, year)}}.

    Attribution goes to the for-work FILM, not the person: the work
    resolves to a library movie through its IMDb id first, TMDb id as
    fallback (the film pass's matching order). Works outside the
    library — other films, TV — simply don't resolve and drop. The
    same statement can arrive once per honored person (two writers
    sharing a screenplay award), so the per-film set dedupe matters
    here even more than in the film pass.
    """

    rows = {}
    for binding in bindings:
        movie_id = work_by_imdb.get(binding.get("workImdb", {}).get("value"))
        if movie_id is None:
            movie_id = work_by_tmdb.get(binding.get("workTmdb", {}).get("value"))
        if movie_id is None:
            continue
        parsed = _parse_award(binding)
        if parsed is None:
            continue
        rows.setdefault(movie_id, set()).add(parsed)
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


def _merge_person_awards(rows):
    """Insert person-derived rows a film doesn't already carry.

    Unlike the film pass this MERGES: the same ceremony event can
    legitimately appear on both items (a film's own item sometimes
    lists its Best Director win too), so a row is skipped when the
    film already has that award with the same win/nomination kind and
    a matching year — where a missing year on either side counts as a
    match, since a film item lacking the date qualifier still means
    the same event. A win still lands when only the nomination is on
    record: winning is new information. Returns (films, inserted).
    """

    existing = {}
    for row in MovieAward.query.filter(MovieAward.movie_id.in_(list(rows))):
        existing.setdefault(row.movie_id, {}).setdefault(
            (row.award_id, row.win), set()
        ).add(row.year)

    films = set()
    inserted = 0
    for movie_id, entries in rows.items():
        stored = existing.setdefault(movie_id, {})
        for award_id, name, win, year in entries:
            years = stored.get((award_id, win))
            if years is not None and (year in years or None in years or year is None):
                continue
            db.session.add(
                MovieAward(
                    movie_id=movie_id,
                    award_id=award_id,
                    award_name=name[:512],
                    win=win,
                    year=year,
                )
            )
            stored.setdefault((award_id, win), set()).add(year)
            films.add(movie_id)
            inserted += 1
    db.session.commit()
    return films, inserted


def _credited_person_ids():
    """TMDb person ids with library credits: credited cast + key crew.

    The cast side follows the /people page's convention — people whose
    only roles are "(uncredited)" don't count — and the crew side is
    restricted to the taste profile's key roles (director, writer,
    cinematographer, composer, editor); grips and gaffers carry no
    craft categories worth a 300-batch crawl.
    """

    key_jobs = [job for jobs, _ in CREW_ROLE_JOBS.values() for job in jobs]
    cast = (
        db.session.query(MovieCast.credit_id)
        .filter(MovieCast.credit_id.isnot(None))
        .filter(
            db.or_(
                MovieCast.character.is_(None),
                db.not_(MovieCast.character.like("%(uncredited)%")),
            )
        )
        .distinct()
    )
    crew = (
        db.session.query(MovieCrew.credit_id)
        .filter(MovieCrew.credit_id.isnot(None))
        .filter(MovieCrew.job.in_(key_jobs))
        .distinct()
    )
    return {row[0] for row in cast} | {row[0] for row in crew}


def refresh_person_awards():
    """Backfill craft awards from person items, in polite batches.

    Runs AFTER refresh_movie_awards in the weekly task: the film pass
    replaces rows wholesale, so the person-derived rows are rebuilt on
    top of each fresh baseline (and the merge dedupes against it).
    Standalone runs are idempotent — already-stored rows just skip.
    """

    if not current_app.config["WIKIDATA_SPARQL_URL"]:
        return "WIKIDATA_SPARQL_URL is not configured, skipping awards refresh"

    # Full work maps, unlike the film pass's either/or split: a film
    # queried by IMDb id must still resolve when the work item only
    # carries its TMDb id, and vice versa

    work_by_imdb = {
        imdb_id: movie_id
        for movie_id, imdb_id in db.session.query(Movie.id, Movie.imdb_id).filter(
            Movie.imdb_id.isnot(None), Movie.imdb_id != ""
        )
    }
    work_by_tmdb = {
        str(tmdb_id): movie_id
        for movie_id, tmdb_id in db.session.query(Movie.id, Movie.tmdb_id).filter(
            Movie.tmdb_id.isnot(None)
        )
    }

    person_ids = sorted(_credited_person_ids())
    films = set()
    inserted = 0
    for start in range(0, len(person_ids), AWARDS_BATCH_SIZE):
        batch = person_ids[start : start + AWARDS_BATCH_SIZE]
        values = " ".join(f'"{ext}"' for ext in batch)
        try:
            bindings = _wikidata_sparql(PERSON_AWARDS_QUERY.format(values=values))
        except Exception:
            current_app.logger.warning(traceback.format_exc())
            current_app.logger.warning(
                f"Person-awards batch failed ({len(batch)} ids), moving on"
            )
            continue
        rows = _person_award_rows(bindings, work_by_imdb, work_by_tmdb)
        batch_films, batch_inserted = _merge_person_awards(rows)
        films |= batch_films
        inserted += batch_inserted
        time.sleep(AWARDS_BATCH_PAUSE_SECONDS)

    return (
        f"Scanned {len(person_ids)} credited people, "
        f"added {inserted} craft award records for {len(films)} films"
    )


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
    """Weekly task wrapper: both award passes inside an app context.

    Order matters — the film pass wipes and rebuilds each film's rows,
    the person pass then layers the craft categories back on top.
    """

    with app.app_context():
        film_result = refresh_movie_awards()
        current_app.logger.info(film_result)
        person_result = refresh_person_awards()
        current_app.logger.info(person_result)
        return f"{film_result}; {person_result}"
