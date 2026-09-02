"""Play Name That Frame (GitHub #52). This module is the game itself.

A round draws from the pre-extracted pool (app/frames.py). The page
never touches ffmpeg. There are 4 difficulties, per the issues of
Glenn. Easy serves only the films that the current user has rated,
with 4 choices. Hard (slug "difficult") has 8 choices. Difficult (slug
"siracusa", renamed per #203) takes free text. Fitzflix fuzzy-matches
the text against the titles of the film. Extra Difficult (slug
"extra", #202) takes free text too. But it opens on a tight crop of
the frame. It scores by how early the correct guess arrives. The
points double if the user has not rated the named film. The
library-wide difficulties also deal rated films by default. Each has
an include-unrated switch (inverted as requested by Glenn,
2026-08-27). A plain /game visit opens again at the difficulty that
the user chose last. An authenticated route serves the frames. The key
of the route is the opaque token of the pool. Thus, the image URL and
the page markup do not leak the answer before a guess arrives."""

import hashlib
import io
import json
import random
import re
import secrets

from datetime import datetime
from difflib import SequenceMatcher

from flask import (
    abort,
    current_app,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    url_for,
)
from flask_login import current_user, login_required
from unidecode import unidecode

from app import db
from app.frames import (
    ACTIVE_LUMA,
    DEALT_TTL,
    POOL_KEY,
    dealt_key,
    frame_path,
    pool_entries,
)
from app.main import bp
from app.main.forms import GuessFrameForm
from app.models import Movie, MovieCast, UserFrameScore, UserMovieReview, movie_genres

# This maps a difficulty to its number of multiple-choice options.
# None means free text.

DIFFICULTIES = {"easy": 4, "difficult": 8, "siracusa": None, "extra": None}

# Extra Difficult (#202) zooms in. It does not widen the choices. The
# round opens on a tight crop. The player guesses or zooms out. A miss
# also zooms out. Thus, a round is a maximum of 3 looks at the same
# spot: the crop, a wider crop, and the full frame. They are worth 3,
# 2, and 1 points. The server holds the live round (token + stage).
# Thus, the image route cannot serve the full frame early.

EXTRA_STAGES = 3
EXTRA_ZOOM = {1: 0.3, 2: 0.6}  # crop side, as a fraction of the frame side
EXTRA_ROUND_KEY = "fitzflix:frames:extra:{user_id}"

# This is the number of top-billed cast members of the answer that
# anchor the shared-cast distractor tier (#201).

TOP_CAST_SIZE = 5

# By default, the library-wide difficulties deal only the films that
# the user has rated. This was inverted as requested by Glenn
# (2026-08-27). Rated-only was the mode that the players used. Thus,
# the switch now *widens* the deals to unrated films. It does not
# narrow them. The include-unrated flag is per user and per
# difficulty. A Redis set of slugs holds it, with no expiry. It is a
# preference, not round state. Easy never widens.

RATED_FILTER_DIFFICULTIES = ("difficult", "siracusa", "extra")
UNRATED_FILTER_KEY = "fitzflix:frames:unrated:{user_id}"

# A correct guess of an unrated film on Extra Difficult doubles the
# points of the stage (requested by Glenn, 2026-08-27). Fitzflix shows
# the bonus only after the guess arrives. A prompt that quotes doubled
# points would tell the player that the answer is an unrated film.

UNRATED_BONUS = 2

# The game opens again at the difficulty that the user chose last
# (requested by Glenn, 2026-08-27). Before, a plain /game visit reset
# the difficulty to Easy.

LAST_DIFFICULTY_KEY = "fitzflix:frames:difficulty:{user_id}"

# A zoomed Extra Difficult win earns bragging rights (requested by
# Glenn, 2026-08-27). Fitzflix cuts and stores the winning crop when
# it grades the guess. That is necessary because the round state
# clears, and the played frame is replaced (sometimes deleted) some
# seconds after the reveal. The store is PNG because the async
# Clipboard API only takes image/png. One hour is sufficient time to
# paste a brag.

BRAG_KEY = "fitzflix:frames:brag:{token}"
BRAG_TTL = 3600

# The distractors stay in the era of the answer (requested by Glenn,
# 2026-08-20). An option that is decades away gives the round away by
# deduction. Each difficulty tries its tight window first, then its
# wide window.

YEAR_WINDOWS = {"easy": (5, 10), "difficult": (2, 5)}

