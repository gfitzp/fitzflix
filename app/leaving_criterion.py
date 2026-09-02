"""Build the "Leaving the Criterion Channel" shelf of the landing page.

criterionchannel.com/leaving-{month}-{lastday} is the canonical source
for the films that leave at the end of the month. It is structured
HTML with a tooltip for each film. The tooltip carries the title, the
director, and the year. No feed or API exists. The JSON and RSS
variants return 404. JustWatch has no public leaving API. TMDB does not
license departure data. Fitzflix scrapes this 1 official page to get
the titles. This is a narrow and deliberate exception to the
no-scraping rule. A daily task parses the collection. The task does
nothing while the stored set is current. The task matches each film to
TMDB by title and year. It embeds the enriched payloads. Thus, the
shelf lives longer than each shorter cache. It stores the set with its
departure date. The landing page ranks the set against the taste
profile of the viewer for Criterion subscribers.

Shelf rules: The shelf excludes owned films. There is no urgency for
them. The shelf is about films to watch before they leave. The shelf
excludes diary films unless they are on the watchlist of the user. A
leaving film on the watchlist is the strongest signal of all. Watch it
now, or buy the disc.
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

# The app instance of this process. Fitzflix resolves it lazily. Thus,
# the monthly task can run on a worker without a second application.

app = LocalProxy(get_app)

# The TMDB provider id for the Criterion Channel. The shelf renders
# only for users that subscribe to it.

CRITERION_PROVIDER_ID = 258

LEAVING_KEY = "fitzflix:criterion:leaving"
MATCH_KEY = "fitzflix:criterion:match:{slug}"
MATCH_CACHE_SECONDS = 60 * 86400
MATCH_CANDIDATES = 5
PAGE_CAP = 10

# There is 1 film for each tooltip: the title heading, then an optional
# "Directed by X • YYYY • Country" line.

TOOLTIP_TITLE_RE = re.compile(
    r"tooltip-item-title[^>]*>\s*<strong>\s*(.*?)\s*</strong>", re.S
)
TOOLTIP_META_RE = re.compile(
    r"(?:Directed by\s+(?P<director>[^•<]+?)\s*)?•\s*(?P<year>\d{4})\s*•"
)


def leaving_page_candidates(today):
    """Return the (url, departure date) candidates, most likely first.

    The order is: the page of this month, the page of the next month,
    then the page of the last month. The new page can appear before the
    old departure passes. Early in a month, the new page can be absent.
    """

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
    """Return [{title, director, year}] from 1 page of the tooltip markup
    of the leaving collection, or [] if the page has no films."""

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
    """Return [{title, director, year}] scraped from 1 VHX collection
    page, without duplicates.

    Each Criterion Channel collection serves the ?html=1&page=N markup.
    This reads the pages until an empty page. It returns [] if the page
    does not answer. This is the generic half of the scraper. The
    leaving page and the newly-added feed (#246, app.newly_added) both
    read through it."""

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
    """Return (departure date, source url, films) scraped from the
    official leaving page.

    This reads the pages until an empty page. It returns (None, None,
    []) if no candidate page answers."""

    for url, departs in leaving_page_candidates(date.today()):
        films = fetch_collection_films(url)
        if films:
            return departs, url, films
    return None, None, []


def _normalize(text):
    """Return a comparison key for titles and names.

    This folds accents. It removes case and punctuation. It removes
    apostrophes (straight or curly) fully. Thus, "Muriel’s Wedding"
    matches "Muriel's Wedding", and "P. J. Hogan" matches "P.J.
    Hogan"."""

    text = re.sub(r"[\'’`]", "", text or "")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _tmdb_json(path, params):
    """Return the JSON body of 1 TMDB GET, or None on a failure (logged)."""

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
    """Return the TMDB id for a leaving film, or None if TMDB has no match.

    This searches by title and year. It caches the result for 2
    months.

    The TMDB search ranks by popularity. Thus, a generic short-film
    title such as "Here", "Kid", or "Ambition" returns a popular feature
    first. The 2026-08 set put "Right Here, Right Now" and "The Karate
    Kid" on the shelf in place of the films of Bas Devos and Hal
    Hartley. Thus, this verifies a scraped director against the credits
    of each candidate. It tries exact-title candidates first. If the
    director matches no candidate that TMDB offers, the film stays
    unmatched (a plain "Also leaving" row). It does not become the
    wrong film. A candidate with no credited director passes on an
    exact title-and-year match. Shorts frequently have no crew on TMDB.
    Without a scraped director, the exact-title candidate wins. If
    there is none, the first result wins (the behaviour before
    2026-08). The cache key carries the director. Thus, a
    director-aware lookup never reads an entry that the older
    title-only matcher wrote.
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
        """Return the search results that are worth a look.

        Exact-title matches come first. Then come releases within 1
        year of the scraped date. Criterion and TMDB frequently differ
        by 1 year (festival year versus release year). The list is
        limited to the few that are worth a credits lookup. With a
        director to verify, only those 2 kinds are candidates. The
        fallback without a year reads 2 pages. A popular unrelated film
        must not cost a credits call."""

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
        """Return True if the credited directors of the candidate include
        the scraped director.

        This also returns True if no director is credited and the title
        and the year agree exactly."""

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
        """Return the chosen candidate id for 1 search, or None.

        If the director of no candidate agrees, the 1 result that TMDB
        knows by exactly this title and year passes. Criterion and TMDB
        can credit a film differently. Criterion files "Regarding Soon"
        under Hal Hartley, its subject. TMDB files it under Richard
        Sylvarnes, who shot and cut it. This passes only if the result
        is unique. Thus, an unrelated film with the same title and year
        cannot get in."""

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
    """Scrape the leaving collection and store the set with its departure
    date.

    This is a daily task. It matches each film to TMDB. It embeds the
    enriched payloads. It does nothing while the departure of the
    stored set is in the future. The daily cadence exists to retry
    until Criterion publishes the page of the new month. That page
    appears at some time after the old set departs. The schedule is
    not known. The stored set has no TTL. The shelf hides after the
    departure date passes."""

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

        # Films that the TMDB matcher cannot resolve still go into the
        # stored set. They carry only the scraped facts (title, director,
        # year). The /leaving page lists them as plain rows. Thus, the
        # departure inventory stays complete. The home shelf skips them.
        # Its cards need posters and taste features.

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
    """Return the (owned, logged, watchlisted, refused) tmdb-id sets for 1
    user over the given films.

    These are the exclusion inputs that the discovery shelves share
    (the leaving shelf here, the newly-added shelves in
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
    """Return the taste-ranked departure shelf for 1 user, or None.

    This renders only for Criterion subscribers with a stored set that
    has not departed yet. Owned, diary, and watchlisted films all drop
    out. This is a discovery shelf since 2026-08-30. A watchlisted
    departure is the watch-it-now-or-buy-it case. It leads the
    watchlist shelf of the landing page. It is not pinned here.
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
    """Return the departure date, as "August 31", or None.

    This returns the date if the film is in the stored leaving set and
    the date has not passed. Each Criterion Channel availability badge
    asks this (the popover of #45c, the movie page, search results,
    filmography rows, the watchlist). Thus, this parses the set 1 time
    for each app context and keeps it on flask.g. That is 1 Redis read
    for each page, not 1 for each film."""

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
    """Return the URL of the scraped page.

    Payloads stored before the source key existed build the URL from
    the departure date. It has the same shape that the candidate list
    builds."""

    return stored.get("source") or (
        "https://www.criterionchannel.com/leaving-"
        f"{calendar.month_name[departs.month].lower()}-{departs.day}"
    )


def leaving_inventory(user):
    """Return the complete departing set for the /leaving page, or None.

    Unlike the home shelf, this excludes nothing. Owned films stay
    listed with their library badge. That is the relaxed case. The disc
    is on the shelf. Seen films stay with their Seen badge. Films that
    the TMDB matcher could not resolve come last as plain scraped rows.
    Thus, the inventory is the full departure set. Watchlisted films
    come first. Then come unowned films by taste score. Owned films
    come after.
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
