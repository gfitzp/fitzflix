"""The landing page's "Streaming on your services" rail.

Candidates come from TMDb's discover endpoint — shared per-provider
pools (popular, acclaimed, and newly streaming sorts) plus taste-shaped
queries built from each user's stored profile — so discover acts as a
candidate generator, never as display truth: its provider and
monetization filters demonstrably cross-contaminate, so every film that
might be shown is verified against the per-title watch-provider cache
first. Survivors are enriched with full credits so crew features score
too, and each recommendation carries its provenance (which named query
produced it) alongside its top contributing features.

Films already in the library or in the user's diary are excluded — the
rail recommends what to watch on services already paid for, never what
to buy (that preference is physical media). The discover data and the
availability data are JustWatch's via TMDb, so the rail carries the
mandatory attribution wherever it renders.
"""

import json
import traceback

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from flask import current_app
from werkzeug.local import LocalProxy

from app import db, get_app
from app.models import File, Movie, User, UserMovieReview, UserMovieStatus
from app.recommendations import (
    CREW_ROLE_JOBS,
    TOP_BILLING_CUTOFF,
    score_movie,
    stored_profile,
)
from app.streaming import (
    batch_title_availability,
    streaming_matches,
    user_provider_ids,
)
from app.videos import tmdb_get

# This process's app instance, resolved lazily so the nightly task can
# run on a worker without building a second application

app = LocalProxy(get_app)

RAIL_KEY = "fitzflix:recs:streaming:{user_id}"
POOL_KEY = "fitzflix:rail:pool:{provider_id}"
POOL_CACHE_SECONDS = 86400
ENRICHED_KEY = "fitzflix:tmdb:movie:{tmdb_id}:enriched"
ENRICHED_CACHE_SECONDS = 7 * 86400

# Shared per-provider pool queries: (provenance tag, sort_by, vote floor).
# The acclaimed variant needs a real vote floor or obscure 10.0-rated
# shovelware tops the list; every query also carries the base hygiene
# floor and include_adult=false

POOL_SORTS = (
    ("popular", "popularity.desc", 50),
    ("acclaimed", "vote_average.desc", 200),
    ("new", "primary_release_date.desc", 50),
)
POOL_PAGES = 3

# Per-user taste-shaped queries drawn from the profile

TASTE_GENRE_PAGES = 2
TASTE_PEOPLE_LIMIT = 3
NEGATIVE_GENRE_THRESHOLD = -0.08

# How deep the rail digs: coarse-ranked candidates verified against the
# per-title availability cache, then enriched with credits. The depth
# feeds the landing page's runtime filter — a tight minute limit thins
# the rail, so it stores well past the dozen displayed (enrichment is
# week-cached, so the extra depth costs one first-night burst)

VERIFY_DEPTH = 100
ENRICH_DEPTH = 50
STORED_RAIL_ITEMS = 50


def _discover(params):
    """One discover page's results, [] on failure (the rail just gets
    shallower)."""

    try:
        r = tmdb_get(
            current_app.config["TMDB_API_URL"] + "/discover/movie",
            params={"api_key": current_app.config["TMDB_API_KEY"], **params},
            timeout=10,
        )
        r.raise_for_status()
        return r.json().get("results") or []
    except Exception:
        current_app.logger.warning(traceback.format_exc())
        return []


def _base_params(provider_ids):
    """The hygiene every rail query carries: US flatrate on the given
    providers, no adult titles, and a vote floor against shovelware."""

    return {
        "watch_region": "US",
        "with_watch_providers": "|".join(str(p) for p in sorted(provider_ids)),
        "with_watch_monetization_types": "flatrate",
        "include_adult": "false",
        "vote_count.gte": 50,
    }


def _merge(pool, items, source):
    """Fold discover items into the candidate pool, accumulating each
    film's provenance tags."""

    for item in items:
        tmdb_id = item.get("id")
        if tmdb_id is None:
            continue
        entry = pool.setdefault(tmdb_id, {"item": item, "sources": []})
        if source not in entry["sources"]:
            entry["sources"].append(source)


