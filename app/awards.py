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

Two passes fill the movie_award table. The film pass reads the award
statements from the film items. But Wikidata records the craft
categories (Best Director, Best Actor, Best Cinematography, and more)
on PERSON items. Those statements have a "for work" (P1686) qualifier
that names the film. For example, the item of On the Waterfront knows
the Best Picture win. The item of Kazan holds the Best Director win.
Thus, a second pass finds each award statement whose for-work
qualifier names a library film. The item that holds the statement is
not important. The second pass attributes the award to the film. The
query must start from the film side. The qualifier lookup that starts
from a bound film resolves a batch of 200 films in less than 1 second.
A sweep of the 61,000 credited people of the library made WDQS time
out on each batch. A person award without a for-work qualifier (a
career honor, a knighthood) never matches.
"""

import time
import traceback

import requests

from flask import current_app
from werkzeug.local import LocalProxy

from app import db, get_app
from app.models import Movie, MovieAward

# This is the app instance of this process. Fitzflix resolves it lazily.
# Thus, a process that already has an application can import this module
# and not build a second application.

app = LocalProxy(get_app)

# The VALUES batches keep each request small. The pause between requests
# keeps the crawl polite. The whole library refreshes in about 20
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

# The craft pass starts from the FILM. It walks backwards into each
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


def _wikidata_sparql(query):
    """Run one SPARQL query against Wikidata, per its access guidelines.

    A 429 response means that WDQS throttled this client. This function
    obeys the Retry-After header of the response, with 1 retry and a
    cap on the delay. Then the error propagates to the failure handling
    of the batch loop.
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
    remove the duplicates.
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


def _merge_person_awards(rows):
    """Insert the person-derived rows that a film does not have yet.

    This MERGES, unlike the film pass. The same ceremony event can
    correctly appear on the two items. For example, the item of a film
    sometimes lists its Best Director win too. Thus, this skips a row
    if the film already has that award with the same kind (win or
    nomination) and a matching year. A missing year on one side counts
    as a match, because a film item without the date qualifier still
    means the same event. A win goes in if only the nomination is on
    record, because the win is new information. Return (films,
    inserted).
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
    """Backfill the for-work craft awards, in the batches of the film pass.

    This uses the same id maps as refresh_movie_awards: IMDb first, then
    TMDB as the fallback. But the query walks from each film into the
    award statements that name the film as their "for work". These are
    the craft categories that the person items hold. The weekly task
    runs this AFTER refresh_movie_awards. The film pass replaces all the
    rows. Thus, this pass rebuilds the craft rows on top of each new
    baseline, and the merge removes the duplicates. A standalone run is
    idempotent. It skips the rows that are already stored.
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
    """Refresh the award rows of each film from Wikidata, in polite batches.

    A film with an IMDb id matches through P345. The other films match
    through their TMDB id and P4947. A failed batch logs a warning, and
    the loop continues. The weekly run repairs a partial refresh.
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
    """Run the two award passes inside an app context, as the weekly task.

    The order is important. The film pass deletes and rebuilds the rows
    of each film. Then the person pass adds the craft categories on top.
    """

    with app.app_context():
        film_result = refresh_movie_awards()
        current_app.logger.info(film_result)
        person_result = refresh_person_awards()
        current_app.logger.info(person_result)
        return f"{film_result}; {person_result}"
