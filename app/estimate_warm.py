"""Nightly estimate pre-warming for the shared score source's tmdb lane.

Glenn's call (Aug 2026): the TMDB API costs nothing but latency, so
spend a nightly budget warming the enriched payloads tomorrow's
browsing will want — the filmographies of the people his taste
profile ranks highest, plus TMDB's popular and top-rated charts — and
pre-score everything into the tmdb overlay, so tiles paint estimates
instantly instead of filling on the fly.

Cursors roll nightly: each night resumes where the last stopped,
expanding the radius through the affinity people and deeper into the
chart pages, and a source that wraps around re-warms its films — which
re-stamps their month-long TTL, so tracked films are effectively
always cached while one-off on-demand payloads still age out at the
enrichment cache's own week.

Runs after the 1:45 recompute (which drops the overlays) and the 2:15
rail rebuild, so the pre-scores derive against the fresh profile and
the rail's own enrichments are already in cache to piggyback on.
"""

import json
import traceback

from concurrent.futures import ThreadPoolExecutor

from flask import current_app
from werkzeug.local import LocalProxy

from app import db, get_app
from app.models import Movie, User, tmdb_get
from app.recommendations import (
    CREW_ROLE_JOBS,
    PATCH_SCORES_TTL,
    TMDB_PATCH_SCORES_KEY,
    _tmdb_copref,
    score_movie,
    stored_profile,
)
from app.streaming_rail import ENRICHED_KEY, _payload_features, enriched_movie

# This process's app instance, resolved lazily so the nightly task can
# run on a worker without building a second application

app = LocalProxy(get_app)

CURSORS_KEY = "fitzflix:estimates:warm:cursors"

# Enriched payload fetches per night — the budget is latency, not
# money, so it's sized generously: 2,000 at the ten-abreast pace is
# a few minutes of queue time

WARM_FETCH_BUDGET = 2000

# Warmed payloads hold a month; the rolling cursor re-warms tracked
# films before that, so in practice they never expire

WARM_TTL = 30 * 86400

# Affinity people warmed per user per night, and chart pages consumed
# per chart per night; TMDB stops paging charts at 500

WARM_PEOPLE = 40
CHART_PAGES_PER_NIGHT = 10
CHART_PAGE_LIMIT = 500
CHARTS = ("popular", "top_rated")

PERSON_CLASSES = ("actor",) + tuple(CREW_ROLE_JOBS)


def _affinity_people(profile):
    """The profile's people, strongest affinity first — the careers the
    user is most likely to browse. A person credited in more than one
    class (actor and director) counts once, at their best score."""

    best = {}
    for key, entry in (profile or {}).get("affinities", {}).items():
        cls, _, raw = key.partition(":")
        if cls in PERSON_CLASSES and raw.isdigit():
            person_id = int(raw)
            score = entry.get("score", 0)
            if score > best.get(person_id, float("-inf")):
                best[person_id] = score
    return [person_id for person_id, _ in sorted(best.items(), key=lambda kv: -kv[1])]


def person_film_ids(person_id):
    """The tmdb ids across one person's career, through the same
    day-cached credits payload the filmography page reads — so a warm
    here also spares that page its fetch."""

    cache_key = f"fitzflix:tmdb:person:{int(person_id)}:credits"
    cached = current_app.redis.get(cache_key)
    if cached:
        credits = json.loads(cached)
        if isinstance(credits, list):
            credits = {"cast": credits, "crew": []}
    else:
        role_jobs = {job for jobs, _ in CREW_ROLE_JOBS.values() for job in jobs}
        try:
            r = tmdb_get(
                current_app.config["TMDB_API_URL"]
                + f"/person/{int(person_id)}/movie_credits",
                params={"api_key": current_app.config["TMDB_API_KEY"]},
                timeout=10,
            )
            r.raise_for_status()
            payload = r.json()
        except Exception:
            current_app.logger.warning(traceback.format_exc())
            return []
        credits = {
            "cast": payload.get("cast") or [],
            "crew": [
                crew_credit
                for crew_credit in payload.get("crew") or []
                if crew_credit.get("job") in role_jobs
            ],
        }
        current_app.redis.set(cache_key, json.dumps(credits), ex=86400)
    return [
        entry["id"]
        for entry in (credits.get("cast") or []) + (credits.get("crew") or [])
        if entry.get("id")
    ]


def chart_page_ids(chart, page):
    """(tmdb ids, total pages) for one page of a TMDB chart."""

    try:
        r = tmdb_get(
            current_app.config["TMDB_API_URL"] + f"/movie/{chart}",
            params={"api_key": current_app.config["TMDB_API_KEY"], "page": page},
            timeout=10,
        )
        r.raise_for_status()
        payload = r.json()
    except Exception:
        current_app.logger.warning(traceback.format_exc())
        return [], None
    ids = [result["id"] for result in payload.get("results") or [] if result.get("id")]
    return ids, payload.get("total_pages")


def prescore_films(redis, user_id, tmdb_ids, profile):
    """Score cached payloads straight into the user's tmdb overlay —
    the same taste-plus-copref recipe resolved_tmdb_score runs, done
    ahead of time so the first tile view reads a finished number.
    Films with local records sit out; the movie-id lane owns them."""

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
            # A cached null is a deleted TMDB id — present so it isn't
            # re-fetched, but nothing to score
            data = json.loads(payload) if payload else None
            if not data:
                continue
            taste, _ = score_movie(_payload_features(data), profile)
            mapping[str(tmdb_id)] = round(taste + _tmdb_copref(user_id, tmdb_id), 4)
        if mapping:
            redis.hset(key, mapping=mapping)
            scored += len(mapping)
    if scored:
        redis.expire(key, PATCH_SCORES_TTL)
    return scored


def warm_estimates():
    """Nightly task: warm tomorrow's estimate payloads and pre-score
    them into each profiled user's tmdb overlay."""

    with app.app_context():
        if not current_app.config["TMDB_API_KEY"]:
            return True
        redis = current_app.redis
        cursors = {
            field.decode(): int(value)
            for field, value in redis.hgetall(CURSORS_KEY).items()
        }

        # Candidates, in the order the budget should favor them: each
        # user's affinity people first, then the charts — both rolling
        # from where last night stopped

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

        # Fetch what the cache lacks, ten abreast under the budget

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

        # Every warmed candidate holds the long TTL — the rolling
        # cursor re-stamps it before it runs out

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
