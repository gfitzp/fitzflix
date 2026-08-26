"""Film awards from Wikidata.

Award wins (P166) and nominations (P1411) for the library's films,
matched through the IMDb (P345) or TMDB (P4947) ids Fitzflix already
stores — Wikidata carries both. Wikidata is the sanctioned source:
TMDB has no awards API, IMDb's is paid-license only. Access follows
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
Best Director win — so a second pass finds every award statement
whose for-work qualifier names a library film, whoever's item holds
it, and attributes it to the film. Querying from the film side
matters: the qualifier lookup anchored on a bound film resolves a
200-film batch in under a second, where sweeping the library's
61,000 credited people timed WDQS out on every batch. Person awards
without a for-work qualifier (career honors, knighthoods) never
match.
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

# WDQS allows a client 30 error queries per minute before throttling
# escalates toward a ban: failed batches wait longer than good ones,
# and a run of consecutive failures means the service itself is having
# a bad day — stop and let the weekly cadence self-heal

AWARDS_ERROR_PAUSE_SECONDS = 10.0
AWARDS_MAX_CONSECUTIVE_FAILURES = 5

# One query shape serves both id systems: {id_prop} is the Wikidata
# property the external ids match against (P345 = IMDb, P4947 = TMDB)

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

# The craft pass anchors on the FILM and walks backwards into any
# award statement whose "for work" qualifier names it — the holder's
# item (usually a person's) never needs naming, so the query stays
# selective enough for WDQS's 60-second limit

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


def _wikidata_sparql(query):
    """Run one SPARQL query against Wikidata, per its access guidelines.

    A 429 means WDQS throttled this client; its Retry-After header is
    honored (one retry, capped) before the error propagates to the
    batch loop's own failure handling.
    """

    from app.videos import wikidata_retry_after_seconds

    contact = current_app.config["SERVER_EMAIL"] or "fitzflix"
    headers = {
        "User-Agent": f"FitzflixBot/1.0 (mailto:{contact})",
        "Accept": "application/sparql-results+json",
        "Accept-Encoding": "gzip,deflate",
    }
    for attempt in range(2):
        r = requests.get(
            current_app.config["WIKIDATA_SPARQL_URL"],
            params={"query": query},
            headers=headers,
            timeout=120,
        )
        if getattr(r, "status_code", None) == 429 and attempt == 0:
            delay = wikidata_retry_after_seconds(r)
            current_app.logger.warning(
                f"Wikidata throttled the awards query (429); retrying in {delay}s"
            )
            time.sleep(delay)
            continue
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


def refresh_person_awards():
    """Backfill for-work craft awards, in the film pass's own batches.

    The same IMDb-first/TMDB-fallback id maps as refresh_movie_awards,
    but the query walks from each film into award statements that name
    it as their "for work" — the craft categories person items hold.
    Runs AFTER refresh_movie_awards in the weekly task: the film pass
    replaces rows wholesale, so the craft rows are rebuilt on top of
    each fresh baseline (and the merge dedupes against it). Standalone
    runs are idempotent — already-stored rows just skip.
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
    touched = set()
    inserted = 0
    consecutive_failures = 0
    for id_prop, mapping in (("P345", by_imdb), ("P4947", by_tmdb)):
        ext_ids = sorted(mapping)
        for start in range(0, len(ext_ids), AWARDS_BATCH_SIZE):
            batch = ext_ids[start : start + AWARDS_BATCH_SIZE]
            values = " ".join(f'"{ext}"' for ext in batch)
            try:
                bindings = _wikidata_sparql(
                    CRAFT_AWARDS_QUERY.format(values=values, id_prop=id_prop)
                )
            except Exception:
                current_app.logger.warning(traceback.format_exc())
                current_app.logger.warning(
                    f"Craft-awards batch failed ({id_prop}, {len(batch)} ids), "
                    f"moving on"
                )
                consecutive_failures += 1
                if consecutive_failures >= AWARDS_MAX_CONSECUTIVE_FAILURES:
                    current_app.logger.warning(
                        f"Craft-awards refresh aborted after "
                        f"{consecutive_failures} consecutive failed batches — "
                        f"Wikidata is having a bad day; the weekly run will "
                        f"pick it back up"
                    )
                    return (
                        f"Craft-awards refresh aborted after "
                        f"{consecutive_failures} consecutive failures; "
                        f"scanned {films} films, added {inserted} records"
                    )
                time.sleep(AWARDS_ERROR_PAUSE_SECONDS)
                continue
            consecutive_failures = 0
            rows = _award_rows(bindings, mapping)
            batch_films, batch_inserted = _merge_person_awards(rows)
            films += len(batch)
            touched |= batch_films
            inserted += batch_inserted
            time.sleep(AWARDS_BATCH_PAUSE_SECONDS)

    return (
        f"Scanned {films} films for craft awards, "
        f"added {inserted} records for {len(touched)} films"
    )


def refresh_movie_awards():
    """Refresh every film's award rows from Wikidata, in polite batches.

    Films with an IMDb id match through P345; the remainder fall back
    to their TMDB id through P4947. A failed batch logs and moves on —
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
    consecutive_failures = 0
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
