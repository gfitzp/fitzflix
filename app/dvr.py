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
    TVSeries,
    movie_genres,
    tv_file_rank,
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

# Per-series TV channels: broadcast order in a windowed slice that
# advances a few episodes each day, so the channel marches through the
# series across rebuilds instead of looping the same premiere block

TV_CHANNEL_NUMBER = 200
MIN_SERIES_EPISODES = 8
TV_WINDOW_ADVANCE = 5

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


def _series_pool(limit):
    """The series with enough owned episodes to sustain a channel,
    deepest first, capped at the configured channel count. Specials
    (season 0) don't count toward the bench."""

    rows = (
        db.session.query(
            TVSeries, db.func.count(db.func.distinct(File.season * 1000 + File.episode))
        )
        .join(File, File.series_id == TVSeries.id)
        .filter(File.season > 0)
        .group_by(TVSeries.id)
        .all()
    )
    deep = [(series, count) for series, count in rows if count >= MIN_SERIES_EPISODES]
    deep.sort(key=lambda pair: pair[1], reverse=True)
    return [series for series, _ in deep[:limit]]


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
    """The per-series channels: (number, slug, name, window) tuples,
    numbered alphabetically in their own band. Each window is a
    contiguous broadcast-order slice that starts a little further into
    the series every day."""

    cap = current_app.config["DVR_CHANNEL_FILMS"]
    pool = _series_pool(current_app.config["DVR_TV_CHANNELS"])
    channels = []
    for offset, series in enumerate(sorted(pool, key=lambda s: s.title.lower())):
        episodes = _series_episodes(series.id)
        if not episodes:
            continue
        start = (day.toordinal() * TV_WINDOW_ADVANCE) % len(episodes)
        window = (episodes + episodes)[start : start + min(cap, len(episodes))]
        channels.append(
            (
                TV_CHANNEL_NUMBER + offset,
                f"tv-{_slugify(series.title)}",
                series.title,
                series,
                window,
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

        for number, slug, name, series, window in _tv_channels(day):
            programs = []
            for file in window:
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
