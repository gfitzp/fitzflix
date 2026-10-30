"""Virtual DVR channels (#182): lineups and schedule math.

The library becomes a small number of 24/7 "live" channels. Plex tunes
them as an M3U tuner. A channel is a frozen, ordered program list and
an epoch timestamp stored in Redis. The schedule is pure arithmetic
over the cumulative program durations. It repeats from the epoch
without end. Thus, the XMLTV guide and the stream compute "what is on
at time T" from the same snapshot. They can never disagree. Nothing
runs and no file is opened until Plex tunes a stream URL.

The program durations must be the real container durations. A
schedule from the TMDB runtime would drift the stream away from the
guide by the accumulated error. Thus, the build probes each file 1
time with ffprobe. It caches the result in Redis, keyed by file id. A
file row is never changed in place. Thus, the cache needs no
invalidation.

The lineup build is the only expensive part. It runs as a nightly
maintenance task. It rotates the lineups in the same way that the
landing-page shelves rotate. The guide that Plex holds between its own
refreshes can be some hours behind a rebuild. The stream is always
correct. The label can be stale. This is authentically cable.
"""

import json
import os
import re
import subprocess
import uuid

from datetime import date, datetime, timezone
from fractions import Fraction
from math import floor, gcd
from random import Random

from flask import current_app
from werkzeug.local import LocalProxy

from app.tasks import enqueue_task
from app import db, get_app
from app.availability_alerts import _leaving_set
from app.settings import get_setting
from app.file_ranking import MOVIE_BEST_ORDER, TV_BEST_ORDER
from app.leaving_criterion import CRITERION_PROVIDER_ID
from app.maintenance import volume_alive
from app.models import (
    DVRChannel,
    File,
    FileAudioTrack,
    Movie,
    RefQuality,
    TMDBGenre,
    TMDBKeyword,
    TMDBNetwork,
    TVSeries,
    movie_genres,
    movie_keywords,
    tv_genres,
    tv_keywords,
    tv_networks,
)
from app.streaming import batch_title_availability, streaming_matches

# The app instance of this process. Fitzflix resolves it lazily. Thus,
# the nightly task can run on a worker without a second application.

app = LocalProxy(get_app)

CHANNELS_KEY = "fitzflix:dvr:channels"
LINEUP_KEY = "fitzflix:dvr:lineup:{slug}"
DURATIONS_KEY = "fitzflix:dvr:durations"

# The series cursors of each channel lived in this hash before #627.
# The first build after the deploy reads it for a lineup with no
# cursors. Delete the key, this constant and _legacy_cursors after it.

LEGACY_CURSORS_KEY = "fitzflix:dvr:cursors:{slug}"
REBUILD_FUNC = "app.dvr.build_channel_lineups"
REBUILD_JOB_ID = "dvr-lineup-rebuild"

# Channel numbering. The all-library mix comes first. The genre
# channels follow in alphabetical order. The numbers are set again on
# each build. Thus, a genre that enters or leaves the top-N renumbers
# its neighbours. This is harmless. Plex maps channels by tvg-id, not
# by number.

MIX_CHANNEL_NUMBER = 100
MIX_CHANNEL_NAME = "Fitzflix Mix"
MIX_CHANNEL_SLUG = "fitzflix-mix"

# A genre needs a real bench to sustain a channel. Below this count,
# it only feeds the mix.

MIN_GENRE_FILMS = 8

# Themed channels built from external signals (Criterion availability,
# the leaving set) are in their own number band. They can be much less
# deep. A 3-film last-call marathon is authentically cable.

CRITERION_CHANNEL_NUMBER = 140
LEAVING_CHANNEL_NUMBER = 141
MIN_SPECIAL_FILMS = 3

# TV channels are genre-based and theme-based, not per-series. Several
# series share a channel. They air in short interleaved blocks, like
# syndication. The auto genre channels (200+) come from the deepest
# TMDB TV genres. The themed channels (240+) come from the spec table
# below. The slot share of each series is proportional to its episode
# depth. Its cursor advances through the broadcast order from day to
# day.

TV_CHANNEL_NUMBER = 200
TV_THEME_NUMBER = 240

# The seeded genre channels must stay below the fixed channels (#397).
# The movie genres start at 101 and the TV genres at 200. The settings
# page has the same limits, but a value from .env can be larger.

MAX_GENRE_CHANNELS = CRITERION_CHANNEL_NUMBER - MIX_CHANNEL_NUMBER - 1
MAX_TV_CHANNELS = TV_THEME_NUMBER - TV_CHANNEL_NUMBER
MIN_SERIES_EPISODES = 8
TV_BLOCK = 2

