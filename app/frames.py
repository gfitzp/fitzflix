"""Name That Frame (GitHub #52): the pre-extracted frame pool.

The game never runs ffmpeg at play time. A nightly task keeps a pool
of single frames — one per pooled movie — extracted from each film's
best library copy at a random moment, and the game page draws from
the pool. Frames are named by opaque tokens (the filename must never
hint at the answer) with a Redis hash mapping token → movie; images
live in FRAME_POOL_DIR and are served through an authenticated route,
not the public static path. The pool tops itself up to
FRAME_POOL_SIZE and retires at least FRAME_POOL_ROTATE entries each
night — every frame a player has already been dealt first, then the
oldest — so every film eventually gets a turn, long-pooled films get
fresh frames, and a played-out frame gives up its slot to one nobody
has seen. Each finished game round also turns the pool over by one on
the spot (replace_frame_task), so frames refresh continuously between
nightly passes. Extraction runs on the transcode queue — the serial
heavy-I/O lane — because the shell can't read /Volumes; only workers
can.
"""

import json
import os
import random
import secrets
import subprocess
import time
import traceback

from flask import current_app
from werkzeug.local import LocalProxy

from app import db, get_app
from app.models import File, Movie, RefQuality

app = LocalProxy(get_app)

POOL_KEY = "fitzflix:frames:pool"

# Skip the first and last stretches of the runtime: studio logos and
# opening titles give the film away cheaply, and end credits give it
# away completely

OFFSET_LOW, OFFSET_HIGH = 0.05, 0.85

# Decode this many seconds forward into the extraction point instead of
# trusting the container's keyframe flags: VC-1 Blu-ray remuxes flag
# non-keyframes as seekable, and an input-side seek that lands on one
# decodes to a flat gray ghost frame (7 pooled frames, Aug 20 2026)

SEEK_LEAD = 30

# Per-user record of the frames the game has dealt: a sorted set of
# token → the turn it was served on. The game reads it to deal a frame
# the player has never seen (and, once a difficulty runs out of those,
# the least-recently-seen one); the nightly pass reads it to retire
# spent frames first, so what a player has already been shown is the
# first thing rotation replaces (#200)

DEALT_KEY = "fitzflix:frames:dealt:{user_id}"

# Long enough that a lapsed player picks up where they left off, short
# enough that the record can't outlive the pool it points into

DEALT_TTL = 60 * 24 * 3600


def dealt_key(user_id):
    """The dealt-frames sorted set for one user."""

    return DEALT_KEY.format(user_id=user_id)


def _all_dealt_tokens():
    """Every token any player has been dealt — rotation's "spent" set."""

    dealt = set()
    for key in current_app.redis.scan_iter(DEALT_KEY.format(user_id="*")):
        dealt |= {token.decode() for token in current_app.redis.zrange(key, 0, -1)}
    return dealt


def _forget_dropped_tokens(pooled):
    """Drop tokens that have left the pool from every player's dealt
    record, so a rotated-out frame stops counting as spent."""

    for key in current_app.redis.scan_iter(DEALT_KEY.format(user_id="*")):
        gone = [
            token
            for token in current_app.redis.zrange(key, 0, -1)
            if token.decode() not in pooled
        ]
        if gone:
            current_app.redis.zrem(key, *gone)


def frame_path(token):
    """The pooled frame's image path for one token."""

    return os.path.join(current_app.config["FRAME_POOL_DIR"], f"{token}.jpg")


def pool_entries():
    """token → {movie_id, extracted_at, offset} for every pooled frame;
    malformed entries are dropped from the answer (the nightly prune
    removes them for real)."""

    entries = {}
    for token, payload in current_app.redis.hgetall(POOL_KEY).items():
        try:
            entries[token.decode()] = json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            continue
    return entries


def _drop_entry(token):
    """Remove one pool entry and its image."""

    current_app.redis.hdel(POOL_KEY, token)
    try:
        os.remove(frame_path(token))
    except OSError:
        pass


def _best_file(movie_id):
    """The movie's best main-feature copy, the card's ranking: never a
    fullscreen copy while a widescreen one exists, then best quality."""

    return (
        db.session.query(File, RefQuality)
        .join(RefQuality, RefQuality.id == File.quality_id)
        .filter(File.movie_id == movie_id)
        .filter(File.feature_type_id == None)
        .order_by(File.fullscreen.asc(), RefQuality.preference.desc())
        .first()
    )


def _probe_duration(file_path):
    """The container duration in seconds, or None — a header read."""

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