# This is how close a free-text (Difficult) guess must come to a real
# title, after normalization. It is loose enough for a typo. It is tight
# enough that a random film name does not score.

FUZZY_THRESHOLD = 0.75

TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{8,64}")

# Normalization folds number words to digits. Thus, 'Pelham 123'
# matches 'Pelham One Two Three' (reported by Glenn, 2026-08-27). Then
# the squeezed comparison pass bridges '1 2 3' and '123'. The fold
# applies to the guess and to the title. Thus, the direction is not
# important. 'apollo thirteen' names Apollo 13 in the same way.

NUMBER_WORDS = {
    "zero": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "ten": "10",
    "eleven": "11",
    "twelve": "12",
    "thirteen": "13",
    "fourteen": "14",
    "fifteen": "15",
    "sixteen": "16",
    "seventeen": "17",
    "eighteen": "18",
    "nineteen": "19",
    "twenty": "20",
    "thirty": "30",
    "forty": "40",
    "fifty": "50",
    "sixty": "60",
    "seventy": "70",
    "eighty": "80",
    "ninety": "90",
}


def _display_title(movie):
    """Return the site-wide display form: the TMDB title and year if known."""

    title = movie.tmdb_title or movie.title
    year = (
        movie.tmdb_release_date.strftime("%Y")
        if movie.tmdb_title and movie.tmdb_release_date
        else movie.year
    )
    return f"{title} ({year})"


def _normalize(text):
    """Fold a title for fuzzy comparison.

    This removes the accents, casefolds, removes the punctuation,
    removes a leading article, folds number words to digits, and
    collapses the whitespace."""

    text = unidecode(text or "").casefold()
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    words = text.split()
    if words and words[0] in ("the", "a", "an"):
        words = words[1:]
    words = [NUMBER_WORDS.get(word, word) for word in words]
    return " ".join(words)


def _fuzzy_match(guess, movie):
    """Return True if the guess is close enough to a title of the film.

    The titles are the local title and the TMDB title, with and without
    the year, and with and without a subtitle. The two halves around a
    ':' (or the filename-safe ' - ') stand alone. Thus, 'Rogue One'
    names 'Rogue One: A Star Wars Story', and 'Wrath of Khan' names its
    Star Trek film (reported by Glenn, 2026-08-27). This compares each
    pair 2 times: as normalized words, and with the spaces removed.
    Normalization turns punctuation into spaces. Thus, 'M*A*S*H'
    normalizes to 'm a s h'. Only the squeezed pass lets a player type
    'mash' (reported by Glenn, 2026-08-27)."""

    normalized = _normalize(guess)
    if not normalized:
        return False
    candidates = {movie.title, movie.tmdb_title, _display_title(movie)}
    for title in (movie.title, movie.tmdb_title):
        for separator in (":", " - "):
            if title and separator in title:
                head, tail = title.split(separator, 1)
                candidates.add(head)
                candidates.add(tail)
    for candidate in filter(None, candidates):
        candidate = _normalize(candidate)
        for pair in (
            (normalized, candidate),
            (
                normalized.replace(" ", ""),
                candidate.replace(" ", ""),
            ),
        ):
            if SequenceMatcher(None, *pair).ratio() >= FUZZY_THRESHOLD:
                return True
    return False


def _include_unrated(difficulty):
    """Return True if the user widened a difficulty to unrated films.

    This applies to the library-wide difficulties. Rated-only is the
    default (inverted as requested by Glenn, 2026-08-27)."""

    return difficulty in RATED_FILTER_DIFFICULTIES and bool(
        current_app.redis.sismember(
            UNRATED_FILTER_KEY.format(user_id=int(current_user.id)), difficulty
        )
    )


def _set_include_unrated(difficulty, on):
    """Persist the include-unrated switch for one difficulty."""

    key = UNRATED_FILTER_KEY.format(user_id=int(current_user.id))
    if on:
        current_app.redis.sadd(key, difficulty)
    else:
        current_app.redis.srem(key, difficulty)


def _rated_only(difficulty):
    """Return True if the deals of a difficulty narrow to rated films.

    This applies to the library-wide difficulties. It is the default,
    unless the user opted in the unrated films. Easy handles its
    always-rated world separately."""

    return difficulty in RATED_FILTER_DIFFICULTIES and not _include_unrated(difficulty)


