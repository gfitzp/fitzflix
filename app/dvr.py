"""Virtual DVR channels (#182): lineups and schedule math.

The library becomes a handful of 24/7 "live" channels that Plex tunes
as an M3U tuner. A channel is a frozen, ordered program list plus an
epoch timestamp stored in Redis; the schedule is pure arithmetic over
the cumulative program durations, repeating from the epoch forever, so
the XMLTV guide and the stream compute "what's on at time T" from the
same snapshot and can never disagree. Nothing runs and no file is
opened until Plex actually tunes a stream URL.

Program durations must be the real container durations — scheduling
from TMDB's runtime would drift the stream away from the guide by the
accumulated error — so the build probes each file once with ffprobe
and caches the result in Redis keyed by file id (a file row is never
mutated in place, so the cache needs no invalidation).

The lineup build is the only expensive part and runs as a nightly
maintenance task, rotating lineups the way the landing-page shelves
rotate. The guide Plex holds between its own refreshes can lag a
rebuild by a few hours; the stream is always right, the label may be
stale — authentically cable.
"""

import json
import os
import re
import subprocess

from datetime import date, datetime, timezone
from random import Random

from flask import current_app
from werkzeug.local import LocalProxy

from app import db, get_app
from app.availability_alerts import _leaving_set
from app.leaving_criterion import CRITERION_PROVIDER_ID
from app.models import (
    File,
    FileAudioTrack,
    Movie,
    RefQuality,
    TMDBGenre,
    TMDBKeyword,
    TMDBNetwork,
    TVSeries,
    movie_genres,
    tv_file_rank,
    tv_genres,
    tv_keywords,
    tv_networks,
)
from app.streaming import batch_title_availability, streaming_matches

# This process's app instance, resolved lazily so the nightly task can
# run on a worker without building a second application

app = LocalProxy(get_app)

CHANNELS_KEY = "fitzflix:dvr:channels"
LINEUP_KEY = "fitzflix:dvr:lineup:{slug}"
DURATIONS_KEY = "fitzflix:dvr:durations"

# Channel numbering: the all-library mix leads, genre channels follow
# alphabetically. Numbers restate themselves every build, so a genre
# arriving or leaving the top-N renumbers its neighbours — harmless,
# Plex maps channels by tvg-id, not number

MIX_CHANNEL_NUMBER = 100
MIX_CHANNEL_NAME = "Fitzflix Mix"
MIX_CHANNEL_SLUG = "fitzflix-mix"

# A genre needs a real bench to sustain a channel; below this it just
# feeds the mix

MIN_GENRE_FILMS = 8

# Themed channels built from external signals (Criterion availability,
# the leaving set) sit in their own number band and can run much
# shallower — a three-film last-call marathon is authentically cable

CRITERION_CHANNEL_NUMBER = 140
LEAVING_CHANNEL_NUMBER = 141
MIN_SPECIAL_FILMS = 3

# TV channels are genre- and theme-based, not per-series: several
# series share a channel, airing in short interleaved blocks like
# syndication. Auto genre channels (200+) come from the deepest TMDB
# TV genres; themed channels (240+) come from the spec table below.
# Each series' slot share is proportional to its episode depth, and
# its cursor advances through broadcast order day over day

TV_CHANNEL_NUMBER = 200
TV_THEME_NUMBER = 240
MIN_SERIES_EPISODES = 8
TV_BLOCK = 2

# Themed TV channels. A series belongs when it satisfies EVERY
# predicate the spec declares — "genre" (TMDB TV genre name),
# "keywords" (any-of, lowercase TMDB keywords), "network_country"
# (any network registered to that country) — OR when its title
# contains a "titles" pin (for series whose TMDB metadata is too thin
# to match otherwise, e.g. Match Game PM carries no keywords at all)

TV_THEME_SPECS = (
    {
        "name": "Game Shows",
        "keywords": ("game show", "quiz show", "panel show"),
        "titles": ("match game",),
    },
    {
        "name": "British Sitcoms",
        "keywords": ("sitcom",),
        "network_country": "GB",
    },
)

# AC-3 tops out at 5.1; sources with more channels downmix, sources
# with fewer keep their layout

AC3_MAX_CHANNELS = 6


