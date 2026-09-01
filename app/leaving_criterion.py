"""The landing page's "Leaving the Criterion Channel" shelf.

criterionchannel.com/leaving-{month}-{lastday} is the canonical source
for what departs at month's end — structured HTML with a tooltip per
film carrying title, director, and year; no feed or API exists (the
JSON/RSS variants 404, JustWatch has no public leaving API, and TMDB
doesn't license departure data). Scraping this one official page for
title extraction is a narrow, deliberate exception to the no-scraping
rule. A daily task — a no-op while the stored set is still current —
parses the collection, matches each film to TMDB by title and year,
embeds the enriched payloads (so the shelf outlives every shorter
cache), and stores the set with its departure date; the
landing page ranks it against the viewer's taste profile for Criterion
subscribers.

Shelf semantics: owned films are excluded (no urgency — the shelf is
about watch-it-before-it-leaves), diary films are excluded unless
they're on the user's watchlist, and a leaving film on the watchlist
is the strongest signal of all — watch it now, or buy the disc.
"""

import calendar
import html
import json
import re
import traceback
import unicodedata

from datetime import date, datetime

import requests

from flask import current_app, g
from werkzeug.local import LocalProxy

from app import db, get_app
from app.models import File, Movie, UserMovieReview, UserMovieStatus, UserWatchlist
from app.recommendations import score_movie, stored_profile
from app.streaming_rail import _payload_features, enriched_movie
from app.models import tmdb_get

# This process's app instance, resolved lazily so the monthly task can
# run on a worker without building a second application

app = LocalProxy(get_app)

# TMDB's provider id for the Criterion Channel — the shelf only renders
# for users subscribed to it

CRITERION_PROVIDER_ID = 258

LEAVING_KEY = "fitzflix:criterion:leaving"
MATCH_KEY = "fitzflix:criterion:match:{slug}"
MATCH_CACHE_SECONDS = 60 * 86400
MATCH_CANDIDATES = 5
PAGE_CAP = 10

# One film per tooltip: the title heading, then an optional
# "Directed by X • YYYY • Country" line

TOOLTIP_TITLE_RE = re.compile(
    r"tooltip-item-title[^>]*>\s*<strong>\s*(.*?)\s*</strong>", re.S
)
TOOLTIP_META_RE = re.compile(
    r"(?:Directed by\s+(?P<director>[^•<]+?)\s*)?•\s*(?P<year>\d{4})\s*•"
)


def leaving_page_candidates(today):
    """(url, departure date) candidates in likelihood order: this
    month's page, next month's (the new page can appear before the old
    departure passes), then last month's (early in a month, before the
    new page exists)."""

    candidates = []
    for months_ahead in (0, 1, -1):
        month = today.month + months_ahead
        year = today.year
        if month > 12:
            month, year = month - 12, year + 1
        elif month < 1:
            month, year = month + 12, year - 1
        last_day = calendar.monthrange(year, month)[1]
        month_name = calendar.month_name[month].lower()
        candidates.append(
            (
                f"https://www.criterionchannel.com/leaving-{month_name}-{last_day}",
                date(year, month, last_day),
            )
        )
    return candidates


def parse_leaving_page(page_html):
    """[{title, director, year}] from one page of the leaving
    collection's tooltip markup; [] when the page has no films."""

    films = []
    for chunk in page_html.split('class="tooltip background-white"')[1:]:
        title_match = TOOLTIP_TITLE_RE.search(chunk)
        if not title_match:
            continue
        film = {
            "title": html.unescape(title_match.group(1)).strip(),
            "director": None,
            "year": None,
        }
        meta = TOOLTIP_META_RE.search(chunk.replace("&nbsp;", " "))
        if meta:
            film["year"] = int(meta.group("year"))
            if meta.group("director"):
                film["director"] = html.unescape(meta.group("director")).strip()
        films.append(film)
    return films