def _rated_movie_ids():
    """Return the movies that the current user RATED.

    A rating is a star rating, not only a diary row. The world of Easy
    and the rated-films filter read this. Before, this accepted each
    diary entry. But the Netflix history import seeded unrated watches
    that nobody remembers (reported by Glenn for The Conversation,
    2026-08-27). Thus, a bare watch no longer counts."""

    return {
        movie_id
        for (movie_id,) in db.session.query(UserMovieReview.movie_id)
        .filter(UserMovieReview.user_id == int(current_user.id))
        .filter(UserMovieReview.movie_id.isnot(None))
        .filter(UserMovieReview.rating.isnot(None))
    }


def _display_year(year, tmdb_title, tmdb_release_date):
    """Return the year that the site shows for a film.

    This is the TMDB year if the TMDB title rules. In all other cases,
    it is the local year."""

    return tmdb_release_date.year if tmdb_title and tmdb_release_date else year


def _shared_cast_movie_ids(answer_id):
    """Return the movies that credit a top-billed cast member of the answer.

    These distractors prevent a known face from naming the film."""

    top_cast = [
        credit_id
        for (credit_id,) in db.session.query(MovieCast.credit_id)
        .filter(MovieCast.movie_id == answer_id)
        .filter(MovieCast.credit_id.isnot(None))
        .order_by(MovieCast.billing_order)
        .limit(TOP_CAST_SIZE)
    ]
    if not top_cast:
        return set()
    return {
        movie_id
        for (movie_id,) in db.session.query(MovieCast.movie_id)
        .filter(MovieCast.credit_id.in_(top_cast))
        .filter(MovieCast.movie_id != answer_id)
        .distinct()
    }


def _shared_genre_movie_ids(answer_id):
    """Return the movies that share a TMDB genre with the answer."""

    answer_genres = db.session.query(movie_genres.c.genre_id).filter(
        movie_genres.c.movie_id == answer_id
    )
    return {
        movie_id
        for (movie_id,) in db.session.query(movie_genres.c.movie_id)
        .filter(movie_genres.c.genre_id.in_(answer_genres))
        .filter(movie_genres.c.movie_id != answer_id)
        .distinct()
    }


def _build_options(answer_id, difficulty, rated_only=False):
    """Return the shuffled multiple-choice list of the round.

    The list is the answer plus random distractors. The distractors
    come from the ladder of Glenn (#201). First come the films that
    share the top-billed cast of the answer, in its era. One Star Trek
    film among strangers gives the round away through the ears of
    Spock. Then come the same-genre films in the era, then each film in
    the era, then the same-genre films outside the era. Each rung tries
    the tight year window of the difficulty before the wide window
    (±5 then ±10 on Easy, ±2 then ±5 on Hard, the rule of Glenn,
    2026-08-20). An option that is decades away gives the answer away.
    Easy walks the ladder over the rated films of the user first. The
    same applies to a difficulty with the rated-films filter on,
    because an unrated option would mark the answer by elimination.
    Then it pads from the whole library. The last resort is any film.
    Thus, a round can always fill its slots."""

    count = DIFFICULTIES[difficulty]
    answer = db.session.get(Movie, answer_id)
    answer_year = _display_year(
        answer.year, answer.tmdb_title, answer.tmdb_release_date
    )
    years = {
        movie_id: _display_year(year, tmdb_title, tmdb_release_date)
        for movie_id, year, tmdb_title, tmdb_release_date in db.session.query(
            Movie.id, Movie.year, Movie.tmdb_title, Movie.tmdb_release_date
        )
        .filter(Movie.id != answer_id)
        .filter(Movie.tmdb_id.isnot(None))
    }
    rated = (
        _rated_movie_ids() - {answer_id}
        if difficulty == "easy" or rated_only
        else set()
    )

    def in_window(span):
        return [
            movie_id
            for movie_id, year in years.items()
            if year is not None
            and answer_year is not None
            and abs(year - answer_year) <= span
        ]

    # This is the base ladder: shared cast in the era, shared genre in
    # the era, each film in the era, shared genre out of the era. Each
    # rung tries the tight window before the wide window. The world of
    # Easy stays the rated films. It walks the whole ladder over them,
    # per the fallback of Glenn. Then it pads from the unrated library
    # in the same way.

    cast_mates = _shared_cast_movie_ids(answer_id)
    genre_mates = _shared_genre_movie_ids(answer_id)
    tight, wide = YEAR_WINDOWS[difficulty]
    ladder = [
        [m for m in in_window(tight) if m in cast_mates],
        [m for m in in_window(wide) if m in cast_mates],
        [m for m in in_window(tight) if m in genre_mates],
        [m for m in in_window(wide) if m in genre_mates],
        in_window(tight),
        in_window(wide),
        [m for m in years if m in genre_mates],
    ]
    tiers = []
    if rated:
        tiers += [[m for m in tier if m in rated] for tier in ladder]
    tiers += ladder + [list(years)]

    distractors = []
    for tier in tiers:
        pool = [m for m in tier if m not in distractors]
        random.shuffle(pool)
        distractors += pool[: count - 1 - len(distractors)]
        if len(distractors) == count - 1:
            break

    ids = distractors + [answer_id]
    random.shuffle(ids)
    movies = {movie.id: movie for movie in Movie.query.filter(Movie.id.in_(ids)).all()}
    return [(movie_id, _display_title(movies[movie_id])) for movie_id in ids]


