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

from collections import namedtuple
from datetime import date, datetime

import requests

from flask import current_app, g
from werkzeug.local import LocalProxy

from app import db, get_app
from app.models import File, Movie
from app.funnel import user_film_sets
from app.recommendations import (
    record_lane_scores,
    stored_profile,
    tmdb_taste_score,
)
from app.cache import MISS, cache_json, cached_json
from app.streaming_rail import enriched_movie, movie_gone
from app.tmdb_apply import tmdb_json_or
from app.title_keys import match_key

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


# film_page_director returns this when the film page did not answer. A
# None director means that the page names no director (#539).

DIRECTOR_FETCH_FAILED = object()


def film_page_director(url):
    """Return the director that the film page of the Channel names, or
    None.

    The cache keeps the answer for 2 months. Thus, a daily refresh reads
    only the pages of the films that are new to a collection. A page
    that does not answer is not cached. Then the function returns
    DIRECTOR_FETCH_FAILED, not None. A missing director disables the
    director test of the TMDB match (#539)."""

    media = MEDIA_ID_RE.search(url or "")
    if not media:
        return None
    cache_key = DIRECTOR_KEY.format(media_id=media.group(1))
    cached = cached_json(cache_key)
    if cached is not MISS:
        return cached or None

    # Imported here. criterion_now imports this module at its top.
    from app.criterion_now import parse_film_info

    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        director = parse_film_info(r.text)["director"]
    except Exception:
        current_app.logger.warning(traceback.format_exc())
        return DIRECTOR_FETCH_FAILED
    cache_json(cache_key, director, MATCH_CACHE_SECONDS)
    return director


def fetch_collection_films(url):
    """Return [{title, director, year}] scraped from 1 collection page,
    without duplicates.

    The page carries its complete playlist. It returns [] if the page
    does not answer. The director of each film comes from its film
    page. This is the generic half of the scraper. The leaving page and
    the newly-added feed (#246, app.newly_added) both read through
    it. A film whose film page did not answer gets "director_failed".
    The callers do not match it that day (#539)."""

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
        director = film_page_director(film["url"])
        entry = {"title": film["title"], "director": director, "year": film["year"]}
        if director is DIRECTOR_FETCH_FAILED:
            entry.update(director=None, director_failed=True)
        unique.append(entry)
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


# The words that carry no meaning in a title comparison.

TITLE_STOPWORDS = {"a", "an", "and", "in", "of", "the", "to"}


def _title_words(text):
    """Return the set of significant words of a title.

    A plural loses its final "s". Thus, "Spider’s Eyes" and "Eyes of
    the Spider" give the same set."""

    words = set()
    for word in match_key(text).split():
        if word in TITLE_STOPWORDS:
            continue
        words.add(word[:-1] if len(word) > 3 and word.endswith("s") else word)
    return words


def _same_person(wanted, name):
    """Return True if a credited name can name a scraped director.

    Both values are comparison keys from match_key. The scraped value
    can hold more than 1 director. The family name of the credit must
    be a word of the scraped value, or 2 words must agree. Thus,
    "Lindsey C. Vickers" matches "Lindsey Vickers", and "Adam Wingard"
    matches the "Andrew Wingard" of the Channel."""

    wanted_words = set(wanted.split())
    name_words = name.split()
    if not name_words:
        return False
    return name_words[-1] in wanted_words or len(wanted_words & set(name_words)) >= 2


# A search result of TMDB, reduced to what the match rules read. The
# title is a comparison key from match_key. The year is the first 4
# characters of the release date. The directors are comparison keys,
# None before the credits lookup or if it failed, and an empty list if
# TMDB credits no director.

Candidate = namedtuple("Candidate", "id title year directors")


def candidate_of(result):
    """Return the Candidate of a TMDB search result, with no directors yet."""

    return Candidate(
        result.get("id"),
        match_key(result.get("title")),
        (result.get("release_date") or "")[:4],
        None,
    )


