"""Warm the estimates for the tmdb lane of the shared score source each night.

Decision by Glenn (2026-08): the TMDB API costs only latency. Thus,
Fitzflix spends a nightly budget to warm the enriched payloads that
the browsing of the next day will want. These are the filmographies
of the people that his taste profile ranks highest, plus the popular
and top-rated charts of TMDB. The task then pre-scores all of them
into the tmdb overlay. Thus, the tiles show the estimates immediately.
They do not fill them on demand.

The cursors move forward each night. Each night continues where the
last night stopped. The radius grows through the affinity people and
deeper into the chart pages. A source that wraps around warms its
films again. This sets their TTL of 1 month again. Thus, the tracked
films are in the cache almost always. The one-off on-demand payloads
still expire after the 1-week TTL of the enrichment cache.

This task runs after the 01:45 recompute and the 02:15 rail rebuild.
The recompute deletes the overlays. Thus, the pre-scores derive from
the fresh profile. The enrichments of the rail are already in the
cache, and this task uses them.
"""

import json
import re

from concurrent.futures import ThreadPoolExecutor

from flask import current_app
from werkzeug.local import LocalProxy

from app import db, get_app
from app.models import Movie, User
from app.tmdb_apply import tmdb_json_or
from app.recommendations import (
    CREW_ROLE_JOBS,
    JOB_TO_CLASS,
    PATCH_SCORES_TTL,
    TMDB_PATCH_SCORES_KEY,
    stored_profile,
    tmdb_taste_score,
)
from app.cache import cached_fetch
from app.streaming_rail import ENRICHED_KEY, enriched_movie

# This is the app instance of this process. Fitzflix resolves it lazily.
# Thus, the nightly task can run on a worker without a second application

app = LocalProxy(get_app)

CURSORS_KEY = "fitzflix:estimates:warm:cursors"

# This is the number of enriched payload fetches per night. The budget
# is latency, not money. Thus, the budget is large. 2000 fetches at 10
# in parallel take some minutes of queue time

WARM_FETCH_BUDGET = 2000

# The warmed payloads stay for 1 month. The rolling cursor warms the
# tracked films again before that time. Thus, in practice they never
# expire

WARM_TTL = 30 * 86400

# These are the affinity people warmed per user per night, and the chart
# pages read per chart per night. TMDB stops the chart pages at 500

WARM_PEOPLE = 40
CHART_PAGES_PER_NIGHT = 10
CHART_PAGE_LIMIT = 500
CHARTS = ("popular", "top_rated")

PERSON_CLASSES = ("actor",) + tuple(CREW_ROLE_JOBS)


def _affinity_people(profile):
    """Return the people of the profile, strongest affinity first.

    These are the careers that the user browses most probably. A person
    with credits in more than 1 class (actor and director) counts 1
    time, at the best score."""

    best = {}
    for key, entry in (profile or {}).get("affinities", {}).items():
        cls, _, raw = key.partition(":")
        if cls in PERSON_CLASSES and raw.isdigit():
            person_id = int(raw)
            score = entry.get("score", 0)
            if score > best.get(person_id, float("-inf")):
                best[person_id] = score
    return [person_id for person_id, _ in sorted(best.items(), key=lambda kv: -kv[1])]


# The movie credits of 1 TMDB person, cached for 1 day. The crew list
# keeps only the jobs of the taste roles. The filmography page and the
# warm read the same payload.

PERSON_CREDITS_KEY = "fitzflix:tmdb:person:{person_id}:credits"
PERSON_CREDITS_SECONDS = 86400


def person_movie_credits(person_id):
    """Return {"cast": [...], "crew": [...]} for 1 person, or None.

    None means a transient TMDB error. Nothing is cached then. This is
    the 1 writer of the key."""

    def fetch():
        payload = tmdb_json_or(f"/person/{int(person_id)}/movie_credits", None, None)
        if payload is None:
            return None, False
        credits = {
            "cast": payload.get("cast") or [],
            "crew": [
                crew_credit
                for crew_credit in payload.get("crew") or []
                if crew_credit.get("job") in JOB_TO_CLASS
            ],
        }
        return credits, True

    return cached_fetch(
        PERSON_CREDITS_KEY.format(person_id=int(person_id)),
        PERSON_CREDITS_SECONDS,
        fetch,
    )


# The TV credits of the person pages, cached in Redis for 1 day.

PERSON_TV_CREDITS_KEY = "fitzflix:tmdb:person:{person_id}:tv_credits"

# A TV role where the person appears as themselves. TMDB writes
# these as "Self", "Self - Host", "Herself (archive footage)". The
# self-word starts the line. The pattern is word-bounded. Thus, a real
# character that only contains the letters (Harry Selfridge) survives.

SELF_ROLE = re.compile(r"(?:him|her|them)?sel(?:f|ves)\b", re.IGNORECASE)


def person_tv_credits(person_id):
    """Return {"cast": [...], "crew": [...]} of the TV career of 1 person, or None.

    The self roles and the crew jobs outside the key roles drop out.
    None means a transient TMDB error. Nothing is cached then. The
    filmography renders without the TV rows."""

    def fetch():
        payload = tmdb_json_or(f"/person/{int(person_id)}/tv_credits", None, None)
        if payload is None:
            return None, False
        return {
            "cast": [
                entry
                for entry in payload.get("cast") or []
                if not SELF_ROLE.match(entry.get("character") or "")
            ],
            "crew": [
                entry
                for entry in payload.get("crew") or []
                if entry.get("job") in JOB_TO_CLASS
            ],
        }, True

    return cached_fetch(
        PERSON_TV_CREDITS_KEY.format(person_id=int(person_id)),
        PERSON_CREDITS_SECONDS,
        fetch,
    )


