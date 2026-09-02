"""Keep the spine catalog of the Criterion Collection.

This module is a strangler split from app.videos, made in the
videos/routes split. Wikidata is the source. Two SPARQL queries, one
for individual releases and one for collector sets, merge into one
cached release list keyed by spine. The monthly refresh task applies
the list to the library. It marks the releases of owned films. It
creates file-less catalog records for spines that the library does not
have. It keeps the inventory of /library/criterion-collection current.

app.videos exports every name in this module again. Thus, stored rq
job strings ("app.videos.refresh_criterion_collection_info") and
import sites continue to resolve. The record-creation path uses the
TMDB helpers of app.videos through lazy imports. Thus, the module
import direction stays one-way.
"""

import json
import time
import traceback

import requests

from flask import current_app
from werkzeug.local import LocalProxy

from app import db, get_app
from app.models import CatalogExclusion, Movie

# Wikidata models Criterion spine numbers as property P12279, TMDB movie
# ids as P4947, and publication dates as P577. The query takes the
# earliest publication year, because a film has one date per release.

CRITERION_SPARQL_QUERY = """
SELECT ?spine ?tmdbId ?filmLabel
       (MIN(YEAR(?date)) AS ?year)
       (SAMPLE(?criterionId) AS ?criterionId) WHERE {
  ?film wdt:P12279 ?spine .
  OPTIONAL { ?film wdt:P4947 ?tmdbId . }
  OPTIONAL { ?film wdt:P9584 ?criterionId . }
  OPTIONAL { ?film wdt:P577 ?date . }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
GROUP BY ?spine ?film ?filmLabel ?tmdbId
"""


# A box set has the spine number on the Wikidata item of the set itself.
# The member films are linked through P527 ("has part"). This query maps
# each member to the spine and the title of its set.

CRITERION_SETS_SPARQL_QUERY = """
SELECT ?spine ?setLabel ?tmdbId ?filmLabel
       (MIN(YEAR(?date)) AS ?year)
       (SAMPLE(?criterionId) AS ?criterionId) WHERE {
  ?set wdt:P12279 ?spine .
  ?set wdt:P527 ?film .
  OPTIONAL { ?film wdt:P4947 ?tmdbId . }
  OPTIONAL { ?film wdt:P9584 ?criterionId . }
  OPTIONAL { ?film wdt:P577 ?date . }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
GROUP BY ?spine ?set ?setLabel ?film ?filmLabel ?tmdbId
"""


CRITERION_CACHE_KEY = "fitzflix:criterion:releases"


CRITERION_CACHE_SECONDS = 7 * 86400


def wikidata_retry_after_seconds(response, default=60, cap=300):
    """Return the seconds to wait after a WDQS 429, from its Retry-After header.

    The header can be absent or can have the shape of an HTTP date. In
    both cases this function returns the default. The cap prevents a
    strange header from stalling a worker.
    """

    try:
        seconds = int(response.headers.get("Retry-After", ""))
    except (TypeError, ValueError):
        seconds = default
    return max(1, min(seconds, cap))


def _wikidata_sparql(url, query):
    """Run one SPARQL query against Wikidata, per its access guidelines.

    WDQS throttles a client with 429 and Retry-After when the client
    exceeds its processing budget. This function obeys the header with
    1 retry, capped. Thus, WDQS keeps polite clients off the
    temporary-ban list.
    """

    contact = current_app.config["SERVER_EMAIL"] or "fitzflix"
    headers = {
        "User-Agent": f"FitzflixBot/1.0 (mailto:{contact})",
        "Accept": "application/sparql-results+json",
        "Accept-Encoding": "gzip,deflate",
    }
    for attempt in range(2):
        r = requests.get(url, params={"query": query}, headers=headers, timeout=60)
        if getattr(r, "status_code", None) == 429 and attempt == 0:
            delay = wikidata_retry_after_seconds(r)
            current_app.logger.warning(
                f"Wikidata throttled the query (429); retrying in {delay}s"
            )
            time.sleep(delay)
            continue
        r.raise_for_status()
        return r.json().get("results", {}).get("bindings", [])