def _round_tokens(difficulty, rated_only=False):
    """Return the pooled tokens that this difficulty can serve.

    The world of Easy is always the rated films of the user. The other
    difficulties narrow to the same world if the rated-films filter is
    on."""

    entries = pool_entries()
    if difficulty == "easy" or rated_only:
        rated = _rated_movie_ids()
        return {
            token: entry
            for token, entry in entries.items()
            if entry.get("movie_id") in rated
        }
    return entries


def _score_row(difficulty):
    """Return the standings row of the user for one difficulty.

    This creates the row on first use. The database keeps the current
    streak and the personal best. Thus, the scores survive sessions and
    devices (requested by Glenn, 2026-08-20)."""

    row = UserFrameScore.query.filter_by(
        user_id=int(current_user.id), difficulty=difficulty
    ).first()
    if row is None:
        row = UserFrameScore(
            user_id=int(current_user.id),
            difficulty=difficulty,
            current_streak=0,
            best_streak=0,
        )
        db.session.add(row)
    return row


def _deal_token(tokens):
    """Pick the frame of this round without repeats.

    Glenn reported a repeat with Finding Nemo (2026-08-20). A per-user
    sorted set stamps each dealt frame with its turn number. A deal
    prefers the frames that the set never held. When a difficulty has
    no more of those, the frames come around again. But they come
    least-recently-seen first, not at random (#200). A frame never
    comes 2 times in a row, because the server remembers the last-dealt
    frame. Thus, a plain page visit cannot repeat it. The record is
    shared across the difficulties. A frame seen on Easy is spoiled for
    Difficult too. The nightly pass reads the record to retire the
    spent frames first. It forgets a token when the token leaves the
    pool."""

    if not tokens:
        return None
    user_id = int(current_user.id)
    key = dealt_key(user_id)
    last_key = f"fitzflix:frames:last:{user_id}"
    last = (current_app.redis.get(last_key) or b"").decode()
    served = {
        token.decode(): score
        for token, score in current_app.redis.zrange(key, 0, -1, withscores=True)
    }
    unseen = [token for token in tokens if token not in served and token != last]
    if unseen:
        token = random.choice(unseen)
    else:
        # The difficulty has lapped. Replay it oldest first. Break the
        # ties at random. Thus, a lapped pool is not a fixed carousel.
        repeats = [token for token in tokens if token != last] or list(tokens)
        random.shuffle(repeats)
        token = min(repeats, key=lambda token: served.get(token, 0))
    current_app.redis.zadd(key, {token: max(served.values(), default=0) + 1})
    current_app.redis.expire(key, DEALT_TTL)
    current_app.redis.set(last_key, token, ex=DEALT_TTL)
    return token


def _enqueue_frame_replacement(movie):
    """Queue the per-round pool top-up (requested by Glenn, 2026-08-27).

    After Fitzflix grades a round, the transcode queue swaps the frame
    of its film for a frame of a film that is not in the pool. Thus,
    the pool turns over continuously and does not wait for the nightly
    pass. This runs only on a reveal. A skipped frame keeps its slot."""

    current_app.transcode_queue.enqueue(
        "app.frames.replace_frame_task",
        args=(movie.id,),
        job_timeout=600,
        description=f"Replacing the played frame from '{movie.title}'",
    )


def _extra_round():
    """Return the live Extra Difficult round of the user, or None.

    The round is {token, stage}. The server-side state makes the stages
    honest. The posted stage never decides the points. The image route
    never serves more of the frame than the round has earned."""

    payload = current_app.redis.get(
        EXTRA_ROUND_KEY.format(user_id=int(current_user.id))
    )
    if not payload:
        return None
    try:
        round_ = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        return None
    return round_ if isinstance(round_, dict) and round_.get("token") else None