def fetch_collection_films(url):
    """Deduped [{title, director, year}] scraped from one VHX
    collection page (the ?html=1&page=N markup every Criterion Channel
    collection serves), paginating until an empty page; [] when the
    page doesn't answer. The generic half of the scraper — the leaving
    page and the newly-added feed (#246, app.newly_added) both read
    through it."""

    films = []
    try:
        for page in range(1, PAGE_CAP + 1):
            r = requests.get(url, params={"html": 1, "page": page}, timeout=15)
            if r.status_code != 200:
                break
            page_films = parse_leaving_page(r.text)
            if not page_films:
                break
            films.extend(page_films)
    except Exception:
        current_app.logger.warning(traceback.format_exc())
        return []
    seen = set()
    unique = []
    for film in films:
        key = (film["title"].lower(), film["year"])
        if key not in seen:
            seen.add(key)
            unique.append(film)
    return unique


def fetch_leaving_films():
    """(departure date, source url, films) scraped from the official
    leaving page, paginating until an empty page; (None, None, []) when
    no candidate page answers."""

    for url, departs in leaving_page_candidates(date.today()):
        films = fetch_collection_films(url)
        if films:
            return departs, url, films
    return None, None, []


def _normalize(text):
    """A comparison key for titles and names: accents folded, case
    and punctuation dropped, apostrophes (straight or curly) removed
    outright so "Muriel’s Wedding" meets "Muriel's Wedding" and
    "P. J. Hogan" meets "P.J. Hogan"."""

    text = re.sub(r"[\'’`]", "", text or "")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _tmdb_json(path, params):
    """One TMDB GET's JSON body, or None on any failure (logged)."""

    try:
        r = tmdb_get(
            current_app.config["TMDB_API_URL"] + path,
            params={"api_key": current_app.config["TMDB_API_KEY"], **params},
            timeout=10,
        )
        r.raise_for_status()
        return r.json()
    except Exception:
        current_app.logger.warning(traceback.format_exc())
        return None


