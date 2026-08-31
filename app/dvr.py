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
from app.models import File, Movie, RefQuality, FileAudioTrack, TMDBGenre, movie_genres

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


def build_channel_lineups(day=None):
    """Build the day's channel lineups and store them in Redis: the
    all-library mix plus the deepest genres, each a seeded shuffle of
    its eligible films capped at DVR_CHANNEL_FILMS.

    Runs nightly on the maintenance queue. Probing durations is the
    only real cost, and the per-file cache makes every build after a
    film's first appearance free.
    """

    with app.app_context():
        redis_client = current_app.redis
        day = day or date.today()
        cap = current_app.config["DVR_CHANNEL_FILMS"]

        best = _best_files_by_movie()
        genres_by_movie = _genre_names_by_movie(list(best))

        # Channels: the mix, then the deepest genres alphabetically

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

        channels = [(MIX_CHANNEL_NUMBER, MIX_CHANNEL_SLUG, MIX_CHANNEL_NAME, None)]
        for offset, name in enumerate(top, start=1):
            channels.append((MIX_CHANNEL_NUMBER + offset, _slugify(name), name, name))

        epoch = datetime.now(timezone.utc).timestamp()
        index = []
        for number, slug, name, genre in channels:
            eligible = [
                movie_id
                for movie_id in best
                if genre is None or genre in genres_by_movie.get(movie_id, [])
            ]
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
                programs.append(
                    _program(movie, file, duration, genres_by_movie.get(movie_id, []))
                )

            if not programs:
                current_app.logger.warning(f"DVR: channel {slug} has no programs")
                continue
            lineup = {
                "slug": slug,
                "name": name,
                "number": number,
                "epoch": epoch,
                "programs": programs,
            }
            redis_client.set(LINEUP_KEY.format(slug=slug), json.dumps(lineup))
            index.append({"number": number, "slug": slug, "name": name})

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
