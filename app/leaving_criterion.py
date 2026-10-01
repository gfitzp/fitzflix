"""Build the "Leaving the Criterion Channel" shelf of the landing page.

criterionchannel.com/discover/leaving-{month}-{lastday} is the
canonical source for the films that leave at the end of the month. The
page is a Next.js page since 2026-09. Its app data carries a playlist
with the title, the release date, and the film page of each film. The
film page carries the director. No feed or API exists. JustWatch has
no public leaving API. TMDB does not license departure data.
Fitzflix scrapes this 1 official page to get
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
DIRECTOR_KEY = "fitzflix:criterion:director:{media_id}"
MATCH_CACHE_SECONDS = 60 * 86400
MATCH_CANDIDATES = 5

# The collection pages are below /discover since the 2026-09 redesign of
# the Channel. The old top-level addresses no longer show a collection.

COLLECTION_ORIGIN = "https://www.criterionchannel.com/discover"

# A collection page carries its films in the data of the Next.js app,
# as a "playlist" array. Each film has a title, a release date, a
# content type, and a deeplink to its film page.

PLAYLIST_START_RE = re.compile(r'"playlist"\s*:\s*\[')
MEDIA_ID_RE = re.compile(r"/films/([^/?#]+)")


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
                f"{COLLECTION_ORIGIN}/leaving-{month_name}-{last_day}",
                date(year, month, last_day),
            )
        )
    return candidates


def parse_collection_page(page_html):
    """Return [{title, director, year, url}] from 1 collection page, or
    [] if the page has no films.

    The films are the first playlist of the page that holds a film. An
    entry of a different content type (a series, a collection) is not
    a film and is left out. The playlist has no director. Thus, the
    director is None here. The url is the film page."""

    # Imported here. criterion_now imports this module at its top.
    from app.criterion_now import _app_data

    data = _app_data(page_html)
    decoder = json.JSONDecoder()
    for match in PLAYLIST_START_RE.finditer(data):
        try:
            playlist, _ = decoder.raw_decode(data, match.end() - 1)
        except ValueError:
            continue
        films = []
        for entry in playlist:
            if not isinstance(entry, dict) or entry.get("contentType") != "film":
                continue
            title = str(entry.get("title") or "").replace("\xa0", " ").strip()
            if not title:
                continue
            year = re.match(r"(\d{4})", str(entry.get("release_date") or ""))
            films.append(
                {
                    "title": title,
                    "director": None,
                    "year": int(year.group(1)) if year else None,
                    "url": entry.get("deeplink") or None,
                }
            )
        if films:
            return films
    return []


def film_page_director(url):
    """Return the director that the film page of the Channel names, or
    None.

    The cache keeps the answer for 2 months. Thus, a daily refresh reads
    only the pages of the films that are new to a collection. A page
    that does not answer is not cached."""

    media = MEDIA_ID_RE.search(url or "")
    if not media:
        return None
    cache_key = DIRECTOR_KEY.format(media_id=media.group(1))
    cached = current_app.redis.get(cache_key)
    if cached is not None:
        return json.loads(cached) or None

    # Imported here. criterion_now imports this module at its top.
    from app.criterion_now import parse_film_info

    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        director = parse_film_info(r.text)["director"]
    except Exception:
        current_app.logger.warning(traceback.format_exc())
        return None
    current_app.redis.set(cache_key, json.dumps(director), ex=MATCH_CACHE_SECONDS)
    return director


def fetch_collection_films(url):
    """Return [{title, director, year}] scraped from 1 collection page,
    without duplicates.

    The page carries its complete playlist. It returns [] if the page
    does not answer. The director of each film comes from its film
    page. This is the generic half of the scraper. The leaving page and
    the newly-added feed (#246, app.newly_added) both read through
    it."""

    try:
        r = requests.get(url, timeout=15)
        if r.status_code != 200:
            return []
        films = parse_collection_page(r.text)
    except Exception:
        current_app.logger.warning(traceback.format_exc())
        return []
    seen = set()
    unique = []
    for film in films:
        key = (film["title"].lower(), film["year"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(
            {
                "title": film["title"],
                "director": film_page_director(film["url"]),
                "year": film["year"],
            }
        )
    return unique


def fetch_leaving_films():
    """Return (departure date, source url, films) scraped from the
    official leaving page.

    It returns (None, None, []) if no candidate page has films."""

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


# The words that carry no meaning in a title comparison.

TITLE_STOPWORDS = {"a", "an", "and", "in", "of", "the", "to"}


def _title_words(text):
    """Return the set of significant words of a title.

    A plural loses its final "s". Thus, "Spider’s Eyes" and "Eyes of
    the Spider" give the same set."""

    words = set()
    for word in _normalize(text).split():
        if word in TITLE_STOPWORDS:
            continue
        words.add(word[:-1] if len(word) > 3 and word.endswith("s") else word)
    return words


def _same_person(wanted, name):
    """Return True if a credited name can name a scraped director.

    Both values are comparison keys from _normalize. The scraped value
    can hold more than 1 director. The family name of the credit must
    be a word of the scraped value, or 2 words must agree. Thus,
    "Lindsey C. Vickers" matches "Lindsey Vickers", and "Adam Wingard"
    matches the "Andrew Wingard" of the Channel."""

    wanted_words = set(wanted.split())
    name_words = name.split()
    if not name_words:
        return False
    return name_words[-1] in wanted_words or len(wanted_words & set(name_words)) >= 2


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
    of each candidate. It tries exact-title candidates first. A strict
    name test runs first, then a loose one (_same_person). If no
    candidate passes, the films of the director are the last source
    (by_filmography). If the director matches no film that TMDB
    offers, the film stays unmatched (a plain "Also leaving" row). It does not become the
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

    credited = {}

    def directed_by(result, loose=False):
        """Return True if the credited directors of the candidate include
        the scraped director.

        This also returns True if no director is credited and the title
        and the year agree exactly. The strict test wants one name to
        contain the other. The loose test is _same_person."""

        if result["id"] not in credited:
            body = _tmdb_json(f"/movie/{result['id']}/credits", {})
            credited[result["id"]] = (
                None
                if body is None
                else [
                    _normalize(person.get("name"))
                    for person in body.get("crew") or []
                    if person.get("job") == "Director"
                ]
            )
        directors = credited[result["id"]]
        if directors is None:
            return False
        if not directors:
            return _normalize(result.get("title")) == wanted_title and (
                result.get("release_date") or ""
            )[:4] == str(year)
        if loose:
            return any(_same_person(wanted_director, name) for name in directors)
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
            # The Channel and TMDB can write the name of a person
            # differently (a middle initial, a wrong first name). The
            # loose test runs only after the strict test fails for each
            # candidate.
            for result in found:
                if directed_by(result, loose=True):
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

    def by_filmography():
        """Return the film of the scraped director that has this title
        under a different word order, or None.

        The Channel and TMDB can give a film different English titles.
        The Channel has "Spider’s Eyes". TMDB has "Eyes of the Spider".
        Then the title search finds nothing. This reads the films that
        the director made within 1 year of the scraped date. A film
        passes if its significant words contain those of the scraped
        title, or the reverse. The answer must be 1 film only."""

        wanted_words = _title_words(title)
        if not (director and year and wanted_words):
            return None
        hits = set()
        for name in re.split(r",|&|\band\b", director):
            wanted_name = _normalize(name)
            if not wanted_name:
                continue
            body = _tmdb_json("/search/person", {"query": name.strip()})
            people = [
                person
                for person in (body or {}).get("results") or []
                if _normalize(person.get("name")) == wanted_name
            ]
            for person in people[:2]:
                body = _tmdb_json(f"/person/{person['id']}/movie_credits", {})
                for credit in (body or {}).get("crew") or []:
                    released = (credit.get("release_date") or "")[:4]
                    if credit.get("job") != "Director" or not released.isdigit():
                        continue
                    if abs(int(released) - int(year)) > 1:
                        continue
                    for credit_title in (
                        credit.get("title"),
                        credit.get("original_title"),
                    ):
                        words = _title_words(credit_title)
                        if words and (words <= wanted_words or wanted_words <= words):
                            hits.add(credit["id"])
        if len(hits) != 1:
            return None
        (found_id,) = hits
        current_app.logger.info(
            f"Leaving-Criterion: '{title}' ({year}) matched TMDB {found_id} "
            f"through the films of {director}"
        )
        return found_id

    tmdb_id = None
    if year:
        tmdb_id = pick({"primary_release_year": year})
    if tmdb_id is None:
        tmdb_id = pick({})
    if tmdb_id is None:
        tmdb_id = by_filmography()
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
        # The Leaving Soon channel of the dial reads this set. Rebuild
        # the same day when an owned film is on it. The Channel
        # publishes the page of the new month on its own clock. On
        # 2026-09-01, that was after the nightly build.
        from app.dvr import enqueue_lineup_rebuild

        enqueue_lineup_rebuild(
            "new leaving-Criterion set", tmdb_ids=[item["tmdb_id"] for item in items]
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
        f"{COLLECTION_ORIGIN}/leaving-"
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