def provider_pool(provider_id, provider_name):
    """The shared candidate pool for one provider — popular, acclaimed,
    and newly streaming pages — cached for a day so on-demand rail
    computes don't refetch what the nightly run already pulled."""

    cache_key = POOL_KEY.format(provider_id=int(provider_id))
    cached = current_app.redis.get(cache_key)
    if cached:
        return json.loads(cached)

    pool = {}
    for tag, sort_by, vote_floor in POOL_SORTS:
        for page in range(1, POOL_PAGES + 1):
            params = _base_params([provider_id])
            params.update(
                {"sort_by": sort_by, "page": page, "vote_count.gte": vote_floor}
            )
            _merge(pool, _discover(params), f"{tag} on {provider_name}")

    if pool:
        current_app.redis.set(
            cache_key,
            json.dumps({str(k): v for k, v in pool.items()}),
            ex=POOL_CACHE_SECONDS,
        )
        return {str(k): v for k, v in pool.items()}
    return {}


def _taste_queries(profile, provider_ids):
    """(provenance tag, extra params, pages) tuples shaped by the user's
    profile: their top genres, their favorite decade, and their
    highest-affinity people — the queries that yield the crispest
    explanations."""

    affinities = profile.get("affinities", {})

    def top(cls, count):
        entries = [
            (key, entry)
            for key, entry in affinities.items()
            if entry["class"] == cls and entry["score"] > 0
        ]
        entries.sort(key=lambda pair: pair[1]["score"], reverse=True)
        return entries[:count]

    negative_genres = [
        key.split(":", 1)[1]
        for key, entry in affinities.items()
        if entry["class"] == "genre" and entry["score"] < NEGATIVE_GENRE_THRESHOLD
    ]
    exclusions = (
        {"without_genres": ",".join(negative_genres)} if negative_genres else {}
    )

    queries = []

    genres = top("genre", 2)
    if genres:
        labels = "/".join(entry["label"] for _, entry in genres)
        genre_ids = "|".join(key.split(":", 1)[1] for key, _ in genres)
        queries.append(
            (
                f"your taste in {labels}",
                {"with_genres": genre_ids, "sort_by": "popularity.desc", **exclusions},
                TASTE_GENRE_PAGES,
            )
        )

    decades = top("decade", 1)
    if decades:
        decade = int(decades[0][0].split(":", 1)[1])
        queries.append(
            (
                f"your affinity for the {decade}s",
                {
                    "primary_release_date.gte": f"{decade}-01-01",
                    "primary_release_date.lte": f"{decade + 9}-12-31",
                    "sort_by": "popularity.desc",
                    **exclusions,
                },
                1,
            )
        )

    people = [
        (key, entry)
        for cls in (
            "director",
            "actor",
            *(c for c in CREW_ROLE_JOBS if c != "director"),
        )
        for key, entry in top(cls, TASTE_PEOPLE_LIMIT)
    ]
    people.sort(key=lambda pair: pair[1]["score"], reverse=True)
    for key, entry in people[:TASTE_PEOPLE_LIMIT]:
        person_id = key.split(":", 1)[1]
        queries.append(
            (
                f"features {entry['label']}",
                {"with_people": person_id, "sort_by": "popularity.desc"},
                1,
            )
        )

    return queries


def _excluded_tmdb_ids(user_id):
    """TMDb ids the rail must never recommend: films with a local
    main-feature file, films already in this user's diary (owned or
    review-only records alike), and films they've waved off."""

    owned = db.session.query(Movie.tmdb_id).filter(
        Movie.tmdb_id.isnot(None),
        Movie.files.any(File.feature_type_id.is_(None)),
    )
    logged = (
        db.session.query(Movie.tmdb_id)
        .join(UserMovieReview, UserMovieReview.movie_id == Movie.id)
        .filter(Movie.tmdb_id.isnot(None))
        .filter(UserMovieReview.user_id == int(user_id))
    )
    refused = (
        db.session.query(Movie.tmdb_id)
        .join(UserMovieStatus, UserMovieStatus.movie_id == Movie.id)
        .filter(Movie.tmdb_id.isnot(None))
        .filter(UserMovieStatus.user_id == int(user_id))
        .filter(UserMovieStatus.kind == "not_interested")
    )
    return (
        {tmdb_id for (tmdb_id,) in owned}
        | {tmdb_id for (tmdb_id,) in logged}
        | {tmdb_id for (tmdb_id,) in refused}
    )