def match_tmdb_id(title, year, director=None):
    """The TMDB id for a leaving film, by title-and-year search, cached
    for two months; None when TMDB has no match.

    TMDB's search ranks by popularity, so a generic short-film title
    like "Here", "Kid", or "Ambition" comes back with a popular
    feature first — the Aug 2026 set put "Right Here, Right Now" and
    "The Karate Kid" on the shelf in place of Bas Devos's and Hal
    Hartley's films. So a scraped director is verified against each
    candidate's credits, exact-title candidates are tried first, and
    a film whose director matches nothing TMDB offers stays unmatched
    (a plain "Also leaving" row) rather than becoming the wrong film.
    A candidate with no director credited at all passes on an exact
    title-and-year match — shorts often have no crew on TMDB. Without
    a scraped director, the exact-title candidate wins, else the
    first result (the pre-Aug 2026 behaviour). The cache key carries
    the director, so a director-aware lookup never reads an entry the
    older title-only matcher wrote.
    """

    slug = re.sub(r"[^a-z0-9]+", "-", f"{title}-{year}-{director or ''}".lower()).strip(
        "-"
    )
    cache_key = MATCH_KEY.format(slug=slug)
    cached = current_app.redis.get(cache_key)
    if cached:
        stored = json.loads(cached)
        return stored or None

    wanted_title = _normalize(title)
    wanted_director = _normalize(director) if director else None

    def candidates(params, pages):
        """Search results worth a look, exact-title matches first, then
        releases within a year of the scraped date (Criterion and TMDB
        often disagree by one — festival year versus release year),
        capped at the handful worth a credits lookup. With a director
        to verify, only those two kinds are candidates at all: the
        year-less fallback reads two pages, and a popular stranger
        shouldn't cost a credits call."""

        results = []
        for page in range(1, pages + 1):
            body = _tmdb_json("/search/movie", {"query": title, "page": page, **params})
            page_results = (body or {}).get("results") or []
            results.extend(page_results)
            if len(page_results) < 20:
                break

        def rank(result):
            exact = _normalize(result.get("title")) == wanted_title
            released = (result.get("release_date") or "")[:4]
            near = (
                bool(year)
                and released.isdigit()
                and abs(int(released) - int(year)) <= 1
            )
            return (not exact, not near)

        ordered = sorted(results, key=rank)
        if wanted_director:
            ordered = [result for result in ordered if rank(result) != (True, True)]
        return ordered[:MATCH_CANDIDATES]

    def directed_by(result):
        """True when the candidate's credited directors include the
        scraped one — or when nothing is credited and the title and
        year agree exactly."""

        body = _tmdb_json(f"/movie/{result['id']}/credits", {})
        if body is None:
            return False
        directors = [
            _normalize(person.get("name"))
            for person in body.get("crew") or []
            if person.get("job") == "Director"
        ]
        if not directors:
            return _normalize(result.get("title")) == wanted_title and (
                result.get("release_date") or ""
            )[:4] == str(year)
        return any(
            name and (name in wanted_director or wanted_director in name)
            for name in directors
        )

    def pick(params):
        """The chosen candidate id for one search, or None. When no
        candidate's director corroborates, the one result TMDB knows
        by exactly this title and year still passes — Criterion and
        TMDB can credit a film differently (Criterion files "Regarding
        Soon" under Hal Hartley, its subject; TMDB under Richard
        Sylvarnes, who shot and cut it) — but only when it's unique,
        so a same-title stranger from the same year can't slip in."""

        found = candidates(params, pages=1 if params else 2)
        if wanted_director:
            for result in found:
                if directed_by(result):
                    return result.get("id")
            exact = [
                result
                for result in found
                if _normalize(result.get("title")) == wanted_title
                and (result.get("release_date") or "")[:4] == str(year)
            ]
            if len(exact) == 1:
                current_app.logger.info(
                    f"Leaving-Criterion: '{title}' ({year}) matched TMDB "
                    f"{exact[0].get('id')} by exact title and year; TMDB "
                    f"credits a director other than {director}"
                )
                return exact[0].get("id")
            return None
        return found[0].get("id") if found else None

    tmdb_id = None
    if year:
        tmdb_id = pick({"primary_release_year": year})
    if tmdb_id is None:
        tmdb_id = pick({})
    current_app.redis.set(cache_key, json.dumps(tmdb_id), ex=MATCH_CACHE_SECONDS)
    return tmdb_id


def refresh_leaving_criterion():
    """Daily task: scrape the leaving collection, match each film to
    TMDB, embed the enriched payloads, and store the set with its
    departure date. A no-op while the stored set's departure is still
    ahead — the daily cadence exists to retry until Criterion publishes
    the new month's page, which appears sometime after the old set
    departs, not on a knowable schedule. The stored set has no TTL —
    the shelf simply hides once the departure date passes."""

    with app.app_context():
        stored = current_app.redis.get(LEAVING_KEY)
        if stored and date.fromisoformat(json.loads(stored)["departs"]) >= date.today():
            return True

        departs, source, films = fetch_leaving_films()
        if not films:
            current_app.logger.warning(
                "Leaving-Criterion: no films found on any candidate page"
            )
            return True

        # Films the TMDB matcher can't resolve still make the stored
        # set, carrying just the scraped facts (title, director, year)
        # — the /leaving page lists them as plain rows so the departure
        # inventory stays complete; the home shelf skips them (its
        # cards need posters and taste features)

        items = []
        for film in films:
            tmdb_id = match_tmdb_id(film["title"], film["year"], film["director"])
            payload = enriched_movie(tmdb_id) if tmdb_id is not None else None
            if payload:
                items.append({**payload, "tmdb_id": tmdb_id})
            else:
                items.append({**film, "tmdb_id": None})

        current_app.redis.set(
            LEAVING_KEY,
            json.dumps(
                {
                    "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "departs": departs.isoformat(),
                    "source": source,
                    "items": items,
                }
            ),
        )
        current_app.logger.info(
            f"Leaving-Criterion: stored {len(items)} of {len(films)} films "
            f"departing {departs.isoformat()}"
        )
        return True