# Themed TV channels. A series belongs when it satisfies EACH predicate
# that the spec declares. "genre" is a TMDB TV genre name. "keywords"
# is an any-of list of lowercase TMDB keywords. "network_country"
# matches any network registered to that country. A series also
# belongs when its title contains a "titles" pin. Pins exist for a
# series whose TMDB metadata is too thin to match in a different way.
# For example, Match Game PM has no keywords at all.

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

# AC-3 has a maximum of 5.1 channels. A source with more channels is
# downmixed. A source with fewer channels keeps its layout.

AC3_MAX_CHANNELS = 6


def _slugify(name):
    """Return the channel id.

    The M3U tvg-id, the XMLTV channel id, and the stream URL use it. It
    is lowercase. Each run of non-alphanumeric characters becomes 1
    hyphen."""

    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _probe_duration(file_path):
    """Return the container duration in seconds, or None.

    This is a header read, not a full scan."""

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


def _duration_fingerprint(file):
    """Return the text that identifies the media of a File row.

    An import of the same name reuses the row, and the id stays the
    same. The import sets a new localized date. A rewrite in place
    records a new size. Thus, the size and the localized date change
    when the media changes (#395)."""

    localized = file.date_localized.isoformat() if file.date_localized else ""
    return f"{file.filesize_bytes or 0}:{localized}"


def _cached_duration(redis_client, file):
    """Return the real duration of the file in seconds, or None if the probe fails.

    This function probes the file on first sight. After that, it caches
    the duration by file id, with the fingerprint of the media. A cached
    value with a different fingerprint is stale, and the file gets a new
    probe. A value from before the fingerprint has no fingerprint, and it
    is also stale."""

    fingerprint = _duration_fingerprint(file)
    cached = redis_client.hget(DURATIONS_KEY, str(file.id))
    if cached is not None:
        if isinstance(cached, bytes):
            cached = cached.decode()
        stored_fingerprint, _, value = cached.rpartition("|")
        if stored_fingerprint == fingerprint:
            try:
                return float(value)
            except ValueError:
                pass
    duration = _probe_duration(
        os.path.join(current_app.config["LIBRARY_DIR"], file.file_path)
    )
    if duration and duration > 0:
        redis_client.hset(DURATIONS_KEY, str(file.id), f"{fingerprint}|{duration:.3f}")
        return duration
    return None


def _prune_durations(redis_client):
    """Remove the cached durations of the File rows that no longer exist.

    The import deletes rows of worse copies and of recordings. Without
    this, their fields stay in the hash without end (#413)."""

    live = {str(file_id) for (file_id,) in db.session.query(File.id)}
    dead = []
    for field in redis_client.hkeys(DURATIONS_KEY):
        name = field.decode() if isinstance(field, bytes) else field
        if name not in live:
            dead.append(name)
    for start in range(0, len(dead), 1000):
        redis_client.hdel(DURATIONS_KEY, *dead[start : start + 1000])
    if dead:
        current_app.logger.info(
            f"DVR: removed {len(dead)} cached durations of deleted files"
        )


def _first_audio_channels(file_id):
    """Return the channel count of the first audio track of the file.

    The count has a maximum of 6, the 5.1 limit of AC-3. The result is
    6 when the scan recorded nothing usable.

    The first track is always the default track (the house rule). Thus,
    it is the track that the stream maps.
    """

    track = (
        FileAudioTrack.query.filter_by(file_id=file_id)
        .order_by(FileAudioTrack.track.asc())
        .first()
    )
    if track and track.channels:
        # The scan stores a layout in its spoken form. "5.1" is 6
        # channels. The ".1" is the LFE. Thus, the count is the whole
        # number plus 1 for a ".1". A digits-only read gave 5, and
        # ffmpeg dropped the subwoofer from each 5.1 airing.
        match = re.fullmatch(r"\s*(\d+)(?:\.(\d))?\s*", str(track.channels))
        if match:
            total = int(match.group(1)) + (1 if match.group(2) == "1" else 0)
            return min(total, AC3_MAX_CHANNELS)
    return AC3_MAX_CHANNELS


def _on_disk(file):
    """Return True if the local copy of the file is present.

    A row can outlive its local file. A superseded edition keeps its
    row and its S3 archive. The WEBDL rebuild leaves rows whose file
    is a renamed sibling. None of those can air. Call this only after
    _library_online. Thus, a stuck share never hangs a stat for each
    row."""

    return os.path.exists(
        os.path.join(current_app.config["LIBRARY_DIR"], file.file_path)
    )


def _library_online():
    """Return True if both library shares are mounted and answer.

    The build asks this 1 time. On a dead share, each row reads as
    absent. A build on that reading would replace the working dial
    with nothing. Thus, the build keeps the lineups of yesterday
    instead."""

    return all(
        volume_alive(current_app.config[key]) for key in ("MOVIE_LIBRARY", "TV_LIBRARY")
    )