def _discover_features(item):
    """Coarse feature tuples straight off a discover payload item —
    genre ids, decade, language — for pre-verification ranking. Labels
    aren't needed at this stage; the display pass rescores enriched
    payloads."""

    features = []
    for genre_id in item.get("genre_ids") or []:
        features.append(("genre", f"genre:{genre_id}", str(genre_id)))
    release = item.get("release_date") or ""
    if release[:4].isdigit():
        decade = int(release[:4]) // 10 * 10
        features.append(("decade", f"decade:{decade}", f"{decade}s"))
    language = item.get("original_language")
    if language:
        features.append(
            ("language", f"language:{language}", f"{language.upper()}-language")
        )
    return features


def enriched_movie(tmdb_id):
    """The trimmed movie payload the rail scores and displays — genres,
    keywords, top cast, role-mapped crew, runtime — cached for a week
    (credits and runtimes are stable)."""

    cache_key = ENRICHED_KEY.format(tmdb_id=int(tmdb_id))
    cached = current_app.redis.get(cache_key)
    if cached:
        return json.loads(cached)
    try:
        r = tmdb_get(
            current_app.config["TMDB_API_URL"] + f"/movie/{int(tmdb_id)}",
            params={
                "api_key": current_app.config["TMDB_API_KEY"],
                "append_to_response": "credits,keywords",
            },
            timeout=10,
        )
        r.raise_for_status()
        payload = r.json() or {}
    except Exception:
        current_app.logger.warning(traceback.format_exc())
        return None

    credits = payload.get("credits") or {}
    job_to_class = {
        job: cls for cls, (jobs, _) in CREW_ROLE_JOBS.items() for job in jobs
    }
    trimmed = {
        "tmdb_id": payload.get("id"),
        "title": payload.get("title"),
        "year": (payload.get("release_date") or "")[:4],
        "poster_path": payload.get("poster_path"),
        "runtime": payload.get("runtime"),
        "overview": payload.get("overview"),
        "original_language": payload.get("original_language"),
        "genres": [
            {"id": g.get("id"), "name": g.get("name")}
            for g in payload.get("genres") or []
        ],
        "keywords": [
            {"id": k.get("id"), "name": k.get("name")}
            for k in (payload.get("keywords") or {}).get("keywords") or []
        ],
        "cast": [
            {"id": c.get("id"), "name": c.get("name")}
            for c in (credits.get("cast") or [])[:TOP_BILLING_CUTOFF]
        ],
        "crew": [
            {"id": c.get("id"), "name": c.get("name"), "job": c.get("job")}
            for c in credits.get("crew") or []
            if c.get("job") in job_to_class
        ],
    }
    current_app.redis.set(cache_key, json.dumps(trimmed), ex=ENRICHED_CACHE_SECONDS)
    return trimmed


def _payload_features(payload):
    """Full feature tuples from an enriched payload, in the exact key
    space the taste profile uses — so library films and streaming
    candidates score on the same affinities, crew roles included."""

    features = []
    for genre in payload.get("genres") or []:
        features.append(("genre", f"genre:{genre['id']}", genre["name"]))
    year = payload.get("year") or ""
    if str(year).isdigit():
        decade = int(year) // 10 * 10
        features.append(("decade", f"decade:{decade}", f"{decade}s"))
    language = payload.get("original_language")
    if language:
        features.append(
            ("language", f"language:{language}", f"{language.upper()}-language")
        )
    for keyword in payload.get("keywords") or []:
        features.append(("keyword", f"keyword:{keyword['id']}", keyword["name"]))
    for actor in payload.get("cast") or []:
        features.append(("actor", f"actor:{actor['id']}", actor["name"]))
    job_to_class = {
        job: cls for cls, (jobs, _) in CREW_ROLE_JOBS.items() for job in jobs
    }
    for person in payload.get("crew") or []:
        cls = job_to_class[person["job"]]
        label = CREW_ROLE_JOBS[cls][1].format(name=person["name"])
        features.append((cls, f"{cls}:{person['id']}", label))

    seen = set()
    unique = []
    for row in features:
        if row[1] not in seen:
            seen.add(row[1])
            unique.append(row)
    return unique