def user_film_sets(user, tmdb_ids):
    """(owned, logged, watchlisted, refused) tmdb-id sets for one user
    over the given films — the exclusion inputs the discovery shelves
    share (the leaving shelf here, the newly-added shelves in
    app.newly_added)."""

    owned = {
        tmdb_id
        for (tmdb_id,) in db.session.query(Movie.tmdb_id)
        .filter(Movie.tmdb_id.in_(tmdb_ids))
        .filter(Movie.files.any(File.feature_type_id.is_(None)))
    }
    logged = {
        tmdb_id
        for (tmdb_id,) in db.session.query(Movie.tmdb_id)
        .join(UserMovieReview, UserMovieReview.movie_id == Movie.id)
        .filter(Movie.tmdb_id.in_(tmdb_ids))
        .filter(UserMovieReview.user_id == int(user.id))
    }
    watchlisted = {
        tmdb_id
        for (tmdb_id,) in db.session.query(Movie.tmdb_id)
        .join(UserWatchlist, UserWatchlist.movie_id == Movie.id)
        .filter(Movie.tmdb_id.in_(tmdb_ids))
        .filter(UserWatchlist.user_id == int(user.id))
    }
    refused = {
        tmdb_id
        for (tmdb_id,) in db.session.query(Movie.tmdb_id)
        .join(UserMovieStatus, UserMovieStatus.movie_id == Movie.id)
        .filter(Movie.tmdb_id.in_(tmdb_ids))
        .filter(UserMovieStatus.user_id == int(user.id))
        .filter(UserMovieStatus.kind == "not_interested")
    }
    return owned, logged, watchlisted, refused


def leaving_shelf(user):
    """The taste-ranked departure shelf for one user, or None.

    Renders only for Criterion subscribers with a stored set that
    hasn't departed yet. Owned, diary, and watchlisted films all drop
    out — this is a discovery shelf since Aug 30 2026: a watchlisted
    departure is the watch-it-now-or-buy-it case, and it leads the
    landing page's watchlist shelf instead of pinning here.
    """

    subscribed = {row.provider_id for row in user.streaming_providers}
    if CRITERION_PROVIDER_ID not in subscribed:
        return None
    payload = current_app.redis.get(LEAVING_KEY)
    if not payload:
        return None
    stored = json.loads(payload)
    departs = date.fromisoformat(stored["departs"])
    if departs < date.today():
        return None
    profile = stored_profile(current_app.redis, user.id)
    if not profile:
        return None

    tmdb_ids = [item["tmdb_id"] for item in stored.get("items", []) if item["tmdb_id"]]
    if not tmdb_ids:
        return None
    owned, logged, watchlisted, refused = user_film_sets(user, tmdb_ids)

    items = []
    for item in stored.get("items", []):
        tmdb_id = item["tmdb_id"]
        if tmdb_id is None:
            continue
        if tmdb_id in owned:
            continue
        if tmdb_id in refused:
            continue
        if tmdb_id in logged or tmdb_id in watchlisted:
            continue
        score, contributions = score_movie(_payload_features(item), profile)
        items.append(
            {
                "tmdb_id": tmdb_id,
                "title": item.get("title"),
                "year": item.get("year"),
                "poster_path": item.get("poster_path"),
                "runtime": item.get("runtime"),
                "because": [
                    label
                    for contribution, label in contributions[:3]
                    if contribution > 0
                ],
                "score": round(score, 4),
            }
        )

    items.sort(key=lambda item: item["score"], reverse=True)
    return {"departs": departs, "url": _source_url(stored, departs), "items": items}


def leaving_departure(tmdb_id):
    """The departure date, as "August 31", when the film is in the
    stored leaving set and that date hasn't passed; None otherwise.
    Every Criterion Channel availability badge asks this (#45c's
    popover, the movie page, search results, filmography rows, the
    watchlist), so the set is parsed once per app context and kept
    on flask.g — one Redis read per page, not one per film."""

    if tmdb_id is None:
        return None
    index = getattr(g, "_leaving_criterion_index", None)
    if index is None:
        index = {}
        payload = current_app.redis.get(LEAVING_KEY)
        if payload:
            stored = json.loads(payload)
            departs = date.fromisoformat(stored["departs"])
            if departs >= date.today():
                label = departs.strftime("%B %-d")
                index = {
                    item["tmdb_id"]: label
                    for item in stored.get("items", [])
                    if item.get("tmdb_id")
                }
        g._leaving_criterion_index = index
    return index.get(tmdb_id)