def _best_files_by_movie():
    """Return the best main-feature copy on disk of each owned movie.

    The order is MOVIE_BEST_ORDER, the ranking of the movie cards. Skip
    the rows whose file is absent. Thus, an absent best
    row yields to a present lesser one. A movie with no copy on disk
    leaves the pool, because there is nothing to air and nothing to
    probe."""

    rows = (
        db.session.query(Movie, File)
        .join(File, File.movie_id == Movie.id)
        .join(RefQuality, RefQuality.id == File.quality_id)
        .filter(File.feature_type_id == None)  # noqa: E711
        .order_by(Movie.id, *MOVIE_BEST_ORDER)
        .all()
    )
    best = {}
    for movie, file in rows:
        if movie.id not in best and _on_disk(file):
            best[movie.id] = (movie, file)
    return best


def _genre_names_by_movie(movie_ids):
    """Return a map of movie id -> list of genre names.

    This is 1 query for the whole candidate pool."""

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
    """Return the stored program record.

    It holds all that the guide and the stream need. Thus, they never
    touch the database."""

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
    """Return the owned films that stream on the Criterion Channel now.

    The source is the availability cache. This function reads only the
    cache. The nightly refresh keeps the payload of each owned film
    warm. A cold entry only waits for the next build. The tmdb_id lets
    streaming_matches synthesize the match from the scraped Criterion
    stores. Thus, a day-one arrival that TMDB did not notice yet still
    joins the channel."""

    by_tmdb = {}
    for movie_id, (movie, _) in best.items():
        if movie.tmdb_id:
            by_tmdb.setdefault(movie.tmdb_id, movie_id)
    if not by_tmdb:
        return []
    payloads, _ = batch_title_availability(list(by_tmdb), fetch_limit=0)
    # This asks for each owned film, with or without a cached payload.
    # The scraped stores can vouch for a film whose availability entry
    # is cold.
    return [
        movie_id
        for tmdb_id, movie_id in by_tmdb.items()
        if streaming_matches(
            payloads.get(tmdb_id), {CRITERION_PROVIDER_ID}, tmdb_id=tmdb_id
        )
    ]


def _leaving_owned(best):
    """Return the owned films in the leaving-Criterion set, and the departure date.

    The channel airs OUR copies. The files stay. The streaming
    availability goes. Thus, "leaving" is a last-call programming cue,
    not a storage fact."""

    leaving, departs = _leaving_set()
    if not leaving:
        return [], None
    ids = [
        movie_id for movie_id, (movie, _) in best.items() if movie.tmdb_id in leaving
    ]
    return ids, departs


def _span_of(season, episode, last_episode):
    """Return the (season, episode) pairs of a file. A span file has more than 1."""

    last = last_episode if last_episode and last_episode > episode else episode
    return {(season, number) for number in range(episode, last + 1)}


def _program_count(rows):
    """Return the programs of (season, episode, last_episode) rows in broadcast order.

    A row whose first episode is in a span that came before it is not a
    new program (#428)."""

    covered = set()
    count = 0
    for season, episode, last_episode in rows:
        if (season, episode) in covered:
            continue
        covered |= _span_of(season, episode, last_episode)
        count += 1
    return count


def _series_catalog():
    """Return each series with owned regular episodes, with annotations.

    The annotations are the episode count, the TMDB genre names, the
    lowercase keywords, and the network countries. This is the pool
    that each TV channel selects from. This function builds it in 4
    queries."""

    # A span file counts 1 time. A single file of an episode that a span
    # covers is not a program of its own (#428).

    # The order is the order of _series_episodes. Thus, a single file and
    # a span with the same first episode tie the same way in both. The
    # count then agrees with the programs that air (follow-up #428).

    episodes_by_series = {}
    for series_id, season, episode, last_episode in (
        db.session.query(File.series_id, File.season, File.episode, File.last_episode)
        .join(RefQuality, RefQuality.id == File.quality_id)
        .filter(File.series_id != None)  # noqa: E711
        .filter(File.season > 0)
        .order_by(File.series_id, File.season, File.episode, *TV_BEST_ORDER)
        .all()
    ):
        episodes_by_series.setdefault(series_id, []).append(
            (season, episode, last_episode)
        )
    counts = {
        series_id: _program_count(rows)
        for series_id, rows in episodes_by_series.items()
    }
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


def _movie_keywords_by_movie(movie_ids):
    """Return a map of movie id -> set of lowercase keyword names.

    This is 1 query for the whole candidate pool."""

    if not movie_ids:
        return {}
    rows = (
        db.session.query(movie_keywords.c.movie_id, TMDBKeyword.name)
        .join(TMDBKeyword, TMDBKeyword.id == movie_keywords.c.keyword_id)
        .filter(movie_keywords.c.movie_id.in_(movie_ids))
        .all()
    )
    names = {}
    for movie_id, name in rows:
        names.setdefault(movie_id, set()).add((name or "").lower())
    return names