def rank(candidate, wanted_title, year):
    """Return the sort key of a candidate. (False, False) is the best.

    Exact-title matches come first. Then come releases within 1 year of
    the scraped date. Criterion and TMDB frequently differ by 1 year
    (festival year versus release year)."""

    exact = candidate.title == wanted_title
    near = (
        bool(year)
        and candidate.year.isdigit()
        and abs(int(candidate.year) - int(year)) <= 1
    )
    return (not exact, not near)


def exact_title_and_year(candidate, wanted_title, year):
    """Return True if the title and the year of a candidate agree exactly."""

    return candidate.title == wanted_title and candidate.year == str(year)


def director_matches(candidate, wanted_director, loose=False, exact=False):
    """Return True if the credited directors of a candidate include the scraped director.

    exact is the result of exact_title_and_year. A candidate with no
    credited director passes on it. Shorts frequently have no crew on
    TMDB. A candidate whose credits lookup failed never passes. The
    strict test wants one name to contain the other. The loose test is
    _same_person."""

    directors = candidate.directors
    if directors is None:
        return False
    if not directors:
        return exact
    if loose:
        return any(_same_person(wanted_director, name) for name in directors)
    return any(
        name and (name in wanted_director or wanted_director in name)
        for name in directors
    )


def unique_exact(candidates, wanted_title, year):
    """Return the 1 candidate with exactly this title and year, or None.

    Criterion and TMDB can credit a film differently. Criterion files
    "Regarding Soon" under Hal Hartley, its subject. TMDB files it under
    Richard Sylvarnes, who shot and cut it. The rule passes only if the
    result is unique. Thus, an unrelated film with the same title and
    year cannot get in."""

    exact = [c for c in candidates if exact_title_and_year(c, wanted_title, year)]
    return exact[0] if len(exact) == 1 else None


def filmography_hit(credit, wanted_words, year):
    """Return True if a crew credit of a person is the scraped film.

    The credit must be a Director credit within 1 year of the scraped
    date. Its significant words must contain those of the scraped
    title, or the reverse. The title or the original title can pass."""

    released = (credit.get("release_date") or "")[:4]
    if credit.get("job") != "Director" or not released.isdigit():
        return False
    if abs(int(released) - int(year)) > 1:
        return False
    for credit_title in (credit.get("title"), credit.get("original_title")):
        words = _title_words(credit_title)
        if words and (words <= wanted_words or wanted_words <= words):
            return True
    return False


class _MatchLookups:
    """The TMDB reads of 1 match_tmdb_id call.

    failed holds the path of each call that failed. A failed call must
    not read as "TMDB has no such film". The credited directors of each
    candidate are read 1 time."""

    def __init__(self):
        self.failed = []
        self._directors = {}

    def json(self, path, params):
        """Return the JSON body of a TMDB call, or None if it failed."""

        body = tmdb_json_or(path, params, None)
        if body is None:
            self.failed.append(path)
        return body

    def search(self, title, params, pages):
        """Return the Candidates of up to pages pages of a title search."""

        results = []
        for page in range(1, pages + 1):
            body = self.json("/search/movie", {"query": title, "page": page, **params})
            page_results = (body or {}).get("results") or []
            results.extend(page_results)
            if len(page_results) < 20:
                break
        return [candidate_of(result) for result in results]

    def with_directors(self, candidate):
        """Return the candidate with its credited directors."""

        if candidate.id not in self._directors:
            body = self.json(f"/movie/{candidate.id}/credits", {})
            self._directors[candidate.id] = (
                None
                if body is None
                else [
                    match_key(person.get("name"))
                    for person in body.get("crew") or []
                    if person.get("job") == "Director"
                ]
            )
        return candidate._replace(directors=self._directors[candidate.id])

    def director_credits(self, name):
        """Yield the crew credits of the people that TMDB knows by exactly this name.

        Only the first 2 people of the person search count."""

        wanted_name = match_key(name)
        if not wanted_name:
            return
        body = self.json("/search/person", {"query": name.strip()})
        people = [
            person
            for person in (body or {}).get("results") or []
            if match_key(person.get("name")) == wanted_name
        ]
        for person in people[:2]:
            body = self.json(f"/person/{person['id']}/movie_credits", {})
            yield from (body or {}).get("crew") or []