def _save_extra_round(token, stage):
    """Store the token and the stage of the live round of the current user."""

    current_app.redis.set(
        EXTRA_ROUND_KEY.format(user_id=int(current_user.id)),
        json.dumps({"token": token, "stage": stage}),
        ex=DEALT_TTL,
    )


def _clear_extra_round():
    """Delete the live round of the current user. It ended or was skipped."""

    current_app.redis.delete(EXTRA_ROUND_KEY.format(user_id=int(current_user.id)))


# A crop window must be at least this fraction of real picture. Real
# picture means pixels above ACTIVE_LUMA (defined with the extraction
# floor in app/frames.py). The active-area box is a bounding box, not
# a mask. The box of a starfield spans the whole frame. But a window
# between the stars is still black (reported by Glenn for The Empire
# Strikes Back, 2026-08-27). Thus, the centre looks for light before
# Fitzflix cuts the crop.

CROP_MIN_ACTIVE = 0.10


def _active_picture_box(image):
    """Return the bounding box of the real picture of the frame.

    The real picture is each pixel brighter than near-black. A
    letterboxed or pillarboxed transfer bakes its bars into the frame.
    A zoom window that arrives on a bar is a wall of black (reported by
    Glenn, 2026-08-27). Thus, the Extra Difficult crops stay in this
    box. Return None if no pixel is above the limit. Then the caller
    uses the full frame."""

    mask = image.convert("L").point(lambda v: 255 if v > ACTIVE_LUMA else 0)
    return mask.getbbox()


def _crop_box(token, stage, width, height, active=None, centre=None):
    """Return the crop rectangle of the stage.

    The zoom of the stage sets the size. The centre is a point hashed
    from the token. It can be anywhere in the active picture area of
    the frame (the whole frame if there is no `active` box). The
    rectangle is clamped to the edges of that area. The clamp keeps the
    stages nested for each centre. Thus, a zoom out always shows more
    of the same spot. An early version pinned the centre where the
    widest stage fit without a clamp. That confined each crop to the
    middle fifth of the frame (noticed by Glenn, 2026-08-27). A caller
    that has looked at the pixels can pass its own `centre` (the 2
    coordinates as 0-1 fractions). _crop_centre does that to steer the
    window onto real picture."""

    bound_left, bound_top, bound_right, bound_bottom = active or (0, 0, width, height)
    span_w = bound_right - bound_left
    span_h = bound_bottom - bound_top
    fraction = EXTRA_ZOOM[stage]
    digest = hashlib.sha256(token.encode()).digest()
    centre_x, centre_y = centre or (digest[0] / 255, digest[1] / 255)
    crop_w = max(1, int(span_w * fraction))
    crop_h = max(1, int(span_h * fraction))
    left = min(
        max(int(bound_left + centre_x * span_w - crop_w / 2), bound_left),
        bound_right - crop_w,
    )
    top = min(
        max(int(bound_top + centre_y * span_h - crop_h / 2), bound_top),
        bound_bottom - crop_h,
    )
    return left, top, left + crop_w, top + crop_h


def _crop_centre(token, image, active):
    """Return the crop centre of the round.

    This is the first token-hashed candidate whose stage-1 window holds
    enough real picture. If no candidate does, it is the brightest
    candidate. Each stage shares the one centre. Thus, the crops stay
    nested. The choice is a pure function of the token and the frame.
    Thus, a reload serves the same window. Without this search, a
    window could arrive on the black space *inside* the active area.
    The bounding box of a starfield spans the frame (reported by Glenn
    for The Empire Strikes Back, 2026-08-27)."""

    digest = hashlib.sha256(token.encode()).digest()
    grey = image.convert("L")
    best, best_fraction = None, -1.0
    for n in range(0, len(digest) - 1, 2):
        centre = (digest[n] / 255, digest[n + 1] / 255)
        box = _crop_box(token, 1, *image.size, active, centre=centre)
        histogram = grey.crop(box).histogram()
        total = sum(histogram)
        fraction = sum(histogram[ACTIVE_LUMA + 1 :]) / total if total else 0.0
        if fraction >= CROP_MIN_ACTIVE:
            return centre
        if fraction > best_fraction:
            best, best_fraction = centre, fraction
    return best