def _channel_members(channel, ctx):
    """Resolve one channel row to its members.

    The members are the explicit picks and the rule matches. Return
    (movie_ids, series_entries).

    Genres and keywords are any-of matches against each library that
    the include flags open. network_country applies to series.
    criterion and leaving restrict the rule-matched film pool. A title
    pin pulls a matching title in past each other filter. Pins exist
    for the titles whose TMDB metadata is too thin to match by rule.
    """

    genre_terms = set(channel.rule_list("genres"))
    keyword_terms = set(channel.rule_list("keywords"))
    pin_terms = channel.rule_list("title_pins")
    country = (channel.network_country or "").strip().upper() or None

    movie_ids = {movie.id for movie in channel.movies if movie.id in ctx["best"]}
    if channel.include_movies:
        for movie_id in ctx["best"]:
            if genre_terms and not genre_terms & ctx["movie_genres"].get(
                movie_id, set()
            ):
                continue
            if keyword_terms and not keyword_terms & ctx["movie_keywords"].get(
                movie_id, set()
            ):
                continue
            if channel.criterion_only and movie_id not in ctx["criterion"]:
                continue
            if channel.leaving_only and movie_id not in ctx["leaving"]:
                continue
            movie_ids.add(movie_id)
    if pin_terms:
        for movie_id, (movie, _) in ctx["best"].items():
            title = (movie.tmdb_title or movie.title or "").lower()
            if any(pin in title for pin in pin_terms):
                movie_ids.add(movie_id)

    chosen = {series.id for series in channel.series if series.id in ctx["catalog"]}
    if channel.include_tv:
        for series_id, entry in ctx["catalog"].items():
            if genre_terms and not genre_terms & {
                genre.lower() for genre in entry["genres"]
            }:
                continue
            if keyword_terms and not keyword_terms & entry["keywords"]:
                continue
            if country and country not in entry["countries"]:
                continue
            chosen.add(series_id)
    if pin_terms:
        for series_id, entry in ctx["catalog"].items():
            if any(pin in entry["series"].title.lower() for pin in pin_terms):
                chosen.add(series_id)

    return sorted(movie_ids), [ctx["catalog"][series_id] for series_id in chosen]


def _apportion(sizes, cap):
    """Return the daily slots of each series. The slots add up to exactly the cap (#427).

    sizes is the episode count of each series. When all the episodes fit
    in the cap, each series airs all of them. Otherwise each series gets
    1 slot, and the rest of the cap goes in proportion to the remaining
    episodes, by the largest remainder. A series never gets more slots
    than it has episodes. The caller gives at most cap series."""

    total = sum(sizes)
    if total <= cap:
        return list(sizes)
    rest = cap - len(sizes)
    spare = [size - 1 for size in sizes]
    spare_total = sum(spare)
    shares = [Fraction(rest * extra, spare_total) for extra in spare]
    quotas = [1 + floor(share) for share in shares]
    order = sorted(
        range(len(sizes)), key=lambda i: (-(shares[i] - floor(shares[i])), i)
    )
    for i in order[: cap - sum(quotas)]:
        quotas[i] += 1
    return quotas


def _rotation_turns(index, days, count, cap):
    """Return the days before day number days on which series index had its slot.

    With more series than the cap, a window of cap series moves along the
    list by cap each day. Each series gets 1 slot when the window
    covers it. The count is the cursor of the series. Thus, each episode
    airs in its turn."""

    period = count // gcd(count, cap)
    per_period = cap // gcd(count, cap)
    full, rest = divmod(days, period)
    return full * per_period + sum(
        1 for day in range(rest) if (index - day * cap) % count < cap
    )


def _episode_key(file):
    """Return the broadcast position of an episode file as [season, episode]."""

    return [file.season, file.episode]


def _locate(episodes, key):
    """Return the position of the first episode at or after key. Return 0 if none is.

    An episode that left the library leaves no gap. The next one takes
    its place. A new episode before the key does not move the cursor."""

    target = tuple(key)
    for position, file in enumerate(episodes):
        if (file.season, file.episode) >= target:
            return position
    return 0


def _stored_start(cursor, day, episodes):
    """Return the start position of a series from its stored cursor, or None (#460).

    A rebuild on the same day starts where the first build of the day
    started. A later day starts after the last episode that aired. A
    cursor from a later day, or a damaged one, gives None."""

    if not cursor:
        return None
    try:
        stored_day = date.fromisoformat(cursor["day"])
        if stored_day == day:
            return _locate(episodes, cursor["start"])
        if stored_day < day:
            return _locate(episodes, cursor["next"])
    except (KeyError, TypeError, ValueError):
        return None
    return None


