"""Name that Frame (GitHub #52): the pre-extracted frame pool.

The game never runs ffmpeg at play time. A nightly task keeps a pool
of single frames — one per pooled movie — extracted from each film's
best library copy at a random moment, and the game page draws from
the pool. Frames are named by opaque tokens (the filename must never
hint at the answer) with a Redis hash mapping token → movie; images
live in FRAME_POOL_DIR and are served through an authenticated route,
not the public static path. The pool tops itself up to
FRAME_POOL_SIZE and rotates FRAME_POOL_ROTATE of its oldest entries
each night, so every film eventually gets a turn and long-pooled
films get fresh frames. Extraction runs on the transcode queue — the
serial heavy-I/O lane — because the shell can't read /Volumes; only
workers can.
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
        }
        valid = {}
        for token, entry in entries.items():
            if entry.get("movie_id") not in playable or not os.path.isfile(
                frame_path(token)
            ):
                _drop_entry(token)
            else:
                valid[token] = entry

        pooled_movies = {entry["movie_id"] for entry in valid.values()}
        size = current_app.config["FRAME_POOL_SIZE"]
        rotate = current_app.config["FRAME_POOL_ROTATE"]
        min_rated = current_app.config["FRAME_POOL_MIN_RATED"]

        # Per-reviewer floors first: whoever's Easy world is short of
        # the minimum gets extractions from their own unpooled rated
        # films before the general fill

        to_extract = []
        chosen = set()
        reviewers = {
            user_id
            for (user_id,) in db.session.query(UserMovieReview.user_id).distinct()
        }
        for user_id in sorted(reviewers):
            rated_playable = {
                movie_id
                for (movie_id,) in db.session.query(UserMovieReview.movie_id)
                .filter(UserMovieReview.user_id == user_id)
                .filter(UserMovieReview.movie_id.isnot(None))
            } & playable
            floor = min(min_rated, len(rated_playable))
            pooled_rated = len(rated_playable & (pooled_movies | chosen))
            candidates = list(rated_playable - pooled_movies - chosen)
            random.shuffle(candidates)
            needed = candidates[: max(0, floor - pooled_rated)]
            to_extract += needed
            chosen |= set(needed)

        fresh = list(playable - pooled_movies - chosen)
        random.shuffle(fresh)
        top_up = fresh[: max(0, size - len(valid) - len(to_extract))]
        to_extract += top_up
        fresh = fresh[len(top_up) :]

        # A full pool rotates its oldest entries: retire each one now
        # and queue a replacement — an unpooled movie when any remain,
        # otherwise the same movie gets a brand-new random frame

        if len(valid) + len(to_extract) >= size:
            oldest = sorted(
                valid.items(), key=lambda item: item[1].get("extracted_at", 0)
            )[:rotate]
            for token, entry in oldest:
                _drop_entry(token)
                to_extract.append(fresh.pop() if fresh else entry["movie_id"])

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
            f"Frame pool: {len(valid)} pooled, {len(entries) - len(valid)} "
            f"pruned, {len(to_extract)} extractions queued"
        )
        return {"pooled": len(valid), "queued": len(to_extract)}


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