def _slugify(name):
    """The channel id used in the M3U tvg-id, the XMLTV channel id, and
    the stream URL: lowercase, runs of non-alphanumerics collapsed to
    hyphens."""

    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _probe_duration(file_path):
    """The container duration in seconds, or None — a header read, not
    a full scan."""

    try:
        result = subprocess.run(
            [
                current_app.config["FFPROBE_BIN"],
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "csv=p=0",
                file_path,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return float(result.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _cached_duration(redis_client, file):
    """The file's real duration in seconds, probing on first sight and
    caching by file id thereafter; None when the probe fails."""

    cached = redis_client.hget(DURATIONS_KEY, str(file.id))
    if cached is not None:
        try:
            return float(cached)
        except ValueError:
            pass
    duration = _probe_duration(
        os.path.join(current_app.config["LIBRARY_DIR"], file.file_path)
    )
    if duration and duration > 0:
        redis_client.hset(DURATIONS_KEY, str(file.id), f"{duration:.3f}")
        return duration
    return None


def _first_audio_channels(file_id):
    """How many channels the file's first audio track carries, capped
    at AC-3's 5.1 ceiling; 6 when the scan recorded nothing usable.

    The first track is always the default track (the house rule), so
    it is the one the stream maps.
    """

    track = (
        FileAudioTrack.query.filter_by(file_id=file_id)
        .order_by(FileAudioTrack.track.asc())
        .first()
    )
    if track and track.channels:
        match = re.search(r"\d+", str(track.channels))
        if match:
            return min(int(match.group()), AC3_MAX_CHANNELS)
    return AC3_MAX_CHANNELS


def _best_files_by_movie():
    """Each owned movie's best main-feature copy: never a fullscreen
    copy while a widescreen one exists, then best quality — the same
    ranking the movie cards use."""

    rows = (
        db.session.query(Movie, File)
        .join(File, File.movie_id == Movie.id)
        .join(RefQuality, RefQuality.id == File.quality_id)
        .filter(File.feature_type_id == None)  # noqa: E711
        .order_by(Movie.id, File.fullscreen.asc(), RefQuality.preference.desc())
        .all()
    )
    best = {}
    for movie, file in rows:
        best.setdefault(movie.id, (movie, file))
    return best


def _genre_names_by_movie(movie_ids):
    """Map of movie id -> list of genre names, one query for the whole
    candidate pool."""

    if not movie_ids:
        return {}
    rows = (
        db.session.query(movie_genres.c.movie_id, TMDBGenre.name)
        .join(TMDBGenre, TMDBGenre.id == movie_genres.c.genre_id)
        .filter(movie_genres.c.movie_id.in_(movie_ids))
        .all()
    )
    names = {}
    for movie_id, name in rows:
        names.setdefault(movie_id, []).append(name)
    return names


def _program(movie, file, duration, genres):
    """The stored program record: everything the guide and the stream
    need, so neither ever touches the database."""

    return {
        "movie_id": movie.id,
        "tmdb_id": movie.tmdb_id,
        "title": movie.tmdb_title or movie.title,
        "year": movie.year,
        "overview": movie.tmdb_overview or "",
        "poster_path": movie.tmdb_poster_path,
        "file_path": file.file_path,
        "duration": round(duration, 3),
        "audio_channels": _first_audio_channels(file.id),
        "genres": genres,
    }


def _criterion_movie_ids(best):
    """Owned films streaming on the Criterion Channel right now, per
    the availability cache. Cache-only reads: the nightly refresh keeps
    every owned film's payload warm, and a cold entry just waits for
    the next build."""

    by_tmdb = {}
    for movie_id, (movie, _) in best.items():
        if movie.tmdb_id:
            by_tmdb.setdefault(movie.tmdb_id, movie_id)
    if not by_tmdb:
        return []
    payloads, _ = batch_title_availability(list(by_tmdb), fetch_limit=0)
    return [
        by_tmdb[tmdb_id]
        for tmdb_id, payload in payloads.items()
        if payload and streaming_matches(payload, {CRITERION_PROVIDER_ID})
    ]


def _leaving_owned(best):
    """Owned films in the current leaving-Criterion set, plus the
    departure date. The channel airs OUR copies — the files aren't
    going anywhere, the streaming availability is, so "leaving" is a
    last-call programming cue, not a storage fact."""

    leaving, departs = _leaving_set()
    if not leaving:
        return [], None
    ids = [
        movie_id for movie_id, (movie, _) in best.items() if movie.tmdb_id in leaving
    ]
    return ids, departs


def _series_catalog():
    """Every series with owned regular episodes, annotated with its
    episode count, TMDB genre names, lowercase keywords, and network
    countries — the pool every TV channel selects from, built in four
    queries."""

    counts = dict(
        db.session.query(
            File.series_id,
            db.func.count(db.func.distinct(File.season * 1000 + File.episode)),
        )
        .filter(File.series_id != None)  # noqa: E711
        .filter(File.season > 0)
        .group_by(File.series_id)
        .all()
    )
    if not counts:
        return {}
    catalog = {
        series.id: {
            "series": series,
            "episodes": counts[series.id],
            "genres": set(),
            "keywords": set(),
            "countries": set(),
        }
        for series in TVSeries.query.filter(TVSeries.id.in_(counts)).all()
    }
    for tv_id, name in (
        db.session.query(tv_genres.c.tv_id, TMDBGenre.name)
        .join(TMDBGenre, TMDBGenre.id == tv_genres.c.genre_id)
        .filter(tv_genres.c.tv_id.in_(catalog))
        .all()
    ):
        catalog[tv_id]["genres"].add(name)
    for tv_id, name in (
        db.session.query(tv_keywords.c.tv_id, TMDBKeyword.name)
        .join(TMDBKeyword, TMDBKeyword.id == tv_keywords.c.keyword_id)
        .filter(tv_keywords.c.tv_id.in_(catalog))
        .all()
    ):
        catalog[tv_id]["keywords"].add((name or "").lower())
    for tv_id, country in (
        db.session.query(tv_networks.c.tv_id, TMDBNetwork.origin_country)
        .join(TMDBNetwork, TMDBNetwork.id == tv_networks.c.network_id)
        .filter(tv_networks.c.tv_id.in_(catalog))
        .all()
    ):
        if country:
            catalog[tv_id]["countries"].add(country)
    return catalog


def _theme_matches(entry, spec):
    """Whether a series belongs on a themed channel: a title pin wins
    outright, otherwise every predicate the spec declares must hold."""

    title = entry["series"].title.lower()
    if any(pin in title for pin in spec.get("titles", ())):
        return True
    if spec.get("genre") and spec["genre"] not in entry["genres"]:
        return False
    keywords = spec.get("keywords")
    if keywords and not any(keyword in entry["keywords"] for keyword in keywords):
        return False
    country = spec.get("network_country")
    if country and country not in entry["countries"]:
        return False
    return True


def _channel_window(members, day, cap):
    """The day's program window for a multi-series channel, as
    (series, file) pairs: each series gets slots proportional to its
    depth, aired as short interleaved blocks like syndication, and its
    cursor starts a little further into broadcast order every day."""

    members = sorted(members, key=lambda m: m["series"].title.lower())
    total = sum(m["episodes"] for m in members)
    cursors = []
    for member in members:
        episodes = _series_episodes(member["series"].id)
        if not episodes:
            continue
        quota = min(len(episodes), max(1, round(cap * len(episodes) / total)))
        start = (day.toordinal() * quota) % len(episodes)
        cursors.append([member["series"], episodes[start:] + episodes[:start], quota])

    window = []
    while len(window) < cap:
        progressed = False
        for cursor in cursors:
            series, rotated, remaining = cursor
            if remaining <= 0:
                continue
            take = min(TV_BLOCK, remaining, cap - len(window))
            for _ in range(take):
                window.append((series, rotated.pop(0)))
            cursor[2] = remaining - take
            progressed = True
            if len(window) >= cap:
                break
        if not progressed:
            break
    return window


def _series_episodes(series_id):
    """The series' best copy of every regular episode in broadcast
    order — the same per-episode quality ranking the library pages
    use, specials excluded."""

    ranked = (
        db.session.query(File.id, tv_file_rank())
        .join(TVSeries, TVSeries.id == File.series_id)
        .join(RefQuality, RefQuality.id == File.quality_id)
        .subquery()
    )
    return (
        File.query.join(ranked, ranked.c.id == File.id)
        .filter(File.series_id == series_id)
        .filter(File.season > 0)
        .filter(ranked.c.rank == 1)
        .order_by(File.season.asc(), File.episode.asc())
        .all()
    )


def _episode_program(series, file, duration):
    """The stored program record for one episode: the series carries
    the artwork, overview, and guide title; the file carries the
    numbering and the optional episode title (File.edition, the house
    convention)."""

    span = f"S{file.season:02d}E{file.episode:02d}"
    if file.last_episode and file.last_episode != file.episode:
        span = f"{span}-E{file.last_episode:02d}"
    return {
        "series_id": series.id,
        "tmdb_id": series.tmdb_id,
        "title": series.title,
        "subtitle": file.edition or None,
        "episode_num": span,
        "year": (
            series.tmdb_first_air_date.year if series.tmdb_first_air_date else None
        ),
        "overview": series.tmdb_overview or "",
        "poster_path": series.tmdb_poster_path,
        "file_path": file.file_path,
        "duration": round(duration, 3),
        "audio_channels": _first_audio_channels(file.id),
        "genres": [],
    }


def _tv_channels(day):
    """The TV dial: (number, slug, name, window) tuples — the deepest
    TMDB TV genres as auto channels, then the themed channels from
    TV_THEME_SPECS, each window a multi-series interleave. A channel
    needs MIN_SERIES_EPISODES owned episodes across its members."""

    cap = current_app.config["DVR_CHANNEL_EPISODES"]
    catalog = _series_catalog()
    if not catalog:
        return []

    totals = {}
    for entry in catalog.values():
        for name in entry["genres"]:
            totals[name] = totals.get(name, 0) + entry["episodes"]
    deep = [name for name, count in totals.items() if count >= MIN_SERIES_EPISODES]
    top = sorted(
        sorted(deep, key=totals.get, reverse=True)[
            : current_app.config["DVR_TV_CHANNELS"]
        ]
    )

    channels = []
    for offset, name in enumerate(top):
        members = [e for e in catalog.values() if name in e["genres"]]
        # "TV" suffix so the guide never shows a movie genre channel
        # and a TV genre channel under the same name (Comedy collides)
        channels.append(
            (
                TV_CHANNEL_NUMBER + offset,
                f"tv-{_slugify(name)}",
                f"{name} TV",
                _channel_window(members, day, cap),
            )
        )
    for offset, spec in enumerate(TV_THEME_SPECS):
        members = [e for e in catalog.values() if _theme_matches(e, spec)]
        if sum(m["episodes"] for m in members) < MIN_SERIES_EPISODES:
            continue
        channels.append(
            (
                TV_THEME_NUMBER + offset,
                f"tv-{_slugify(spec['name'])}",
                spec["name"],
                _channel_window(members, day, cap),
            )
        )
    return channels


def build_channel_lineups(day=None):
    """Build the day's channel lineups and store them in Redis: the
    all-library mix, the deepest genres, the Criterion and Leaving
    Soon overlays (when deep enough), and the per-series TV channels.
    Movie channels are seeded shuffles capped at DVR_CHANNEL_FILMS; TV
    channels are broadcast-order windows.

    Runs nightly on the maintenance queue. Probing durations is the
    only real cost, and the per-file cache makes every build after a
    file's first appearance free.
    """

    with app.app_context():
        redis_client = current_app.redis
        day = day or date.today()
        cap = current_app.config["DVR_CHANNEL_FILMS"]

        best = _best_files_by_movie()
        genres_by_movie = _genre_names_by_movie(list(best))

        # Movie channels: the mix, the deepest genres alphabetically,
        # then the themed overlays when they have the bench for it.
        # Each is (number, slug, name, eligible movie ids, guide note)

        counts = {}
        for movie_id in best:
            for name in genres_by_movie.get(movie_id, []):
                counts[name] = counts.get(name, 0) + 1
        deep = [name for name, count in counts.items() if count >= MIN_GENRE_FILMS]
        top = sorted(
            sorted(deep, key=counts.get, reverse=True)[
                : current_app.config["DVR_GENRE_CHANNELS"]
            ]
        )

        movie_channels = [
            (MIX_CHANNEL_NUMBER, MIX_CHANNEL_SLUG, MIX_CHANNEL_NAME, list(best), None)
        ]
        for offset, name in enumerate(top, start=1):
            eligible = [
                movie_id
                for movie_id in best
                if name in genres_by_movie.get(movie_id, [])
            ]
            movie_channels.append(
                (MIX_CHANNEL_NUMBER + offset, _slugify(name), name, eligible, None)
            )

        criterion_ids = _criterion_movie_ids(best)
        if len(criterion_ids) >= MIN_SPECIAL_FILMS:
            movie_channels.append(
                (
                    CRITERION_CHANNEL_NUMBER,
                    "criterion",
                    "Criterion",
                    criterion_ids,
                    None,
                )
            )

        leaving_ids, departs = _leaving_owned(best)
        if departs and len(leaving_ids) >= MIN_SPECIAL_FILMS:
            movie_channels.append(
                (
                    LEAVING_CHANNEL_NUMBER,
                    "leaving-soon",
                    "Leaving Soon",
                    leaving_ids,
                    f"Leaving the Criterion Channel {departs.strftime('%B %-d')}.",
                )
            )

        epoch = datetime.now(timezone.utc).timestamp()
        index = []

        def store(number, slug, name, programs):
            """Write one channel's lineup and index it, dropping empty
            channels."""

            if not programs:
                current_app.logger.warning(f"DVR: channel {slug} has no programs")
                return
            lineup = {
                "slug": slug,
                "name": name,
                "number": number,
                "epoch": epoch,
                "programs": programs,
            }
            redis_client.set(LINEUP_KEY.format(slug=slug), json.dumps(lineup))
            index.append({"number": number, "slug": slug, "name": name})

        for number, slug, name, eligible, note in movie_channels:
            Random(f"dvr:{slug}:{day.isoformat()}").shuffle(eligible)
            programs = []
            for movie_id in eligible:
                if len(programs) >= cap:
                    break
                movie, file = best[movie_id]
                duration = _cached_duration(redis_client, file)
                if duration is None:
                    current_app.logger.warning(
                        f"DVR: no duration for {file.file_path}; skipping"
                    )
                    continue
                program = _program(
                    movie, file, duration, genres_by_movie.get(movie_id, [])
                )
                if note:
                    program["overview"] = f"{note} {program['overview']}".strip()
                programs.append(program)
            store(number, slug, name, programs)

        for number, slug, name, window in _tv_channels(day):
            programs = []
            for series, file in window:
                duration = _cached_duration(redis_client, file)
                if duration is None:
                    current_app.logger.warning(
                        f"DVR: no duration for {file.file_path}; skipping"
                    )
                    continue
                programs.append(_episode_program(series, file, duration))
            store(number, slug, name, programs)

        redis_client.set(CHANNELS_KEY, json.dumps(index))
        current_app.logger.info(
            f"DVR: built {len(index)} channel lineups "
            f"({len(best)} owned films in pool)"
        )
        return True


def channel_index(redis_client):
    """The stored channel list ([{number, slug, name}, ...]), or an
    empty list before the first build."""

    stored = redis_client.get(CHANNELS_KEY)
    return json.loads(stored) if stored else []


def channel_lineup(redis_client, slug):
    """The stored lineup for one channel, or None."""

    stored = redis_client.get(LINEUP_KEY.format(slug=slug))
    return json.loads(stored) if stored else None


def program_at(lineup, when):
    """What the channel is playing at the given epoch timestamp: the
    program index and how many seconds into it the moment falls.

    The lineup repeats from its epoch forever, so this is position
    arithmetic over the cumulative durations — no state, no drift.
    """

    total = sum(program["duration"] for program in lineup["programs"])
    position = (when - lineup["epoch"]) % total
    for index, program in enumerate(lineup["programs"]):
        if position < program["duration"]:
            return index, position
        position -= program["duration"]
    return 0, 0.0


def programs_between(lineup, start, stop):
    """The channel's airings overlapping [start, stop): yields
    (start_ts, stop_ts, program) tuples, cycling the lineup as needed.
    This is what the XMLTV guide renders."""

    index, offset = program_at(lineup, start)
    cursor = start - offset
    while cursor < stop:
        program = lineup["programs"][index]
        yield cursor, cursor + program["duration"], program
        cursor += program["duration"]
        index = (index + 1) % len(lineup["programs"])