def _stage_crop_image(token, stage):
    """Return the window of the stage onto the pooled frame as a PIL image.

    The round page and the brag store serve this piece. Return None if
    the pooled image does not decode."""

    from PIL import Image

    try:
        with Image.open(frame_path(token)) as image:
            active = _active_picture_box(image)
            centre = _crop_centre(token, image, active)
            box = _crop_box(token, stage, *image.size, active, centre=centre)
            return image.crop(box).convert("RGB")
    except (OSError, ValueError):
        return None


def _cropped_frame_response(token, stage):
    """Return the Extra Difficult crop as a response.

    The crop is the window of the stage onto the pooled frame. The
    server cuts it. The full frame never reaches the page before stage
    3. Return None if the image does not decode. Then the route can
    serve the full frame and not return a 500 on a bad pool entry."""

    crop = _stage_crop_image(token, stage)
    if crop is None:
        return None
    buffer = io.BytesIO()
    crop.save(buffer, format="JPEG", quality=90)
    buffer.seek(0)
    # Do not cache. The same token serves different pixels as the round
    # advances. A crop is cheap to cut again.
    return send_file(buffer, mimetype="image/jpeg", max_age=0)


def _stash_brag_crop(token, stage):
    """Cut the crop of the winning stage and store it under a share token.

    The brag buttons of the reveal use the share token. This runs
    synchronously at grading time, before the frame replacement can
    delete the pooled image. Return None if the image does not
    decode."""

    crop = _stage_crop_image(token, stage)
    if crop is None:
        return None
    buffer = io.BytesIO()
    crop.save(buffer, format="PNG")
    share_token = secrets.token_urlsafe(12)
    current_app.redis.set(
        BRAG_KEY.format(token=share_token), buffer.getvalue(), ex=BRAG_TTL
    )
    return share_token


def _render_extra_round(token, stage, form, missed=None):
    """Render the Extra Difficult guessing page at one stage.

    The player reaches the page with a new deal, with a zoom out, or
    with a mid-round miss. A miss shows the wrong guess."""

    score = UserFrameScore.query.filter_by(
        user_id=int(current_user.id), difficulty="extra"
    ).first()
    return render_template(
        "game.html",
        title="Name That Frame",
        difficulty="extra",
        difficulties=DIFFICULTIES,
        result=None,
        token=token,
        options=None,
        stage=stage,
        stage_points=EXTRA_STAGES + 1 - stage,
        missed_guess=missed,
        include_unrated=_include_unrated("extra"),
        rated_only=_rated_only("extra"),
        streak=score.current_streak if score else 0,
        best=score.best_streak if score else 0,
        points=(score.points or 0) if score else 0,
        seen=(score.rounds_seen or 0) if score else 0,
        won=(score.rounds_won or 0) if score else 0,
        form=form,
        pool_size=len(pool_entries()),
    )


def _extra_post(form, token, movie):
    """Grade one Extra Difficult action.

    A Zoom Out or a mid-round miss advances the stage. A hit banks the
    points of the stage. A stage-3 miss ends the round and the streak.
    A give-up after the first zoom out does the same (#202). The
    surrender rule was requested by Glenn (2026-08-27): a started round
    is won or lost."""

    round_ = _extra_round()
    if round_ is None or round_.get("token") != token:
        # The posted round is not the live round. The tab is stale, or
        # the user skipped the round in a different tab. Deal or resume.
        # Do not grade against the wrong stage.
        return redirect(url_for("main.name_that_frame", difficulty="extra"))
    stage = min(int(round_.get("stage") or 1), EXTRA_STAGES)

    if form.zoom_out.data:
        stage = min(stage + 1, EXTRA_STAGES)
        _save_extra_round(token, stage)
        return _render_extra_round(token, stage, form)

    gave_up = bool(form.give_up.data)
    guessed = "" if gave_up else (form.guess.data or "").strip()
    correct = False if gave_up else _fuzzy_match(guessed, movie)
    if not gave_up and not correct and stage < EXTRA_STAGES:
        # A mid-round miss zooms out. It does not end the round. The
        # wrong guess buys the same look as a Zoom Out.
        stage += 1
        _save_extra_round(token, stage)
        return _render_extra_round(token, stage, form, missed=guessed or None)

    _clear_extra_round()
    # A correct guess while zoomed in is the feat worth a brag. A
    # full-frame win is only a win.
    brag = _stash_brag_crop(token, stage) if correct and stage < EXTRA_STAGES else None
    points_won = EXTRA_STAGES + 1 - stage if correct else 0
    # The unrated bonus stays secret until it applies. The round prompt
    # quoted the base points. Only now can the reveal say that the
    # player had not rated the film.
    doubled = bool(points_won) and movie.id not in _rated_movie_ids()
    if doubled:
        points_won *= UNRATED_BONUS
    score = _score_row("extra")
    score.points = (score.points or 0) + points_won
    score.current_streak = score.current_streak + 1 if correct else 0
    if correct:
        score.rounds_won = (score.rounds_won or 0) + 1
    new_best = correct and score.current_streak > score.best_streak
    if new_best:
        score.best_streak = score.current_streak
        score.date_best = datetime.now()
    db.session.commit()
    _enqueue_frame_replacement(movie)

    return render_template(
        "game.html",
        title="Name That Frame",
        difficulty="extra",
        difficulties=DIFFICULTIES,
        result={
            "correct": correct,
            "guess": guessed or None,
            "movie_id": movie.id,
            "answer": _display_title(movie),
            "new_best": new_best,
            "points_won": points_won,
            "doubled": doubled,
            "brag": brag,
        },
        answer_movie=movie,
        token=token,
        options=None,
        stage=None,
        include_unrated=_include_unrated("extra"),
        rated_only=_rated_only("extra"),
        streak=score.current_streak,
        best=score.best_streak,
        points=score.points or 0,
        seen=(score.rounds_seen or 0),
        won=(score.rounds_won or 0),
        form=form,
        pool_size=len(pool_entries()),
    )


