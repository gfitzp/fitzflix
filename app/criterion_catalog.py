"""The Criterion Collection spine catalog (the videos/routes split's strangler split from
app.videos).

Wikidata is the source: two SPARQL queries (individual releases and
collector's sets) merge into a day-cached release list keyed by spine,
which the weekly refresh task applies to the library — marking owned
films' releases, creating file-less catalog records for spines the
library lacks, and keeping /library/criterion-collection's inventory
current.

app.videos re-exports every name here, so stored rq job strings
("app.videos.refresh_criterion_collection_info") and import sites keep
resolving; the record-creation path leans on app.videos' TMDb helpers
via lazy imports, keeping the module import direction one-way.
"""

import json
import time
import traceback

import requests

from flask import current_app
from werkzeug.local import LocalProxy

from app import db, get_app
from app.models import CatalogExclusion, Movie

# Wikidata models Criterion spine numbers as property P12279, TMDb movie ids
# as P4947, and publication dates as P577; the earliest publication year is
# taken since a film carries one date per release

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


# Box sets carry the spine number on the set's own Wikidata item, with the
# member films linked via P527 ("has part"): map each member to its set's
# spine and title

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
    """Seconds to wait out a WDQS 429, from its Retry-After header.

    The header may be absent or HTTP-date-shaped; both fall back to the
    default, and the cap keeps a strange header from stalling a worker.
    """

    try:
        seconds = int(response.headers.get("Retry-After", ""))
    except (TypeError, ValueError):
        seconds = default
    return max(1, min(seconds, cap))


def _wikidata_sparql(url, query):
    """Run one SPARQL query against Wikidata, per its access guidelines.

    WDQS throttles with 429 + Retry-After when a client outruns its
    processing budget; honoring the header (one retry, capped) is what
    keeps polite clients off the temporary-ban list.
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
    """A SPARQL result row as a release dict, or None if the spine is bad."""

    spine = binding.get("spine", {}).get("value", "")
    if not spine.isdigit():
        return None

    tmdb_id = binding.get("tmdbId", {}).get("value", "")
    title = (binding.get("filmLabel", {}).get("value") or "").strip()
    year = binding.get("year", {}).get("value", "")

    # "title" is uppercased for matching; "label" keeps Wikidata's own
    # casing for display (the catalog page shows releases the library
    # has no record for). Cached payloads from before the label was
    # stored simply lack the key, so readers must fall back to "title"

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
    """Fetch Criterion Collection spine numbers from Wikidata.

    Access follows Wikidata's data-access guidelines: a descriptive
    User-Agent with a contact address, a single narrowly-scoped SPARQL
    query, and results cached in Redis for a week so per-import lookups
    never re-query the endpoint. The monthly scheduled refresh forces a
    fresh fetch.
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

    # Standalone releases come first, so a film that has both its own
    # release and a set membership keeps its own spine — the matching
    # lookups keep the first entry per film

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
    """Index Criterion releases by TMDb id and by (title, year)."""

    by_tmdb_id = {}
    by_title_year = {}
    for release in criterion_collection:
        if release.get("tmdb_id"):
            by_tmdb_id.setdefault(release["tmdb_id"], release)
        if release.get("title") and release.get("year"):
            by_title_year.setdefault((release["title"], release["year"]), release)
    return by_tmdb_id, by_title_year


def assign_criterion_release(movie, by_tmdb_id, by_title_year):
    """Record a movie's Criterion spine number if a release matches.

    TMDb id matches are exact; title and year are the fallback for movies
    that haven't been matched to TMDb yet. Box-set members get their set's
    spine and title. Wikidata doesn't model in-print status, so existing
    values are kept and new matches get optimistic defaults; hand-curated
    set titles are never overwritten.
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
    """Create file-less Movie records for spine releases the library
    has no record of, so the whole Criterion catalog is first-class.

    Criterion's general prestige makes every spine film worth knowing
    about permanently: a durable record keeps its poster, overview,
    genres, and awards in the database instead of a week-long cache
    (Glenn's call, Aug 2026). Records are created under the Wikidata
    label; the enqueued TMDb refresh renames each to TMDb's canonical
    title and year (tmdb_movie_apply's standard behavior), so a later
    import of the film matches the record by title+year and attaches
    its files instead of spawning a duplicate. Year-less releases are
    skipped — title+year is a record's identity. Returns the number of
    records created; the caller commits before this returns via the
    internal commit, so the enqueued refreshes can see their rows.
    """

    # TMDb record plumbing stays in app.videos; lazy so the module
    # import direction stays one-way

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
    # Hand-excluded ids (Wikidata junk like an unfinished film with a
    # stale TMDb id) are never re-created — see CatalogExclusion
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
        # An adopted title+year record (tmdb id just attached) needs the
        # refresh as much as a new one — anything never stamped does
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
    """Refresh Criterion Collection information from Wikidata.

    Runs monthly on the 18th — Criterion announces each month's new titles
    around the 15th, and a few days leaves time for Wikidata to catch up.
    A full refresh forces a fresh fetch; single-movie refreshes use the
    week-long cache. Full refreshes also create records for spine
    releases the library has never seen, so new announcements join the
    catalog as first-class films automatically.
    """

    with app.app_context():
        try:

            # If the user specified a particular movie to be updated, update the
            # Criterion Collection info for just that one movie. Otherwise, update all.

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


# This process's app instance, resolved lazily so importing this module from
# a process that already has an application doesn't build a second one

app = LocalProxy(get_app)
