"""Name That Frame (GitHub #52): the pre-extracted frame pool.

The game never runs ffmpeg at play time. A nightly task keeps a pool
of single frames, one per pooled movie. The task extracts each frame
from the best library copy of the film at a random moment. The game
page draws from the pool. The name of a frame is an opaque token,
because the filename must never show the answer. A Redis hash maps
each token to a movie. The images are in FRAME_POOL_DIR. Fitzflix
serves them through an authenticated route, not through the public
static path. The pool fills itself up to FRAME_POOL_SIZE. It retires
at least FRAME_POOL_ROTATE entries each night. It retires every frame
that a player was dealt first, then the oldest frames. Thus, every
film gets a turn. A film that was pooled for a long time gets a new
frame. A played-out frame gives its slot to a frame that nobody saw.
Each completed game round also replaces one pool entry immediately
(replace_frame_task). Thus, frames refresh continuously between the
nightly passes. Extraction runs on the transcode queue, the serial
heavy-I/O lane, because the shell cannot read /Volumes. Only workers
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

# Do not use the first and last parts of the runtime. Studio logos and
# opening titles identify the film too easily. End credits identify it
# completely.

OFFSET_LOW, OFFSET_HIGH = 0.05, 0.85

# Decode this many seconds forward into the extraction point. Do not
# trust the keyframe flags of the container. A VC-1 Blu-ray remux flags
# non-keyframes as seekable. An input-side seek that arrives on one
# decodes to a flat gray ghost frame (7 pooled frames, 2026-08-20).

SEEK_LEAD = 30

# A pixel at least this bright (0-255 luma) counts as picture. The value
# is high enough to ignore JPEG noise in the letterbox bars. It is low
# enough that a dim scene still counts. The Extra Difficult crop logic of
# the game (app/main/game.py) shares this value. That logic keeps its
# windows inside the active picture area of the frame.

ACTIVE_LUMA = 24

# An extracted frame must be at least this fraction picture. The
# extraction gets this many random offsets to find one. A fade-out, a
# hard cut, or an empty starfield extracts as a black rectangle. The
# Empire Strikes Back round of Glenn served one on 2026-08-27. No crop
# logic downstream can make content that is not there. Thus, a frame
# that is too dark never enters the pool.

FRAME_MIN_ACTIVE = 0.05
FRAME_ATTEMPTS = 4

# The per-user record of the frames that the game dealt. It is a sorted
# set of token -> the turn when the game served it. The game reads it
# to deal a frame that the player never saw. After a difficulty has no
# such frames, the game deals the least-recently-seen frame. The nightly
# pass reads the record to retire spent frames first. Thus, the frames
# that a player already saw are the first that rotation replaces (#200).

DEALT_KEY = "fitzflix:frames:dealt:{user_id}"

# The TTL is long enough that a lapsed player continues where they
# stopped. It is short enough that the record cannot outlive the pool
# that it points into.

DEALT_TTL = 60 * 24 * 3600


def dealt_key(user_id):
    """Return the key of the dealt-frames sorted set for one user."""

    return DEALT_KEY.format(user_id=user_id)


def _all_dealt_tokens():
    """Return every token that any player was dealt, the "spent" set."""

    dealt = set()
    for key in current_app.redis.scan_iter(DEALT_KEY.format(user_id="*")):
        dealt |= {token.decode() for token in current_app.redis.zrange(key, 0, -1)}
    return dealt


def _forget_dropped_tokens(pooled):
    """Remove the tokens that left the pool from the dealt record of every
    player.

    Thus, a rotated-out frame no longer counts as spent."""

    for key in current_app.redis.scan_iter(DEALT_KEY.format(user_id="*")):
        gone = [
            token
            for token in current_app.redis.zrange(key, 0, -1)
            if token.decode() not in pooled
        ]
        if gone:
            current_app.redis.zrem(key, *gone)


def frame_path(token):
    """Return the image path of the pooled frame for one token."""

    return os.path.join(current_app.config["FRAME_POOL_DIR"], f"{token}.jpg")


def pool_entries():
    """Return token -> {movie_id, extracted_at, offset} for every pooled
    frame.

    This function drops a malformed entry from the answer. The nightly
    prune removes it from the pool."""

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


def _frame_has_picture(path):
    """Return True if the extracted image has enough picture for a guess.

    Enough means at least FRAME_MIN_ACTIVE of its pixels above the luma
    bar. A file that does not decode also fails. It would only cause an
    error downstream."""

    from PIL import Image

    try:
        with Image.open(path) as image:
            histogram = image.convert("L").histogram()
    except (OSError, ValueError):
        return False
    total = sum(histogram)
    return total > 0 and sum(histogram[ACTIVE_LUMA + 1 :]) / total >= FRAME_MIN_ACTIVE


def _best_file(movie_id):
    """Return the best main-feature copy of the movie.

    The ranking is the same as on the card. Never select a fullscreen copy
    if a widescreen copy exists. Then select the best quality."""

    return (
        db.session.query(File, RefQuality)
        .join(RefQuality, RefQuality.id == File.quality_id)
        .filter(File.movie_id == movie_id)
        .filter(File.feature_type_id == None)
        .order_by(File.fullscreen.asc(), RefQuality.preference.desc())
        .first()
    )


def _probe_duration(file_path):
    """Return the container duration in seconds, or None. This reads only
    the header."""

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
    """Prune dead pool entries, then queue extractions to fill the pool up
    to FRAME_POOL_SIZE (nightly task).

    If the pool is full, this task rotates the oldest FRAME_POOL_ROTATE
    entries. It prefers movies that are not yet pooled. Thus, the whole
    library cycles through. Each reviewer gets at least
    FRAME_POOL_MIN_RATED pooled frames from their own diary (limited by
    the number of their rated films). Thus, Easy mode never runs short
    (requested by Glenn, 2026-08-20)."""

    from app.models import UserMovieReview

    with app.app_context():
        entries = pool_entries()

        # Prune an entry if the movie is gone, if its last main-feature
        # file is gone, or if the image is missing.

        playable = {
            movie_id
            for (movie_id,) in db.session.query(Movie.id).filter(
                Movie.files.any(File.feature_type_id.is_(None))
            )
            # The game never deals a home movie or a different recording
            # without a TMDB entry (#205). This also prunes the ones that
            # are already pooled.
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

        # Rotation runs BEFORE the reviewer floors below. It retires spent
        # frames before frames that are only old (#200). Both orders are
        # important. If the floors were measured first, rotation removed
        # the rated frames that the floors had just guaranteed. Easy then
        # settled a quarter under its minimum. If rotation retired by age
        # alone, frames that a player was already dealt stayed in the
        # pool. Films that the player never saw waited outside it.

        dealt = _all_dealt_tokens()
        fresh = list(playable - pooled_movies)
        random.shuffle(fresh)

        # A full pool retires every spent frame, and never fewer than
        # FRAME_POOL_ROTATE frames. Thus, the pool continues to turn over
        # also for a player who did not play the game.

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

        # Per-reviewer floors. A reviewer whose Easy world is below the
        # minimum gets extractions from their own unpooled rated films
        # before the general fill. A film that was just retired stays out
        # for the night. The purpose of the retirement was to show a
        # different film. Thus, the floor draws only on films that the pool
        # never served.

        floors = []
        chosen = set()
        reviewers = {
            user_id
            for (user_id,) in db.session.query(UserMovieReview.user_id).distinct()
        }
        for user_id in sorted(reviewers):
            # Rated means starred. This is the same as the world of the
            # game. An unrated diary row (for example, a Netflix-import
            # watch) does not count. If it did, the floor would pool films
            # that Easy cannot deal.
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
                # A reviewer with only some more rated films than the floor
                # can run out of unpooled films. The floor outranks the
                # retirement. The film comes back, on a new frame.

                spare = [movie_id for movie_id in retired if movie_id not in chosen]
                random.shuffle(spare)
                needed += spare[:short]
            floors += needed
            chosen |= set(needed)

        # Refill the room that the prune and the rotation left.
        # FRAME_POOL_SIZE is the limit. The floors are a composition rule
        # for the pool. They do not permit the pool to grow past the limit.
        # The slots go to the reviewer floors first. Then they go to films
        # that the pool never held. Only if the library has no such films
        # do the films that were just retired come back on new frames.

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

        # A token that just left the pool no longer counts as spent.

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
    """Replace one pool entry immediately after a round ends (per-round
    task, requested by Glenn, 2026-08-27).

    This task does not wait for the nightly pass. It extracts a frame from
    a playable film that the pool does not hold. If the whole library is
    pooled, it extracts a new frame of the played film. On success, it
    retires the old frame of the played film. Thus, the pool never grows
    past its size. The extraction runs on the transcode queue, seconds
    after the reveal. The reveal page has already rendered. Thus, the
    played token can safely leave the pool. If the extraction fails, the
    played frame stays. The nightly pass retires it."""

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
            # extract_frame_task only retires the old frames of its own
            # movie. Remove the frame of the played film here.
            for token in played_tokens:
                _drop_entry(token)
        return replacement


def extract_frame_task(movie_id):
    """Extract one random frame from the best copy of the movie into the
    pool (task).

    This task replaces an existing frame for the movie. There is one frame
    per film. Rotation refreshes it. If the extraction fails, the pool
    does not change. The next nightly pass tries again."""

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

        token = secrets.token_urlsafe(12)
        os.makedirs(current_app.config["FRAME_POOL_DIR"], exist_ok=True)
        out = frame_path(token)
        for attempt in range(FRAME_ATTEMPTS):
            offset = random.uniform(OFFSET_LOW, OFFSET_HIGH) * duration
            pre_seek = max(0.0, offset - SEEK_LEAD)
            try:
                # This is the recipe of Glenn from the issue, plus a sar
                # correction. Thus, an anamorphic DVD comes out at display
                # proportions. The seek has 2 stages: fast to SEEK_LEAD
                # seconds early, then accurate for the rest. Thus, the
                # decoder crosses a real keyframe on the way.
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
            if _frame_has_picture(out):
                break
            # A black rectangle is not a usable round (the Empire Strikes
            # Back report of Glenn, 2026-08-27). Try a different moment.
            try:
                os.remove(out)
            except OSError:
                pass
            current_app.logger.info(
                f"Frame from '{file.basename}' at {offset:.1f}s is nearly "
                f"all black; retrying at a fresh offset"
            )
        else:
            current_app.logger.warning(
                f"No usable frame found in '{file.basename}' after "
                f"{FRAME_ATTEMPTS} attempts; leaving the pool untouched"
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