def _source_url(stored, departs):
    """The scraped page's own URL; payloads stored before the source
    key existed reconstruct it from the departure date (the same shape
    the candidate list builds)."""

    return stored.get("source") or (
        "https://www.criterionchannel.com/leaving-"
        f"{calendar.month_name[departs.month].lower()}-{departs.day}"
    )


def leaving_inventory(user):
    """The complete departing set for the /leaving page, or None.

    Unlike the home shelf, nothing is excluded: owned films stay
    listed with their library badge (the relaxing case — the disc is
    on the shelf), seen films stay with their Seen badge, and films
    the TMDB matcher couldn't resolve trail as plain scraped rows so
    the inventory is the whole departure set. Watchlisted films lead,
    then unowned films by taste score, owned films after.
    """

    payload = current_app.redis.get(LEAVING_KEY)
    if not payload:
        return None
    stored = json.loads(payload)
    departs = date.fromisoformat(stored["departs"])
    if departs < date.today():
        return None
    profile = stored_profile(current_app.redis, user.id)

    matched = [item for item in stored.get("items", []) if item.get("tmdb_id")]
    unmatched = [item for item in stored.get("items", []) if not item.get("tmdb_id")]
    tmdb_ids = [item["tmdb_id"] for item in matched]

    owned = {}
    seen = set()
    watchlisted = set()
    if tmdb_ids:
        owned = dict(
            db.session.query(Movie.tmdb_id, Movie.id)
            .filter(Movie.tmdb_id.in_(tmdb_ids))
            .filter(Movie.files.any(File.feature_type_id.is_(None)))
        )
        seen = {
            tmdb_id
            for (tmdb_id,) in db.session.query(Movie.tmdb_id)
            .join(UserMovieReview, UserMovieReview.movie_id == Movie.id)
            .filter(Movie.tmdb_id.in_(tmdb_ids))
            .filter(UserMovieReview.user_id == int(user.id))
        }
        watchlisted = {
            tmdb_id
            for (tmdb_id,) in db.session.query(Movie.tmdb_id)
            .join(UserWatchlist, UserWatchlist.movie_id == Movie.id)
            .filter(Movie.tmdb_id.in_(tmdb_ids))
            .filter(UserWatchlist.user_id == int(user.id))
        }

    items = []
    for item in matched:
        tmdb_id = item["tmdb_id"]
        if profile:
            score, contributions = score_movie(_payload_features(item), profile)
        else:
            score, contributions = 0.0, []
        items.append(
            {
                "tmdb_id": tmdb_id,
                "title": item.get("title"),
                "year": item.get("year"),
                "poster_path": item.get("poster_path"),
                "runtime": item.get("runtime"),
                "overview": item.get("overview"),
                "movie_id": owned.get(tmdb_id),
                "owned": tmdb_id in owned,
                "seen": tmdb_id in seen,
                "watchlisted": tmdb_id in watchlisted,
                "because": [
                    label
                    for contribution, label in contributions[:3]
                    if contribution > 0
                ],
                "score": round(score, 4),
            }
        )
    items.sort(
        key=lambda item: (
            not item["watchlisted"],
            item["owned"],
            -item["score"],
            (item["title"] or "").lower(),
        )
    )
    unmatched = sorted(
        (
            {
                "title": film.get("title"),
                "year": film.get("year"),
                "director": film.get("director"),
            }
            for film in unmatched
        ),
        key=lambda film: (film["title"] or "").lower(),
    )
    return {
        "departs": departs,
        "url": _source_url(stored, departs),
        "items": items,
        "unmatched": unmatched,
    }