def _advanced_cursor(day, episodes, start, advance):
    """Return the stored cursor of a series that moves advance positions from start.

    advance is the position after the last episode that aired, counted
    from start (#474). It is not the count of the episodes that aired. A
    dropped episode before the last aired one stays behind the cursor.
    Thus, no aired episode airs again the next day."""

    return {
        "day": day.isoformat(),
        "start": _episode_key(episodes[start]),
        "next": _episode_key(episodes[(start + advance) % len(episodes)]),
    }


def _channel_window(members, day, cap, cursors=None, starts=None):
    """Return the program window of the day for a multi-series channel.

    The window is a list of (series, file) pairs. Each series gets
    slots proportional to its depth, and the slots fill the cap exactly
    (#427). They air as short interleaved blocks, like syndication. The
    cursor of each series moves each day by the slots that the series
    aired. Thus, no episode is skipped. With more series than the cap,
    the series take 1 slot each in turn.

    cursors maps a series id to its stored cursor (#460). A series with
    a cursor continues after the last episode that aired, also when the
    series gained or lost an episode. A series with no cursor starts at
    the position of the day formula. starts gets the episode list and
    the start position of each series in the window. The caller moves a
    cursor only by the episodes that really aired (follow-up #460).
    A duration probe can drop an episode from the lineup."""

    members = sorted(members, key=lambda m: (m["series"].title.lower(), m["series"].id))
    lists = [(m["series"], _series_episodes(m["series"].id)) for m in members]
    lists = [(series, episodes) for series, episodes in lists if episodes]
    if not lists or cap <= 0:
        return []

    def start_of(series, episodes, formula):
        if cursors is not None:
            stored = _stored_start(cursors.get(series.id), day, episodes)
            if stored is not None:
                return stored
        return formula % len(episodes)

    ordinal = day.toordinal()
    cursor_rows = []
    if len(lists) <= cap:
        quotas = _apportion([len(episodes) for _, episodes in lists], cap)
        for (series, episodes), quota in zip(lists, quotas):
            start = start_of(series, episodes, ordinal * quota)
            cursor_rows.append([series, episodes, start, quota])
    else:
        count = len(lists)
        for index, (series, episodes) in enumerate(lists):
            if (index - ordinal * cap) % count >= cap:
                continue
            start = start_of(
                series, episodes, _rotation_turns(index, ordinal, count, cap)
            )
            cursor_rows.append([series, episodes, start, 1])
    cursors_now = [
        [series, episodes[start:] + episodes[:start], quota]
        for series, episodes, start, quota in cursor_rows
    ]

    window = []
    while len(window) < cap:
        progressed = False
        for cursor in cursors_now:
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

    if starts is not None:
        for series, episodes, start, _ in cursor_rows:
            starts[series.id] = (episodes, start)
    return window


def _series_episodes(series_id):
    """Return the best copy on disk of each regular episode, in broadcast order.

    The order is TV_BEST_ORDER, the per-episode ranking of the library
    pages. Specials are excluded. Rows whose
    file is absent are skipped, in the same way as _best_files_by_movie
    skips them."""

    rows = (
        File.query.join(RefQuality, RefQuality.id == File.quality_id)
        .filter(File.series_id == series_id)
        .filter(File.season > 0)
        .order_by(
            File.season.asc(),
            File.episode.asc(),
            *TV_BEST_ORDER,
        )
        .all()
    )
    # A span file covers each episode of its span. A single file of an
    # episode in an aired span would air it two times (#428).

    episodes = []
    covered = set()
    for file in rows:
        key = (file.season, file.episode)
        if key not in covered and _on_disk(file):
            covered |= _span_of(file.season, file.episode, file.last_episode)
            episodes.append(file)
    return episodes


def _episode_program(series, file, duration):
    """Return the stored program record for one episode.

    The series supplies the artwork, the overview, and the guide title.
    The file supplies the numbering and the optional episode title
    (File.edition, the house convention)."""

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


def _merge_programs(movie_programs, episode_programs):
    """Return the schedule of a mixed channel.

    The episodes carry the rhythm. The movies space themselves evenly
    through the cycle, like the nightly feature presentation of a
    station."""

    if not movie_programs or not episode_programs:
        return movie_programs or episode_programs
    merged = []
    movies = list(movie_programs)
    step = len(episode_programs) / len(movie_programs)
    next_at = step
    for position, program in enumerate(episode_programs, start=1):
        merged.append(program)
        while movies and position >= next_at - 1e-9:
            merged.append(movies.pop(0))
            next_at += step
    merged.extend(movies)
    return merged