def _parse_criterion_binding(binding):
    """Return a SPARQL result row as a release dict, or None if the spine is bad."""

    spine = binding.get("spine", {}).get("value", "")
    if not spine.isdigit():
        return None

    tmdb_id = binding.get("tmdbId", {}).get("value", "")
    title = (binding.get("filmLabel", {}).get("value") or "").strip()
    year = binding.get("year", {}).get("value", "")

    # "title" is uppercase for matching. "label" keeps the casing of
    # Wikidata for display. The catalog page shows the releases that the
    # library has no record for. Cached payloads from before Fitzflix
    # stored the label do not have the key. Thus, readers must fall back
    # to "title".

    return {
        "spine_number": int(spine),
        "tmdb_id": int(tmdb_id) if tmdb_id.isdigit() else None,
        "title": title.upper(),
        "label": title,
        "year": int(year) if year.isdigit() else None,
        "criterion_film_id": binding.get("criterionId", {}).get("value") or None,
        "set_title": None,
    }


def get_criterion_collection_from_wikidata(force_refresh=False):
    """Fetch the Criterion Collection spine numbers from Wikidata.

    Access obeys the data-access guidelines of Wikidata. The client sends
    a descriptive User-Agent with a contact address. It sends 1 SPARQL
    query with a narrow scope. Redis caches the results for 1 week. Thus,
    the per-import lookups never query the endpoint again. The monthly
    scheduled refresh forces a new fetch.
    """

    url = current_app.config["WIKIDATA_SPARQL_URL"]
    if not url:
        return []

    if not force_refresh:
        cached = current_app.redis.get(CRITERION_CACHE_KEY)
        if cached:
            return json.loads(cached)

    criterion_collection = []
    for binding in _wikidata_sparql(url, CRITERION_SPARQL_QUERY):
        release = _parse_criterion_binding(binding)
        if release:
            criterion_collection.append(release)

    # The standalone releases come first. Thus, a film that has its own
    # release and also a set membership keeps its own spine. The matching
    # lookups keep the first entry per film.

    for binding in _wikidata_sparql(url, CRITERION_SETS_SPARQL_QUERY):
        release = _parse_criterion_binding(binding)
        if release:
            release["set_title"] = (
                binding.get("setLabel", {}).get("value") or ""
            ).strip() or None
            criterion_collection.append(release)

    current_app.redis.set(
        CRITERION_CACHE_KEY,
        json.dumps(criterion_collection),
        ex=CRITERION_CACHE_SECONDS,
    )
    current_app.logger.info(
        f"Fetched {len(criterion_collection)} Criterion Collection releases "
        f"from Wikidata"
    )
    return criterion_collection


def criterion_release_lookups(criterion_collection):
    """Index Criterion releases by TMDB id and by (title, year)."""

    by_tmdb_id = {}
    by_title_year = {}
    for release in criterion_collection:
        if release.get("tmdb_id"):
            by_tmdb_id.setdefault(release["tmdb_id"], release)
        if release.get("title") and release.get("year"):
            by_title_year.setdefault((release["title"], release["year"]), release)
    return by_tmdb_id, by_title_year


def assign_criterion_release(movie, by_tmdb_id, by_title_year):
    """Record the Criterion spine number of a movie if a release matches.

    A TMDB id match is exact. Title and year are the fallback for the
    movies that have no TMDB match yet. A box-set member gets the spine
    and the title of its set. Wikidata does not model the in-print
    status. Thus, this function keeps the existing values and gives new
    matches optimistic defaults. It never overwrites a hand-curated set
    title.
    """

    release = by_tmdb_id.get(movie.tmdb_id) if movie.tmdb_id else None
    if release is None and movie.title and movie.year:
        release = by_title_year.get((movie.title.upper(), movie.year))
    if release is None:
        return False

    movie.criterion_spine_number = release["spine_number"]
    if release.get("criterion_film_id"):
        movie.criterion_film_id = release["criterion_film_id"]
    if release.get("set_title") and movie.criterion_set_title == None:
        movie.criterion_set_title = release["set_title"]
    if movie.criterion_in_print == None:
        movie.criterion_in_print = True
    if movie.criterion_disc_owned == None:
        movie.criterion_disc_owned = False

    current_app.logger.info(
        f"{movie} Assigning Criterion Collection "
        f"spine #{movie.criterion_spine_number}"
    )
    return True