def _match_candidates(lookups, title, year, wanted_director, params):
    """Return the search results that are worth a look, best first.

    The list is limited to the few that are worth a credits lookup.
    With a director to verify, only exact titles and near years are
    candidates. The fallback without a year reads 2 pages. A popular
    unrelated film must not cost a credits call."""

    wanted_title = match_key(title)
    found = lookups.search(title, params, pages=1 if params else 2)
    ordered = sorted(found, key=lambda c: rank(c, wanted_title, year))
    if wanted_director:
        ordered = [c for c in ordered if rank(c, wanted_title, year) != (True, True)]
    return ordered[:MATCH_CANDIDATES]


def _pick(lookups, title, year, director, params):
    """Return the chosen candidate id for 1 search, or None.

    The strict director test runs on each candidate first. The Channel
    and TMDB can write the name of a person differently (a middle
    initial, a wrong first name). Thus, the loose test runs next. Then
    unique_exact is the last rule."""

    wanted_title = match_key(title)
    wanted_director = match_key(director) if director else None
    found = _match_candidates(lookups, title, year, wanted_director, params)
    if not wanted_director:
        return found[0].id if found else None
    for loose in (False, True):
        for candidate in found:
            candidate = lookups.with_directors(candidate)
            exact = exact_title_and_year(candidate, wanted_title, year)
            if director_matches(candidate, wanted_director, loose, exact):
                return candidate.id
    only = unique_exact(found, wanted_title, year)
    if only is None:
        return None
    current_app.logger.info(
        f"Leaving-Criterion: '{title}' ({year}) matched TMDB "
        f"{only.id} by exact title and year; TMDB "
        f"credits a director other than {director}"
    )
    return only.id


def _by_filmography(lookups, title, year, director):
    """Return the film of the scraped director that has this title under a different word order, or None.

    The Channel and TMDB can give a film different English titles. The
    Channel has "Spider’s Eyes". TMDB has "Eyes of the Spider". Then
    the title search finds nothing. This reads the films of each
    scraped director (filmography_hit). The answer must be 1 film
    only."""

    wanted_words = _title_words(title)
    if not (director and year and wanted_words):
        return None
    hits = set()
    for name in re.split(r",|&|\band\b", director):
        for credit in lookups.director_credits(name):
            if filmography_hit(credit, wanted_words, year):
                hits.add(credit["id"])
    if len(hits) != 1:
        return None
    (found_id,) = hits
    current_app.logger.info(
        f"Leaving-Criterion: '{title}' ({year}) matched TMDB {found_id} "
        f"through the films of {director}"
    )
    return found_id


def _match_slug(title, year, director):
    """Return the slug of the match cache key. It carries the director."""

    text = f"{title}-{year}-{director or ''}".lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