@bp.route("/game", methods=["GET", "POST"])
@login_required
def name_that_frame():
    """Serve one round per page: a pooled frame, a guess form, the reveal.

    The token names the round. The answer lives only on the server, in
    the pool hash."""

    form = GuessFrameForm()

    if request.method == "POST" and form.validate_on_submit():
        difficulty = (
            form.difficulty.data if form.difficulty.data in DIFFICULTIES else "easy"
        )
        token = form.token.data or ""
        entry = pool_entries().get(token)
        movie = db.session.get(Movie, entry["movie_id"]) if entry else None
        if movie is None:
            # The frame of the round left the pool during the game. Deal
            # a new frame. Do not return an error.
            return redirect(url_for("main.name_that_frame", difficulty=difficulty))

        if difficulty == "extra":
            return _extra_post(form, token, movie)

        if DIFFICULTIES[difficulty] is None:
            guessed = (form.guess.data or "").strip()
            correct = _fuzzy_match(guessed, movie)
            guess_shown = guessed or None
        else:
            guessed = (form.choice.data or "").strip()
            correct = guessed.isdigit() and int(guessed) == movie.id
            chosen = db.session.get(Movie, int(guessed)) if guessed.isdigit() else None
            guess_shown = _display_title(chosen) if chosen else None

        score = _score_row(difficulty)
        score.current_streak = score.current_streak + 1 if correct else 0
        if correct:
            score.rounds_won = (score.rounds_won or 0) + 1
        new_best = correct and score.current_streak > score.best_streak
        if new_best:
            score.best_streak = score.current_streak
            score.date_best = datetime.now()
        db.session.commit()
        _enqueue_frame_replacement(movie)

        return render_template(
            "game.html",
            title="Name That Frame",
            difficulty=difficulty,
            difficulties=DIFFICULTIES,
            result={
                "correct": correct,
                "guess": guess_shown,
                "movie_id": movie.id,
                "answer": _display_title(movie),
                "new_best": new_best,
            },
            # The reveal shows the answer as a standard poster tile: the
            # popover card plus the ladder and the watchlist toggle.
            # Thus, the player can rate or watchlist an interesting film
            # immediately (requested by Glenn, 2026-08-20).
            answer_movie=movie,
            token=token,
            options=None,
            stage=None,
            include_unrated=_include_unrated(difficulty),
            rated_only=_rated_only(difficulty),
            streak=score.current_streak,
            best=score.best_streak,
            points=(score.points or 0),
            seen=(score.rounds_seen or 0),
            won=(score.rounds_won or 0),
            form=form,
            pool_size=len(pool_entries()),
        )

    # A plain /game visit opens again at the difficulty that the user
    # chose last (requested by Glenn, 2026-08-27). It is a preference.
    # Thus, it has no expiry.
    difficulty = request.args.get("difficulty")
    last_key = LAST_DIFFICULTY_KEY.format(user_id=int(current_user.id))
    if difficulty in DIFFICULTIES:
        current_app.redis.set(last_key, difficulty)
    else:
        stored = (current_app.redis.get(last_key) or b"").decode()
        difficulty = stored if stored in DIFFICULTIES else "easy"

    # This is the include-unrated switch. The checkbox always submits a
    # hidden unrated=0 with a checked unrated=1. Thus, a missing "1" is
    # a deliberate un-tick, not a bare visit.
    if difficulty in RATED_FILTER_DIFFICULTIES and "unrated" in request.args:
        _set_include_unrated(difficulty, "1" in request.args.getlist("unrated"))
    rated_only = _rated_only(difficulty)

    tokens = _round_tokens(difficulty, rated_only)
    stage = None
    dealt = False
    if difficulty == "extra":
        # A visit resumes the live round at its stage. It does not deal.
        # A refresh must not be a free zoom reset. Thus, Skip abandons
        # the round explicitly. But only an untouched round skips. After
        # the first zoom out, the round is won, lost, or given up (the
        # rule of Glenn, 2026-08-27). Thus, a hand-typed ?skip=1 cannot
        # avoid the loss.
        round_ = _extra_round()
        if request.args.get("skip") and round_ and int(round_.get("stage") or 1) <= 1:
            _clear_extra_round()
            round_ = None
        if round_ and round_.get("token") in tokens:
            token = round_["token"]
            stage = min(int(round_.get("stage") or 1), EXTRA_STAGES)
        else:
            token = _deal_token(tokens)
            stage = 1
            dealt = token is not None
            if token:
                _save_extra_round(token, stage)
            else:
                _clear_extra_round()
    else:
        token = _deal_token(tokens)
        dealt = token is not None
    options = None
    if token and DIFFICULTIES[difficulty] is not None:
        options = _build_options(tokens[token]["movie_id"], difficulty, rated_only)

    if dealt:
        # Each dealt frame counts as seen (the win-rate rule of Glenn,
        # 2026-08-27). In a skipped or refreshed round, the player looked
        # at the frame and did not name it.
        score = _score_row(difficulty)
        score.rounds_seen = (score.rounds_seen or 0) + 1
        db.session.commit()
    else:
        score = UserFrameScore.query.filter_by(
            user_id=int(current_user.id), difficulty=difficulty
        ).first()

    return render_template(
        "game.html",
        title="Name That Frame",
        difficulty=difficulty,
        difficulties=DIFFICULTIES,
        result=None,
        token=token,
        options=options,
        stage=stage,
        stage_points=(EXTRA_STAGES + 1 - stage) if stage else None,
        missed_guess=None,
        include_unrated=_include_unrated(difficulty),
        rated_only=rated_only,
        streak=score.current_streak if score else 0,
        best=score.best_streak if score else 0,
        points=(score.points or 0) if score else 0,
        seen=(score.rounds_seen or 0) if score else 0,
        won=(score.rounds_won or 0) if score else 0,
        form=form,
        pool_size=len(pool_entries()),
    )