def create_criterion_catalog_records(criterion_collection, by_tmdb_id, by_title_year):
    """Create file-less Movie records for the spine releases that the
    library has no record of. Thus, the whole Criterion catalog is
    first-class.

    The prestige of Criterion makes every spine film worth a permanent
    record. A durable record keeps its poster, overview, genres, and
    awards in the database instead of a cache of 1 week (decided by
    Glenn, 2026-08). This function creates the records under the
    Wikidata label. The enqueued TMDB refresh renames each record to the
    canonical title and year of TMDB. That is the standard behavior of
    tmdb_movie_apply. Thus, a later import of the film matches the
    record by title and year and attaches its files. It does not create
    a duplicate. This function skips the releases without a year,
    because title and year are the identity of a record. It returns the
    number of records created. The internal commit runs before this
    function returns. Thus, the enqueued refreshes can see their rows.
    """

    # The TMDB record functions stay in app.videos. The import is lazy.
    # Thus, the module import direction stays one-way.

    from app.videos import find_or_create_tmdb_movie

    tmdb_ids = [
        release["tmdb_id"] for release in criterion_collection if release.get("tmdb_id")
    ]
    existing = {
        tmdb_id
        for (tmdb_id,) in db.session.query(Movie.tmdb_id).filter(
            Movie.tmdb_id.in_(tmdb_ids or [0])
        )
    }
    # This function never creates a hand-excluded id again. See
    # CatalogExclusion. An example is Wikidata junk, such as an
    # unfinished film with a stale TMDB id.
    excluded = {tmdb_id for (tmdb_id,) in db.session.query(CatalogExclusion.tmdb_id)}
    to_refresh = []
    created_count = 0
    for release in criterion_collection:
        tmdb_id = release.get("tmdb_id")
        if not tmdb_id or tmdb_id in existing or not release.get("year"):
            continue
        if tmdb_id in excluded:
            continue
        existing.add(tmdb_id)
        title = release.get("label") or release.get("title")
        movie, created = find_or_create_tmdb_movie(tmdb_id, title, release["year"])
        assign_criterion_release(movie, by_tmdb_id, by_title_year)
        created_count += created
        # An adopted title and year record, with the TMDB id attached
        # now, needs the refresh as much as a new record. Every record
        # that has no stamp needs it.
        if movie.tmdb_data_as_of is None:
            to_refresh.append(movie)
    db.session.commit()
    for movie in to_refresh:
        current_app.maintenance_queue.enqueue(
            "app.videos.refresh_tmdb_info",
            args=("Movies", movie.id, movie.tmdb_id),
            job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
            description=(f"Refreshing TMDB data for '{movie.title} ({movie.year})'"),
        )
    return created_count


def refresh_criterion_collection_info(movie_id=None):
    """Refresh the Criterion Collection information from Wikidata.

    This task runs monthly on the 18th. Criterion announces the new
    titles of each month at approximately the 15th. Some days give
    Wikidata time to catch up. A full refresh forces a new fetch. A
    single-movie refresh uses the cache of 1 week. A full refresh also
    creates records for the spine releases that the library has never
    seen. Thus, new announcements join the catalog as first-class films
    automatically.
    """

    with app.app_context():
        try:

            # If the user specified one movie, update the Criterion
            # Collection info for only that movie. If not, update all.

            if movie_id:
                movies = Movie.query.filter_by(id=movie_id).all()

            else:
                movies = Movie.query.all()

            criterion_collection = get_criterion_collection_from_wikidata(
                force_refresh=movie_id is None
            )
            by_tmdb_id, by_title_year = criterion_release_lookups(criterion_collection)

            matched = 0
            for movie in movies:
                if assign_criterion_release(movie, by_tmdb_id, by_title_year):
                    matched += 1

            created = 0
            if movie_id is None:
                created = create_criterion_catalog_records(
                    criterion_collection, by_tmdb_id, by_title_year
                )

            db.session.commit()
            current_app.logger.info(
                f"Matched {matched} of {len(movies)} movie(s) against "
                f"{len(criterion_collection)} Criterion Collection releases, "
                f"created {created} catalog record(s)"
            )

        except Exception:
            current_app.logger.error(traceback.format_exc())
            db.session.rollback()

        else:
            return True


# This is the app instance of this process. Fitzflix resolves it lazily.
# Thus, an import of this module from a process that already has an
# application does not build a second one.

app = LocalProxy(get_app)