def refresh_frame_pool_task():
    """Task (nightly): prune dead pool entries, then queue extractions
    to top the pool up to FRAME_POOL_SIZE — rotating the oldest
    FRAME_POOL_ROTATE entries once it's full, preferring movies not
    yet pooled so the whole library cycles through. Each reviewer is
    guaranteed FRAME_POOL_MIN_RATED pooled frames from their own
    diary (capped by how many rated films they actually have), so
    Easy mode never runs thin (Glenn's ask, Aug 20 2026)."""

    from app.models import UserMovieReview

    with app.app_context():
        entries = pool_entries()

        # Prune: the movie is gone, its last main-feature file is gone,
        # or the image itself is missing

        playable = {
            movie_id
            for (movie_id,) in db.session.query(Movie.id).filter(
                Movie.files.any(File.feature_type_id.is_(None))
            )
            # Home movies and other recordings without a TMDB entry
            # never deal (#205) — this prunes any already pooled, too
            .filter(Movie.tmdb_id.isnot(None))
        }
        valid = {}
        for token, entry in entries.items():
            if entry.get("movie_id") not in playable or not os.path.isfile(
                frame_path(token)
            ):
                _drop_entry(token)
            else:
                valid[token] = entry

        pooled = len(valid)
        pooled_movies = {entry["movie_id"] for entry in valid.values()}
        size = current_app.config["FRAME_POOL_SIZE"]
        rotate = current_app.config["FRAME_POOL_ROTATE"]
        min_rated = current_app.config["FRAME_POOL_MIN_RATED"]

        # Rotation runs BEFORE the reviewer floors below, and retires
        # spent frames before merely old ones (#200). Both orderings
        # matter: measuring the floors first let rotation evict the
        # very rated frames they had just guaranteed (Easy settled a
        # quarter under its minimum), and retiring by age alone left
        # frames a player had already been dealt sitting in the pool
        # while films they'd never seen waited outside it.

        dealt = _all_dealt_tokens()
        fresh = list(playable - pooled_movies)
        random.shuffle(fresh)

        # A full pool retires every spent frame — never fewer than
        # FRAME_POOL_ROTATE, so the pool keeps turning over even for a
        # player who hasn't touched the game

        retired = []
        if len(valid) + len(fresh) >= size:
            spent = sum(1 for token in valid if token in dealt)
            oldest = sorted(
                valid.items(),
                key=lambda item: (
                    item[0] not in dealt,
                    item[1].get("extracted_at", 0),
                ),
            )[: max(rotate, spent)]
            for token, entry in oldest:
                _drop_entry(token)
                del valid[token]
                pooled_movies.discard(entry["movie_id"])
                retired.append(entry["movie_id"])

        # Per-reviewer floors: whoever's Easy world is short of the
        # minimum gets extractions from their own unpooled rated films
        # before the general fill. A film just retired stays out for
        # the night — the point of retiring it was to show something
        # else — so the floor draws only on films the pool has never
        # served.

        floors = []
        chosen = set()
        reviewers = {
            user_id
            for (user_id,) in db.session.query(UserMovieReview.user_id).distinct()
        }
        for user_id in sorted(reviewers):
            # Rated means starred, matching the game's world — an
            # unrated diary row (a Netflix-import watch, say) doesn't
            # count, or the floor would pool films Easy can't deal
            rated_playable = {
                movie_id
                for (movie_id,) in db.session.query(UserMovieReview.movie_id)
                .filter(UserMovieReview.user_id == user_id)
                .filter(UserMovieReview.movie_id.isnot(None))
                .filter(UserMovieReview.rating.isnot(None))
            } & playable
            floor = min(min_rated, len(rated_playable))
            pooled_rated = len(rated_playable & (pooled_movies | chosen))
            candidates = list(rated_playable - pooled_movies - chosen - set(retired))
            random.shuffle(candidates)
            needed = candidates[: max(0, floor - pooled_rated)]
            short = max(0, floor - pooled_rated - len(needed))
            if short:
                # A reviewer with barely more rated films than the floor
                # can run out of unpooled ones. The floor outranks the
                # retirement: the film comes back, on a new frame

                spare = [movie_id for movie_id in retired if movie_id not in chosen]
                random.shuffle(spare)
                needed += spare[:short]
            floors += needed
            chosen |= set(needed)

        # Refill the room the prune and rotation left, FRAME_POOL_SIZE
        # being the ceiling — the floors are a composition rule for the
        # pool, not licence to grow past it. Slots go to the reviewer
        # floors first, then to films the pool has never held, and only
        # when the library has none of those left do the films just
        # retired come back on brand-new frames.

        to_extract = floors[: max(0, size - len(valid))]
        chosen = set(to_extract)
        room = max(0, size - len(valid) - len(to_extract))
        top_up = [movie_id for movie_id in fresh if movie_id not in chosen][:room]
        to_extract += top_up
        chosen |= set(top_up)
        room -= len(top_up)
        to_extract += [movie_id for movie_id in retired if movie_id not in chosen][
            :room
        ]

        # Tokens that just left the pool stop counting as spent

        _forget_dropped_tokens(set(valid))

        titles = {
            movie_id: title
            for movie_id, title in db.session.query(Movie.id, Movie.title).filter(
                Movie.id.in_(to_extract or [0])
            )
        }
        for movie_id in to_extract:
            current_app.transcode_queue.enqueue(
                "app.frames.extract_frame_task",
                args=(movie_id,),
                job_timeout=600,
                description=(
                    f"Extracting a frame from '{titles.get(movie_id, movie_id)}'"
                ),
            )
        current_app.logger.info(
            f"Frame pool: {pooled} pooled, {len(entries) - pooled} pruned, "
            f"{len(retired)} retired, {len(to_extract)} extractions queued"
        )
        return {"pooled": pooled, "queued": len(to_extract)}