def seed_default_channels():
    """Create the default dial as editable rows.

    This function runs 1 time, when the channel table is empty. The
    dial has the all-library mix, the deepest movie genres, and the
    Criterion and Leaving Soon overlays. It also has the deepest TV
    genres (with a "TV" suffix) and the starter themes. From then on,
    the dial belongs to the admin editor. It never seeds again."""

    best = _best_files_by_movie()
    genres_by_movie = _genre_names_by_movie(list(best))
    counts = {}
    for movie_id in best:
        for name in genres_by_movie.get(movie_id, []):
            counts[name] = counts.get(name, 0) + 1
    deep = [name for name, count in counts.items() if count >= MIN_GENRE_FILMS]
    top_movie = sorted(
        sorted(deep, key=counts.get, reverse=True)[
            : min(get_setting("DVR_GENRE_CHANNELS"), MAX_GENRE_CHANNELS)
        ]
    )

    rows = [
        DVRChannel(
            number=MIX_CHANNEL_NUMBER,
            name=MIX_CHANNEL_NAME,
            slug=MIX_CHANNEL_SLUG,
            include_movies=True,
        )
    ]
    for offset, name in enumerate(top_movie, start=1):
        rows.append(
            DVRChannel(
                number=MIX_CHANNEL_NUMBER + offset,
                name=name,
                slug=_slugify(name),
                include_movies=True,
                genres=name,
            )
        )
    rows.append(
        DVRChannel(
            number=CRITERION_CHANNEL_NUMBER,
            name="Criterion",
            slug="criterion",
            include_movies=True,
            criterion_only=True,
        )
    )
    rows.append(
        DVRChannel(
            number=LEAVING_CHANNEL_NUMBER,
            name="Leaving Soon",
            slug="leaving-soon",
            include_movies=True,
            leaving_only=True,
        )
    )

    catalog = _series_catalog()
    totals = {}
    for entry in catalog.values():
        for name in entry["genres"]:
            totals[name] = totals.get(name, 0) + entry["episodes"]
    deep_tv = [name for name, count in totals.items() if count >= MIN_SERIES_EPISODES]
    top_tv = sorted(
        sorted(deep_tv, key=totals.get, reverse=True)[
            : min(get_setting("DVR_TV_CHANNELS"), MAX_TV_CHANNELS)
        ]
    )
    for offset, name in enumerate(top_tv):
        # The "TV" suffix makes sure that the guide never shows a movie
        # genre channel and a TV genre channel with the same name
        # (Comedy collides).
        rows.append(
            DVRChannel(
                number=TV_CHANNEL_NUMBER + offset,
                name=f"{name} TV",
                slug=f"tv-{_slugify(name)}",
                include_tv=True,
                genres=name,
            )
        )
    for offset, spec in enumerate(TV_THEME_SPECS):
        rows.append(
            DVRChannel(
                number=TV_THEME_NUMBER + offset,
                name=spec["name"],
                slug=f"tv-{_slugify(spec['name'])}",
                include_tv=True,
                keywords=", ".join(spec.get("keywords", ())) or None,
                network_country=spec.get("network_country"),
                title_pins=", ".join(spec.get("titles", ())) or None,
            )
        )
    db.session.add_all(rows)
    db.session.commit()
    current_app.logger.info(f"DVR: seeded {len(rows)} default channels")
    return True


def rebuild_queued(queue):
    """Return True when a lineup rebuild waits on the queue.

    The helper below queues rebuilds under ids that start with
    REBUILD_JOB_ID. Thus, this is 1 read of the id list, not a fetch of
    each queued job."""

    return any(job_id.startswith(REBUILD_JOB_ID) for job_id in queue.job_ids)


def enqueue_lineup_rebuild(reason, tmdb_ids=None):
    """Queue a lineup rebuild. Thus, the stored dial catches up the same day.

    The saves of the admin editor call this. The Criterion scrapers
    call this when a new set arrives after the nightly build. This
    function does nothing without a configured DVR. It does nothing
    while a rebuild is already queued. A RUNNING rebuild can have read
    the old inputs. Thus, a RUNNING rebuild does not count as queued.
    Each job gets its own id. An id is never used again. rq keeps the
    expiry of an old record under the same id. A queued job under that
    id can then vanish before a worker reaches it (review, 2026-09-02).
    It does nothing when the given tmdb_ids include no owned film. The
    dial airs only owned copies. Thus, a Channel arrival that nobody
    owns cannot change a program. Return the job, or None when nothing
    was queued."""

    if not current_app.config.get("DVR_TOKEN"):
        return None
    if tmdb_ids is not None:
        ids = [tmdb_id for tmdb_id in tmdb_ids if tmdb_id]
        owned = ids and (
            db.session.query(File.id)
            .join(Movie, Movie.id == File.movie_id)
            .filter(Movie.tmdb_id.in_(ids))
            .filter(File.feature_type_id == None)  # noqa: E711
            .first()
        )
        if not owned:
            return None
    queue = current_app.maintenance_queue
    if rebuild_queued(queue):
        return None
    return enqueue_task(
        REBUILD_FUNC,
        queue=queue,
        job_id=f"{REBUILD_JOB_ID}-{uuid.uuid4().hex[:12]}",
        description=f"Building virtual DVR channel lineups ({reason})",
    )