def compute_user_rail(user):
    """The ranked streaming rail for one user, or None when they lack a
    taste profile or provider picks.

    Pipeline: shared provider pools + taste-shaped queries -> coarse
    affinity ranking -> per-title availability verification (discover
    lies; the watch-provider cache doesn't) -> credits enrichment and
    full-feature rescoring of the survivors.
    """

    profile = stored_profile(current_app.redis, user.id)
    provider_ids = user_provider_ids(user)
    if not profile or not provider_ids:
        return None

    provider_names = {row.provider_id: row.name for row in user.streaming_providers}

    pool = {}
    for provider_id in sorted(provider_ids):
        shared = provider_pool(
            provider_id, provider_names.get(provider_id) or f"provider {provider_id}"
        )
        for tmdb_id, entry in shared.items():
            _merge(pool, [entry["item"]], entry["sources"][0])
            for source in entry["sources"][1:]:
                _merge(pool, [entry["item"]], source)

    for tag, extra_params, pages in _taste_queries(profile, provider_ids):
        for page in range(1, pages + 1):
            params = {**_base_params(provider_ids), **extra_params, "page": page}
            _merge(pool, _discover(params), tag)

    excluded = _excluded_tmdb_ids(user.id)
    ranked = []
    for tmdb_id, entry in pool.items():
        tmdb_id = int(tmdb_id)
        if tmdb_id in excluded:
            continue
        score, _ = score_movie(_discover_features(entry["item"]), profile)
        popularity = entry["item"].get("popularity") or 0.0
        ranked.append((score, popularity, tmdb_id, entry))
    ranked.sort(key=lambda row: (row[0], row[1]), reverse=True)

    # Verification: only films genuinely streaming on this user's
    # services survive (day-cached per title, shared with page views)

    candidates = ranked[:VERIFY_DEPTH]
    availability, _ = batch_title_availability(
        [tmdb_id for _, _, tmdb_id, _ in candidates]
    )
    verified = []
    for score, popularity, tmdb_id, entry in candidates:
        matches = streaming_matches(availability.get(tmdb_id), provider_ids)
        if matches:
            verified.append((tmdb_id, entry, matches))
        if len(verified) == ENRICH_DEPTH:
            break

    # Enrichment: full credits so crew affinities score, then the final
    # ranking with human-readable explanations

    flask_app = current_app._get_current_object()

    def enrich(tmdb_id):
        """One enriched payload under its own app context."""

        with flask_app.app_context():
            return tmdb_id, enriched_movie(tmdb_id)

    payloads = {}
    with ThreadPoolExecutor(max_workers=10) as executor:
        for tmdb_id, payload in executor.map(
            enrich, [tmdb_id for tmdb_id, _, _ in verified]
        ):
            payloads[tmdb_id] = payload

    items = []
    for tmdb_id, entry, matches in verified:
        payload = payloads.get(tmdb_id)
        if not payload:
            continue
        score, contributions = score_movie(_payload_features(payload), profile)
        because = [entry["sources"][0]]
        because += [
            label for contribution, label in contributions[:3] if contribution > 0
        ]
        items.append(
            {
                "tmdb_id": tmdb_id,
                "title": payload.get("title"),
                "year": payload.get("year"),
                "poster_path": payload.get("poster_path"),
                "runtime": payload.get("runtime"),
                "providers": matches,
                "because": because[:4],
                "score": round(score, 4),
            }
        )

    items.sort(key=lambda item: item["score"], reverse=True)
    return items[:STORED_RAIL_ITEMS]


def recompute_streaming_rail():
    """Nightly task: rebuild the streaming rail for every user with a
    taste profile and provider picks, into Redis for the landing page."""

    with app.app_context():
        computed_at = datetime.now().strftime("%Y-%m-%d %H:%M")
        for user in User.query.all():
            if not user.streaming_providers.count():
                continue
            items = compute_user_rail(user)
            if items is None:
                continue
            current_app.redis.set(
                RAIL_KEY.format(user_id=user.id),
                json.dumps({"computed_at": computed_at, "items": items}),
            )
            current_app.logger.info(
                f"Streaming rail: stored {len(items)} films for user {user.id}"
            )
        return True


def stored_rail(redis, user_id):
    """The nightly recompute's stored rail for a user, or None."""

    payload = redis.get(RAIL_KEY.format(user_id=int(user_id)))
    return json.loads(payload) if payload else None