def replace_frame_task(movie_id):
    """Task (per-round top-up, Glenn's ask, Aug 27 2026): turn the
    pool over by one as soon as a round ends, instead of waiting for
    the nightly pass. Extract a frame from a playable film the pool
    doesn't currently hold — or, when the whole library is pooled, a
    fresh frame of the played film itself — and on success retire the
    played film's old frame, so the pool never grows past its size.
    The extraction runs here on the transcode queue, seconds after
    the reveal; the reveal page itself has already rendered, so the
    played token can safely leave the pool. A failed extraction
    leaves the played frame in place for the nightly pass to retire."""

    with app.app_context():
        entries = pool_entries()
        played_tokens = [
            token
            for token, entry in entries.items()
            if entry.get("movie_id") == movie_id
        ]
        pooled_movies = {entry.get("movie_id") for entry in entries.values()}
        playable = {
            candidate_id
            for (candidate_id,) in db.session.query(Movie.id)
            .filter(Movie.files.any(File.feature_type_id.is_(None)))
            .filter(Movie.tmdb_id.isnot(None))
        }
        candidates = list(playable - pooled_movies)
        replacement = random.choice(candidates) if candidates else movie_id
        if extract_frame_task(replacement) and replacement != movie_id:
            # extract_frame_task only retires old frames of its own
            # movie — swap the played film's frame out here
            for token in played_tokens:
                _drop_entry(token)
        return replacement


def extract_frame_task(movie_id):
    """Task: extract one random frame from the movie's best copy into
    the pool. Replaces any existing frame for the movie — one frame
    per film, refreshed by rotation. A failed extraction leaves the
    pool untouched; the next nightly pass tries again."""

    with app.app_context():
        best = _best_file(movie_id)
        if best is None:
            return False
        file, _ = best
        source = os.path.join(current_app.config["LIBRARY_DIR"], file.file_path)
        duration = _probe_duration(source)
        if not duration or duration <= 0:
            current_app.logger.info(
                f"'{file.basename}' has no readable duration; skipping frame"
            )
            return False

        offset = random.uniform(OFFSET_LOW, OFFSET_HIGH) * duration
        pre_seek = max(0.0, offset - SEEK_LEAD)
        token = secrets.token_urlsafe(12)
        os.makedirs(current_app.config["FRAME_POOL_DIR"], exist_ok=True)
        out = frame_path(token)
        try:
            # Glenn's recipe from the issue, plus sar correction so
            # anamorphic DVDs come out at display proportions; the
            # seek is two-stage (fast to SEEK_LEAD early, accurate the
            # rest) so the decoder crosses a real keyframe on the way
            subprocess.run(
                [
                    current_app.config["FFMPEG_BIN"],
                    "-y",
                    "-v",
                    "error",
                    "-ss",
                    f"{pre_seek:.3f}",
                    "-i",
                    source,
                    "-ss",
                    f"{offset - pre_seek:.3f}",
                    "-vf",
                    "scale='min(1080,iw*sar)':-2",
                    "-frames:v",
                    "1",
                    "-q:v",
                    "2",
                    out,
                ],
                capture_output=True,
                timeout=600,
            )
        except (OSError, subprocess.SubprocessError):
            current_app.logger.warning(traceback.format_exc())
        if not os.path.isfile(out) or os.path.getsize(out) == 0:
            try:
                os.remove(out)
            except OSError:
                pass
            current_app.logger.warning(
                f"Frame extraction produced nothing for '{file.basename}' "
                f"at {offset:.1f}s"
            )
            return False

        for old_token, entry in pool_entries().items():
            if entry.get("movie_id") == movie_id:
                _drop_entry(old_token)
        current_app.redis.hset(
            POOL_KEY,
            token,
            json.dumps(
                {
                    "movie_id": movie_id,
                    "extracted_at": int(time.time()),
                    "offset": round(offset, 3),
                }
            ),
        )
        current_app.logger.info(
            f"Pooled a frame from '{file.basename}' at {offset:.1f}s"
        )
        return True