def person_film_ids(person_id):
    """Return the tmdb ids across the career of 1 person.

    This function reads the same credits payload that the filmography
    page reads. That payload stays in the cache for 1 day. Thus, a warm
    here also saves that page its fetch."""

    credits = person_movie_credits(person_id)
    if credits is None:
        return []
    return [
        entry["id"]
        for entry in (credits.get("cast") or []) + (credits.get("crew") or [])
        if entry.get("id")
    ]


def chart_page_ids(chart, page):
    """Return (tmdb ids, total pages) for 1 page of a TMDB chart."""

    payload = tmdb_json_or(f"/movie/{chart}", {"page": page}, None)
    if payload is None:
        return [], None
    ids = [result["id"] for result in payload.get("results") or [] if result.get("id")]
    return ids, payload.get("total_pages")


def prescore_films(redis, user_id, tmdb_ids, profile):
    """Score the cached payloads directly into the tmdb overlay of the user.

    This is the same taste-plus-copref recipe that resolved_tmdb_score
    runs. This function runs it early. Thus, the first tile view reads a
    complete number. Films with local records are not included. The
    movie-id lane owns them."""

    recorded = {
        tmdb_id
        for (tmdb_id,) in db.session.query(Movie.tmdb_id).filter(
            Movie.tmdb_id.in_(list(tmdb_ids) or [0])
        )
    }
    key = TMDB_PATCH_SCORES_KEY.format(user_id=int(user_id))
    scored = 0
    candidates = [tmdb_id for tmdb_id in tmdb_ids if tmdb_id not in recorded]
    for start in range(0, len(candidates), 200):
        chunk = candidates[start : start + 200]
        payloads = redis.mget(
            [ENRICHED_KEY.format(tmdb_id=tmdb_id) for tmdb_id in chunk]
        )
        mapping = {}
        for tmdb_id, payload in zip(chunk, payloads):
            # A cached null is a deleted TMDB id. It is present to prevent
            # a second fetch. There is nothing to score
            data = json.loads(payload) if payload else None
            if not data:
                continue
            mapping[str(tmdb_id)], _ = tmdb_taste_score(user_id, tmdb_id, data, profile)
        if mapping:
            redis.hset(key, mapping=mapping)
            scored += len(mapping)
    if scored:
        redis.expire(key, PATCH_SCORES_TTL)
    return scored


def warm_estimates():
    """Warm the estimate payloads for the next day and pre-score them.

    This nightly task writes the scores into the tmdb overlay of each
    user that has a profile."""

    with app.app_context():
        if not current_app.config["TMDB_API_KEY"]:
            return True
        redis = current_app.redis
        cursors = {
            field.decode(): int(value)
            for field, value in redis.hgetall(CURSORS_KEY).items()
        }

        # These are the candidates, in the order that the budget prefers.
        # The affinity people of each user come first, then the charts.
        # Both continue from the point where the last night stopped

        candidate_ids = []
        profiles = {}
        for user in User.query.all():
            profile = stored_profile(redis, user.id)
            if not profile:
                continue
            profiles[user.id] = profile
            people = _affinity_people(profile)
            if not people:
                continue
            start = cursors.get(f"people:{user.id}", 0) % len(people)
            picked = [
                people[(start + offset) % len(people)]
                for offset in range(min(WARM_PEOPLE, len(people)))
            ]
            cursors[f"people:{user.id}"] = (start + len(picked)) % len(people)
            for person_id in picked:
                candidate_ids.extend(person_film_ids(person_id))

        for chart in CHARTS:
            page = cursors.get(f"chart:{chart}", 0)
            for _ in range(CHART_PAGES_PER_NIGHT):
                page = page % CHART_PAGE_LIMIT + 1
                ids, total_pages = chart_page_ids(chart, page)
                candidate_ids.extend(ids)
                if total_pages and page >= min(total_pages, CHART_PAGE_LIMIT):
                    page = 0
            cursors[f"chart:{chart}"] = page

        candidates = list(dict.fromkeys(candidate_ids))
        redis.hset(
            CURSORS_KEY,
            mapping={field: str(value) for field, value in cursors.items()},
        )

        # Fetch the payloads that the cache lacks, 10 in parallel, in the
        # budget

        cached_flags = redis.mget(
            [ENRICHED_KEY.format(tmdb_id=tmdb_id) for tmdb_id in candidates]
        )
        to_fetch = [
            tmdb_id for tmdb_id, payload in zip(candidates, cached_flags) if not payload
        ][:WARM_FETCH_BUDGET]
        if to_fetch:
            flask_app = current_app._get_current_object()

            def warm(tmdb_id):
                with flask_app.app_context():
                    enriched_movie(tmdb_id)

            with ThreadPoolExecutor(max_workers=10) as executor:
                list(executor.map(warm, to_fetch))

        # Each warmed candidate gets the long TTL. The rolling cursor sets
        # it again before it expires

        pipeline = redis.pipeline()
        for tmdb_id in candidates:
            pipeline.expire(ENRICHED_KEY.format(tmdb_id=tmdb_id), WARM_TTL)
        pipeline.execute()

        for user_id, profile in profiles.items():
            scored = prescore_films(redis, user_id, candidates, profile)
            current_app.logger.info(
                f"Estimate warm: {len(candidates)} candidate films, "
                f"{len(to_fetch)} fetched, {scored} pre-scored for user {user_id}"
            )
        return True