def match_tmdb_id(title, year, director=None, failures=None):
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
    (_by_filmography). If the director matches no film that TMDB
    offers, the film stays unmatched (a plain "Also leaving" row). It does not become the
    wrong film. A candidate with no credited director passes on an
    exact title-and-year match. Shorts frequently have no crew on TMDB.
    Without a scraped director, the exact-title candidate wins. If
    there is none, the first result wins (the behaviour before
    2026-08). The cache key carries the director. Thus, a
    director-aware lookup never reads an entry that the older
    title-only matcher wrote. If a TMDB call fails and nothing matches,
    the miss is not cached, and "tmdb" goes into failures, if given.
    """

    cache_key = MATCH_KEY.format(slug=_match_slug(title, year, director))
    stored = cached_json(cache_key)
    if stored is not MISS:
        return stored or None

    lookups = _MatchLookups()
    tmdb_id = None
    if year:
        tmdb_id = _pick(lookups, title, year, director, {"primary_release_year": year})
    if tmdb_id is None:
        tmdb_id = _pick(lookups, title, year, director, {})
    if tmdb_id is None:
        tmdb_id = _by_filmography(lookups, title, year, director)

    # The cache keeps a miss for 2 months. Thus, a failed call does not
    # store one.

    if tmdb_id is None and lookups.failed:
        if failures is not None:
            failures.append("tmdb")
        return None
    cache_json(cache_key, tmdb_id, MATCH_CACHE_SECONDS)
    return tmdb_id


def refresh_leaving_criterion():
    """Scrape the leaving collection and store the set with its departure
    date.

    This is a daily task. It matches each film to TMDB. It embeds the
    enriched payloads. It does nothing while the departure of the
    stored set is in the future, unless a TMDB failure left the set
    incomplete. The daily cadence exists to retry
    until Criterion publishes the page of the new month. That page
    appears at some time after the old set departs. The schedule is
    not known. The stored set has no TTL. The shelf hides after the
    departure date passes."""

    with app.app_context():
        stored = current_app.redis.get(LEAVING_KEY)
        if stored:
            stored = json.loads(stored)
            if date.fromisoformat(stored["departs"]) >= date.today() and not stored.get(
                "incomplete"
            ):
                return True

        departs, source, films = fetch_leaving_films()
        if not films:
            current_app.logger.warning(
                "Leaving-Criterion: no films found on any candidate page"
            )
            return True

        # The candidates include the page of last month. It stays up for
        # some days after its month. A failed fetch of this month must not
        # replace the stored set with the departed one (#324). The stored
        # set keeps its incomplete mark, and the next run tries again.

        if stored and (
            departs < date.fromisoformat(stored["departs"]) or departs < date.today()
        ):
            current_app.logger.warning(
                f"Leaving-Criterion: '{source}' departs {departs.isoformat()}, "
                f"before the stored set ({stored['departs']}). Keeping the "
                f"stored set"
            )
            return True

        # Films that the TMDB matcher cannot resolve still go into the
        # stored set. They carry only the scraped facts (title, director,
        # year). The /leaving page lists them as plain rows. Thus, the
        # departure inventory stays complete. The home shelf skips them.
        # Its cards need posters and taste features.

        # A TMDB failure marks the set incomplete. Then the next daily run
        # matches the set again. Else the films stay unmatched until the
        # set departs.

        items = []
        failures = []
        for film in films:
            # Without the director, the matcher takes the first search
            # hit. A generic title then becomes a different film for 2
            # months. Thus, the film waits for the next run (#539).
            if film.get("director_failed"):
                failures.append("film page")
                items.append({**film, "tmdb_id": None})
                continue
            tmdb_id = match_tmdb_id(
                film["title"], film["year"], film["director"], failures=failures
            )
            payload = enriched_movie(tmdb_id) if tmdb_id is not None else None
            if payload:
                items.append({**payload, "tmdb_id": tmdb_id})
            else:
                if tmdb_id is not None and not movie_gone(tmdb_id):
                    failures.append("tmdb")
                items.append({**film, "tmdb_id": None})

        current_app.redis.set(
            LEAVING_KEY,
            json.dumps(
                {
                    "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "departs": departs.isoformat(),
                    "source": source,
                    "items": items,
                    "incomplete": bool(failures),
                }
            ),
        )
        current_app.logger.info(
            f"Leaving-Criterion: stored {len(items)} of {len(films)} films "
            f"departing {departs.isoformat()}"
            + (
                f" ({len(failures)} lookup failure(s), retrying tomorrow)"
                if failures
                else ""
            )
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

    # A film with a record ranks by the number of its tile (#535).

    recorded = record_lane_scores(current_app.redis, user.id, tmdb_ids, profile)

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
        score, contributions = tmdb_taste_score(user.id, tmdb_id, item, profile)
        score = recorded.get(tmdb_id, score)
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
        _, seen, watchlisted, _ = user_film_sets(user, tmdb_ids)

    items = []
    for item in matched:
        tmdb_id = item["tmdb_id"]
        if profile:
            score, contributions = tmdb_taste_score(user.id, tmdb_id, item, profile)
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