def build_channel_lineups(day=None):
    """Build the lineup of each enabled channel from its stored definition.

    The definition is in the dvr_channel table, the domain of the admin
    editor. On the first run, this function seeds the default dial.
    The rule-matched films are seeded daily shuffles, with a maximum of
    DVR_CHANNEL_FILMS. The series air as interleaved broadcast-order
    windows, with a maximum of DVR_CHANNEL_EPISODES. A channel with
    both spaces its films evenly through the episode cycle.

    This task runs each night on the maintenance queue. The duration
    probes are the only real cost. The per-file cache makes each build
    after the first appearance of a file free.
    """

    with app.app_context():
        redis_client = current_app.redis
        day = day or date.today()
        film_cap = get_setting("DVR_CHANNEL_FILMS")
        episode_cap = get_setting("DVR_CHANNEL_EPISODES")

        if not _library_online():
            # A folder that was never made is not a dead share (#501). A
            # new install has no library folders yet. An ERROR each night
            # would mail the admin.
            from app.maintenance import VOLUMES_ROOT

            missing = [
                current_app.config[key]
                for key in ("MOVIE_LIBRARY", "TV_LIBRARY")
                if not os.path.exists(current_app.config[key])
                and not current_app.config[key].startswith(VOLUMES_ROOT + os.sep)
            ]
            if missing:
                current_app.logger.warning(
                    f"DVR: the library folder(s) {missing} do not exist. Keeping "
                    f"the stored lineups. Make the folders to build the lineups"
                )
            else:
                current_app.logger.error(
                    "DVR: a library share is offline; keeping the stored lineups"
                )
            return True

        # The nightly cron build has a random job id, so the dedupe in
        # enqueue_lineup_rebuild cannot see it. When a triggered rebuild
        # already waits behind this one, that rebuild has the fresher
        # inputs. Thus, this one steps aside.
        if rebuild_queued(current_app.maintenance_queue):
            current_app.logger.info(
                "DVR: a lineup rebuild is already queued; skipping this build"
            )
            return True

        if DVRChannel.query.count() == 0:
            seed_default_channels()
        channels = DVRChannel.query.order_by(DVRChannel.number.asc()).all()

        best = _best_files_by_movie()
        genres_by_movie = _genre_names_by_movie(list(best))
        ctx = {
            "best": best,
            "movie_genres": {
                movie_id: {name.lower() for name in names}
                for movie_id, names in genres_by_movie.items()
            },
            "movie_keywords": _movie_keywords_by_movie(list(best)),
            "catalog": _series_catalog(),
            "criterion": set(),
            "leaving": set(),
        }
        if any(c.enabled and c.criterion_only for c in channels):
            ctx["criterion"] = set(_criterion_movie_ids(best))
        leaving_ids, departs = _leaving_owned(best)
        ctx["leaving"] = set(leaving_ids)

        epoch = datetime.now(timezone.utc).timestamp()
        index = []

        def store(number, slug, name, programs, cursors):
            """Write the lineup of one channel and index it.

            The record holds the series cursors of the channel (#460). A
            cursor holds the day of the last window, the first episode of
            that window, and the next episode to air. The lineup and its
            cursors go into Redis in 1 SET (#627). Thus, a build that keeps
            the old lineup keeps the old cursors too. This function drops
            the empty channels."""

            if not programs:
                current_app.logger.warning(f"DVR: channel {slug} has no programs")
                return
            lineup = {
                "slug": slug,
                "name": name,
                "number": number,
                "epoch": epoch,
                "programs": programs,
                "cursors": {str(key): value for key, value in cursors.items()},
            }
            redis_client.set(LINEUP_KEY.format(slug=slug), json.dumps(lineup))
            index.append({"number": number, "slug": slug, "name": name})

        for channel in channels:
            if not channel.enabled:
                continue
            movie_ids, series_entries = _channel_members(channel, ctx)

            # The Criterion/Leaving overlays keep their bench. A loop of
            # 1 or 2 films reads as broken, not as a marathon.

            if (channel.criterion_only or channel.leaving_only) and not series_entries:
                if len(movie_ids) < MIN_SPECIAL_FILMS:
                    current_app.logger.info(
                        f"DVR: channel {channel.slug} is below the "
                        f"{MIN_SPECIAL_FILMS}-film bench; skipping"
                    )
                    continue
            note = None
            if channel.leaving_only and departs:
                note = f"Leaving the Criterion Channel {departs.strftime('%B %-d')}."

            # The whole shuffled pool is the candidate list, not only
            # its first film_cap entries. A film whose probe fails
            # gives its slot to the next film. It does not shrink the
            # day.
            Random(f"dvr:{channel.slug}:{day.isoformat()}").shuffle(movie_ids)
            movie_programs = []
            for movie_id in movie_ids:
                if len(movie_programs) >= film_cap:
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
                movie_programs.append(program)

            # The cursors of the series continue from the last build (#460).

            stored = channel_lineup(redis_client, channel.slug) or {}
            if "cursors" in stored:
                cursors = {int(key): value for key, value in stored["cursors"].items()}
            else:
                cursors = _legacy_cursors(redis_client, channel.slug)
            starts = {}
            window = _channel_window(series_entries, day, episode_cap, cursors, starts)

            # The window gives the episodes of a series in order from its
            # start. offsets counts the position of each one in that
            # slice. advance keeps the position after the last episode
            # that aired (#474).

            episode_programs = []
            offsets = {}
            advance = {}
            for series, file in window:
                offset = offsets.get(series.id, 0)
                offsets[series.id] = offset + 1
                duration = _cached_duration(redis_client, file)
                if duration is None:
                    current_app.logger.warning(
                        f"DVR: no duration for {file.file_path}; skipping"
                    )
                    continue
                episode_programs.append(_episode_program(series, file, duration))
                advance[series.id] = offset + 1

            # A cursor moves to the position after the last episode that
            # aired. A series with none keeps its cursor.

            member_ids = {entry["series"].id for entry in series_entries}
            for series_id, positions in advance.items():
                episodes, start = starts[series_id]
                cursors[series_id] = _advanced_cursor(day, episodes, start, positions)

            store(
                channel.number,
                channel.slug,
                channel.name,
                _merge_programs(movie_programs, episode_programs),
                {key: value for key, value in cursors.items() if key in member_ids},
            )

        if not index and (best or ctx["catalog"]):
            # Files are on disk, but the build made no program. That is
            # a probe-side outage (ffprobe is missing, or the share got
            # stuck during the build). It is not an empty dial. The
            # stored lineups continue to answer.
            current_app.logger.error(
                "DVR: no channel produced a program; keeping the stored lineups"
            )
            return True

        redis_client.set(CHANNELS_KEY, json.dumps(index))

        # Without this, the stored lineup of a deleted or emptied channel
        # would answer its stream URL without end.

        kept = {LINEUP_KEY.format(slug=entry["slug"]) for entry in index}
        for key in redis_client.scan_iter(match=LINEUP_KEY.format(slug="*")):
            if key.decode() not in kept:
                redis_client.delete(key)

        _prune_durations(redis_client)

        current_app.logger.info(
            f"DVR: built {len(index)} channel lineups "
            f"({len(best)} owned films in pool)"
        )
        return True


