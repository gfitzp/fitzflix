"""The landing page's "Leaving the Criterion Channel" shelf.

criterionchannel.com/leaving-{month}-{lastday} is the canonical source
for what departs at month's end — structured HTML with a tooltip per
film carrying title, director, and year; no feed or API exists (the
JSON/RSS variants 404, JustWatch has no public leaving API, and TMDb
doesn't license departure data). Scraping this one official page for
title extraction is a narrow, deliberate exception to the no-scraping
rule. A monthly task parses the collection, matches each film to TMDb
by title and year, embeds the enriched payloads (so the shelf outlives
every shorter cache), and stores the set with its departure date; the
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

from datetime import date, datetime

import requests

from flask import current_app
from werkzeug.local import LocalProxy

from app import db, get_app
from app.models import File, Movie, UserMovieReview, UserWatchlist
from app.recommendations import score_movie, stored_profile
from app.streaming_rail import _payload_features, enriched_movie
from app.videos import tmdb_get

# This process's app instance, resolved lazily so the monthly task can
# run on a worker without building a second application

app = LocalProxy(get_app)

# TMDb's provider id for the Criterion Channel — the shelf only renders
# for users subscribed to it

CRITERION_PROVIDER_ID = 258

LEAVING_KEY = "fitzflix:criterion:leaving"
MATCH_KEY = "fitzflix:criterion:match:{slug}"
MATCH_CACHE_SECONDS = 60 * 86400
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


def fetch_leaving_films():
    """(departure date, source url, films) scraped from the official
    leaving page, paginating until an empty page; (None, None, []) when
    no candidate page answers."""

    for url, departs in leaving_page_candidates(date.today()):
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
            continue
        if films:
            seen = set()
            unique = []
            for film in films:
                key = (film["title"].lower(), film["year"])
                if key not in seen:
                    seen.add(key)
                    unique.append(film)
            return departs, url, unique
    return None, None, []


def match_tmdb_id(title, year):
    """The TMDb id for a leaving film, by title-and-year search, cached
    for two months; None when TMDb has no match."""

    slug = re.sub(r"[^a-z0-9]+", "-", f"{title}-{year}".lower()).strip("-")
    cache_key = MATCH_KEY.format(slug=slug)
    cached = current_app.redis.get(cache_key)
    if cached:
        stored = json.loads(cached)
        return stored or None

    def search(params):
        """First search-result id for the given params, or None."""

        try:
            r = tmdb_get(
                current_app.config["TMDB_API_URL"] + "/search/movie",
                params={"api_key": current_app.config["TMDB_API_KEY"], **params},
                timeout=10,
            )
            r.raise_for_status()
            results = r.json().get("results") or []
        except Exception:
            current_app.logger.warning(traceback.format_exc())
            return None
        return results[0].get("id") if results else None

    tmdb_id = None
    if year:
        tmdb_id = search({"query": title, "primary_release_year": year})
    if tmdb_id is None:
        tmdb_id = search({"query": title})
    current_app.redis.set(cache_key, json.dumps(tmdb_id), ex=MATCH_CACHE_SECONDS)
    return tmdb_id


def refresh_leaving_criterion():
    """Monthly task: scrape the leaving collection, match each film to
    TMDb, embed the enriched payloads, and store the set with its
    departure date. The stored set has no TTL — the shelf simply hides
    once the departure date passes."""

    with app.app_context():
        departs, source, films = fetch_leaving_films()
        if not films:
            current_app.logger.warning(
                "Leaving-Criterion: no films found on any candidate page"
            )
            return True

        items = []
        for film in films:
            tmdb_id = match_tmdb_id(film["title"], film["year"])
            if tmdb_id is None:
                continue
            payload = enriched_movie(tmdb_id)
            if not payload:
                continue
            items.append({**payload, "tmdb_id": tmdb_id})

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


def leaving_shelf(user):
    """The taste-ranked departure shelf for one user, or None.

    Renders only for Criterion subscribers with a stored set that
    hasn't departed yet. Owned films drop out; diary films drop out
    unless they're on the user's watchlist, and a watchlisted leaving
    film badges and sorts first — the watch-it-now-or-buy-it case.
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

    tmdb_ids = [item["tmdb_id"] for item in stored.get("items", [])]
    if not tmdb_ids:
        return None
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

    items = []
    for item in stored.get("items", []):
        tmdb_id = item["tmdb_id"]
        if tmdb_id in owned:
            continue
        if tmdb_id in logged and tmdb_id not in watchlisted:
            continue
        score, contributions = score_movie(_payload_features(item), profile)
        items.append(
            {
                "tmdb_id": tmdb_id,
                "title": item.get("title"),
                "year": item.get("year"),
                "poster_path": item.get("poster_path"),
                "runtime": item.get("runtime"),
                "watchlisted": tmdb_id in watchlisted,
                "because": [
                    label
                    for contribution, label in contributions[:3]
                    if contribution > 0
                ],
                "score": round(score, 4),
            }
        )

    items.sort(key=lambda item: (item["watchlisted"], item["score"]), reverse=True)
    return {"departs": departs, "items": items}