@bp.route("/game/brag/<token>")
@login_required
def game_brag(token):
    """Serve the stored winning crop of a zoomed round, by its share token.

    The brag buttons of the reveal copy or share this image. The store
    survives the replacement of the played frame, but only for
    BRAG_TTL. After that, the brag is a 404. The page reports that as
    expired."""

    if not TOKEN_PATTERN.fullmatch(token):
        abort(404)
    payload = current_app.redis.get(BRAG_KEY.format(token=token))
    if not payload:
        abort(404)
    return send_file(io.BytesIO(payload), mimetype="image/png", max_age=0)


@bp.route("/game/frame/<token>")
@login_required
def game_frame(token):
    """Serve one pooled frame, by its opaque token.

    This route needs authentication. A library frame never goes through
    the public static path. Only a token that the pool holds resolves.
    While the token is the live Extra Difficult round of the user, the
    server-side stage decides how much of the frame serves (#202). The
    ?stage in the URL of the page is only a cache-buster. Thus, a
    request for a later stage gives nothing extra."""

    if not TOKEN_PATTERN.fullmatch(token) or not current_app.redis.hexists(
        POOL_KEY, token
    ):
        abort(404)
    round_ = _extra_round()
    if round_ and round_.get("token") == token:
        stage = min(int(round_.get("stage") or 1), EXTRA_STAGES)
        if stage < EXTRA_STAGES:
            response = _cropped_frame_response(token, stage)
            if response is not None:
                return response
    return send_from_directory(
        current_app.config["FRAME_POOL_DIR"], f"{token}.jpg", max_age=3600
    )