def _legacy_cursors(redis_client, slug):
    """Return the cursors of a channel from the hash from before #627.

    The first build after the deploy reads them. Thus, each series
    continues from its last position. A field that does not parse is
    dropped."""

    cursors = {}
    for field, raw in redis_client.hgetall(
        LEGACY_CURSORS_KEY.format(slug=slug)
    ).items():
        try:
            cursors[int(field)] = json.loads(raw)
        except ValueError:
            continue
    return cursors


def channel_index(redis_client):
    """Return the stored channel list ([{number, slug, name}, ...]).

    The result is an empty list before the first build."""

    stored = redis_client.get(CHANNELS_KEY)
    return json.loads(stored) if stored else []


def channel_lineup(redis_client, slug):
    """Return the stored lineup for one channel, or None."""

    stored = redis_client.get(LINEUP_KEY.format(slug=slug))
    return json.loads(stored) if stored else None


def program_at(lineup, when):
    """Return what the channel plays at the given epoch timestamp.

    The result is the program index and the number of seconds into the
    program at that moment.

    The lineup repeats from its epoch without end. Thus, this is
    position arithmetic over the cumulative durations. No state, no
    drift.
    """

    total = sum(program["duration"] for program in lineup["programs"])
    position = (when - lineup["epoch"]) % total
    for index, program in enumerate(lineup["programs"]):
        if position < program["duration"]:
            return index, position
        position -= program["duration"]
    return 0, 0.0


def programs_between(lineup, start, stop):
    """Yield the airings of the channel that overlap [start, stop).

    Each item is a (start_ts, stop_ts, program) tuple. The lineup
    cycles as necessary. The XMLTV guide renders this."""

    index, offset = program_at(lineup, start)
    cursor = start - offset
    while cursor < stop:
        program = lineup["programs"][index]
        yield cursor, cursor + program["duration"], program
        cursor += program["duration"]
        index = (index + 1) % len(lineup["programs"])
