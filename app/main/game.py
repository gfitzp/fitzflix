"""Name that Frame (GitHub #52): the guessing game itself.

Rounds draw from the pre-extracted pool (app/frames.py) — the page
never touches ffmpeg. Four difficulties, per Glenn's issues: Easy
serves only films the current user has rated, with four choices;
Hard (slug "difficult") serves the whole pooled library with eight;
Difficult (slug "siracusa", renamed per #203)
serves the whole library and takes free text, fuzzy-matched against
the film's titles; Extra Difficult (slug "extra", #202) is free text
too, but opens on a tight crop of the frame and scores by how early
the guess lands. Frames are served through an authenticated route
keyed by the pool's opaque tokens, so neither the image URL nor the
page markup leaks the answer before a guess lands."""

import hashlib
import io
import json
import random
import re

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
from app.frames import DEALT_TTL, POOL_KEY, dealt_key, frame_path, pool_entries
from app.main import bp
from app.main.forms import GuessFrameForm
from app.models import Movie, MovieCast, UserFrameScore, UserMovieReview, movie_genres

# Difficulty → number of multiple-choice options (None = free text)

DIFFICULTIES = {"easy": 4, "difficult": 8, "siracusa": None, "extra": None}

# Extra Difficult (#202) zooms in instead of widening the choices: the
# round opens on a tight crop and the player either guesses or zooms
# out. A miss zooms out too, so a round is up to three looks at the
# same spot — crop, wider crop, full frame — worth 3, 2, and 1 points.
# The live round (token + stage) is held server-side so the image
# route can't be talked into serving the full frame early.

EXTRA_STAGES = 3
EXTRA_ZOOM = {1: 0.3, 2: 0.6}  # crop side, as a fraction of the frame's
EXTRA_ROUND_KEY = "fitzflix:frames:extra:{user_id}"

# How many of the answer's top-billed cast anchor the shared-cast
# distractor tier (#201)

TOP_CAST_SIZE = 5

# The library-wide difficulties can narrow to films the user has
# rated (Glenn's ask, Aug 27 2026 — tightened from "seen" the same
# day: unrated Netflix-import watches kept surfacing films nobody
# remembers): a per-user, per-difficulty switch, held in a Redis set
# of slugs with no expiry — it's a preference, not round state. Easy
# is already that filter by definition.

RATED_FILTER_DIFFICULTIES = ("difficult", "siracusa", "extra")
RATED_FILTER_KEY = "fitzflix:frames:ratedonly:{user_id}"

# Distractors stay within the answer's era (Glenn, Aug 20 2026: an
# option decades away hands the round to process of deduction) —
# each difficulty tries its tight window first, then its widened one

YEAR_WINDOWS = {"easy": (5, 10), "difficult": (2, 5)}

# How close a free-text (Difficult) guess must come to a real title, after
# normalization — loose enough for a typo, tight enough that a random
# film name doesn't score

FUZZY_THRESHOLD = 0.75

TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{8,64}")


def _display_title(movie):
    """The site-wide display grammar: TMDB title and year when known."""

    title = movie.tmdb_title or movie.title
    year = (
        movie.tmdb_release_date.strftime("%Y")
        if movie.tmdb_title and movie.tmdb_release_date
        else movie.year
    )
    return f"{title} ({year})"


def _normalize(text):
    """Fold a title for fuzzy comparison: unaccent, casefold, drop
    punctuation, strip a leading article, collapse whitespace."""

    text = unidecode(text or "").casefold()
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    words = text.split()
    if words and words[0] in ("the", "a", "an"):
        words = words[1:]
    return " ".join(words)


def _fuzzy_match(guess, movie):
    """True when the guess lands close enough to any of the film's
    titles — local or TMDB, with or without the year, and with or
    without a subtitle: both halves around a ':' (or the
    filename-safe ' - ') stand alone, so 'Rogue One' names 'Rogue
    One: A Star Wars Story' and 'Wrath of Khan' names its Star Trek
    (Glenn's reports, Aug 27 2026). Each pair is compared
    twice: as normalized words, and with the spaces squeezed out —
    normalization turns punctuation into spaces, so 'M*A*S*H'
    normalizes to 'm a s h' and only the squeezed pass lets a player
    type 'mash' (Glenn's report, Aug 27 2026)."""

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


def _rated_only(difficulty):
    """Whether the user has the rated-films filter on for a difficulty
    that offers it."""

    return difficulty in RATED_FILTER_DIFFICULTIES and bool(
        current_app.redis.sismember(
            RATED_FILTER_KEY.format(user_id=int(current_user.id)), difficulty
        )
    )


def _set_rated_only(difficulty, on):
    """Persist the rated-films switch for one difficulty."""

    key = RATED_FILTER_KEY.format(user_id=int(current_user.id))
    if on:
        current_app.redis.sadd(key, difficulty)
    else:
        current_app.redis.srem(key, difficulty)


def _rated_movie_ids():
    """Movies the current user has actually RATED — a star rating, not
    just a diary row. Easy's world and the rated-films filter both
    read this; it used to accept any diary entry, but the Netflix
    history import seeded unrated watches nobody remembers (Glenn's
    Conversation report, Aug 27 2026), so a bare watch no longer
    counts."""

    return {
        movie_id
        for (movie_id,) in db.session.query(UserMovieReview.movie_id)
        .filter(UserMovieReview.user_id == int(current_user.id))
        .filter(UserMovieReview.movie_id.isnot(None))
        .filter(UserMovieReview.rating.isnot(None))
    }


def _display_year(year, tmdb_title, tmdb_release_date):
    """The year the site displays for a film — TMDB's when it rules
    the title, the local one otherwise."""

    return tmdb_release_date.year if tmdb_title and tmdb_release_date else year


def _shared_cast_movie_ids(answer_id):
    """Movies crediting any of the answer's top-billed cast — the
    distractors that keep a familiar face from naming the film."""

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
    """Movies sharing any of the answer's TMDB genres."""

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
    """The round's shuffled multiple-choice list: the answer plus
    random distractors, drawn down Glenn's ladder (#201): films
    sharing the answer's top-billed cast within its era first — one
    Star Trek film among strangers hands the round to Spock's ears —
    then same-genre films within the era, any film within the era,
    and same-genre films outside it. Each rung tries the difficulty's
    tight year window before the widened one (±5→±10 on Easy, ±2→±5
    on Hard; Glenn's rule, Aug 20 2026), since an option decades away
    is its own giveaway. Easy — and any difficulty with the
    rated-films filter on, where an unrated option would mark the
    answer by elimination — walks the ladder over the user's rated
    films first before padding from the whole library, and
    anything-goes is the last resort so a round can always fill its
    slots."""

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

    # The base ladder: shared cast in era, shared genre in era, any
    # film in era, shared genre out of era — tight window before wide
    # at every rung. Easy's universe stays the rated films, walking
    # the whole ladder over them per Glenn's fallback before padding
    # from the unrated library the same way.

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
    """The pooled tokens this difficulty may serve — Easy's world is
    the user's rated films always, and the other difficulties narrow
    to the same world when the rated-films filter is on."""

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
    """The user's standings row for one difficulty, created on first
    use — the DB keeps the running streak and the personal best, so
    scores survive sessions and devices (Glenn's ask, Aug 20 2026)."""

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
    """Pick this round's frame without repeats (Glenn's Finding Nemo
    report, Aug 20 2026): every dealt frame is stamped with its turn
    number in a per-user sorted set, and deals prefer frames that set
    has never held. When a difficulty runs out of those the frames do
    come around again, but least-recently-seen first rather than at
    random (#200) — and never twice in a row, since the last-dealt
    frame is remembered server-side, so a plain page visit can't echo
    it either. The record is shared across difficulties (a frame seen
    on Easy is spoiled for Difficult too); the nightly pass reads it to
    retire spent frames first, and forgets a token once it leaves the
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
        # The difficulty has lapped: replay it oldest-first, breaking
        # ties at random so a lapped pool isn't a fixed carousel
        repeats = [token for token in tokens if token != last] or list(tokens)
        random.shuffle(repeats)
        token = min(repeats, key=lambda token: served.get(token, 0))
    current_app.redis.zadd(key, {token: max(served.values(), default=0) + 1})
    current_app.redis.expire(key, DEALT_TTL)
    current_app.redis.set(last_key, token, ex=DEALT_TTL)
    return token


def _enqueue_frame_replacement(movie):
    """Queue the per-round pool top-up (Glenn's ask, Aug 27 2026):
    once a round is graded, its film's frame gets swapped for a frame
    of an unpooled film on the transcode queue — the pool turns over
    continuously instead of waiting for the nightly pass. Fired only
    on a reveal; a skipped frame keeps its slot."""

    current_app.transcode_queue.enqueue(
        "app.frames.replace_frame_task",
        args=(movie.id,),
        job_timeout=600,
        description=f"Replacing the played frame from '{movie.title}'",
    )


def _extra_round():
    """The user's live Extra Difficult round — {token, stage} — or
    None. Server-side state is what makes the stages honest: the
    posted stage never decides the points, and the image route never
    serves more frame than the round has earned."""

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
    """Persist the live round's token and stage for the current user."""

    current_app.redis.set(
        EXTRA_ROUND_KEY.format(user_id=int(current_user.id)),
        json.dumps({"token": token, "stage": stage}),
        ex=DEALT_TTL,
    )


def _clear_extra_round():
    """Drop the current user's live round — it ended or was skipped."""

    current_app.redis.delete(EXTRA_ROUND_KEY.format(user_id=int(current_user.id)))


# Pixels at least this bright (0-255 luma) count as picture when
# finding a frame's active area — high enough to ride over JPEG noise
# in letterbox bars, low enough that a dim scene still counts

ACTIVE_LUMA = 24


def _active_picture_box(image):
    """The bounding box of the frame's actual picture: everything
    brighter than near-black. Letterboxed and pillarboxed transfers
    bake their bars into the frame, and a zoom window that lands on a
    bar is a wall of black (Glenn's report, Aug 27 2026) — so the
    Extra Difficult crops confine themselves to this box. None when
    no pixel clears the bar (the caller falls back to the full
    frame)."""

    mask = image.convert("L").point(lambda v: 255 if v > ACTIVE_LUMA else 0)
    return mask.getbbox()


def _crop_box(token, stage, width, height, active=None):
    """The stage's crop rectangle: sized by the stage's zoom, centred
    at a point hashed from the token — anywhere in the frame's active
    picture area (the whole frame when no `active` box is given) —
    and clamped to that area's edges. The clamping is what keeps the
    stages nested wherever the centre lands, so zooming out always
    reveals more of the same spot. (An early version instead pinned
    the centre where the widest stage fit unclamped, which confined
    every crop to the middle fifth of the frame — Glenn noticed,
    Aug 27 2026.)"""

    bound_left, bound_top, bound_right, bound_bottom = active or (0, 0, width, height)
    span_w = bound_right - bound_left
    span_h = bound_bottom - bound_top
    fraction = EXTRA_ZOOM[stage]
    digest = hashlib.sha256(token.encode()).digest()
    centre_x = digest[0] / 255
    centre_y = digest[1] / 255
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


def _cropped_frame_response(token, stage):
    """The Extra Difficult crop: the stage's window onto the pooled
    frame, cut server-side — the full frame never reaches the page
    before stage three. None when the image won't decode, so the
    route can fall back to the full frame rather than 500 on a bad
    pool entry."""

    from PIL import Image

    try:
        with Image.open(frame_path(token)) as image:
            box = _crop_box(token, stage, *image.size, _active_picture_box(image))
            crop = image.crop(box).convert("RGB")
            buffer = io.BytesIO()
            crop.save(buffer, format="JPEG", quality=90)
    except (OSError, ValueError):
        return None
    buffer.seek(0)
    # Uncached: the same token serves different pixels as the round
    # advances, and crops are cheap to cut again
    return send_file(buffer, mimetype="image/jpeg", max_age=0)


def _render_extra_round(token, stage, form, missed=None):
    """The Extra Difficult guessing page at one stage — reached fresh,
    by zooming out, or by a mid-round miss (which shows what it
    wasn't)."""

    score = UserFrameScore.query.filter_by(
        user_id=int(current_user.id), difficulty="extra"
    ).first()
    return render_template(
        "game.html",
        title="Name that Frame",
        difficulty="extra",
        difficulties=DIFFICULTIES,
        result=None,
        token=token,
        options=None,
        stage=stage,
        stage_points=EXTRA_STAGES + 1 - stage,
        missed_guess=missed,
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
    """Grade one Extra Difficult action: Zoom Out or a mid-round miss
    advances the stage; a hit banks the stage's points; a stage-three
    miss — or giving up, once the round is past its first zoom-out —
    ends the round and the streak (#202; the surrender rule is
    Glenn's ask, Aug 27 2026: a started round is won or lost)."""

    round_ = _extra_round()
    if round_ is None or round_.get("token") != token:
        # The posted round isn't the live one — a stale tab, or the
        # round was skipped elsewhere. Deal or resume instead of
        # grading against the wrong stage.
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
        # A mid-round miss zooms out instead of ending the round —
        # the wrong guess buys the same look a Zoom Out would
        stage += 1
        _save_extra_round(token, stage)
        return _render_extra_round(token, stage, form, missed=guessed or None)

    _clear_extra_round()
    points_won = EXTRA_STAGES + 1 - stage if correct else 0
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
        title="Name that Frame",
        difficulty="extra",
        difficulties=DIFFICULTIES,
        result={
            "correct": correct,
            "guess": guessed or None,
            "movie_id": movie.id,
            "answer": _display_title(movie),
            "new_best": new_best,
            "points_won": points_won,
        },
        answer_movie=movie,
        token=token,
        options=None,
        stage=None,
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
    """One round per page: a pooled frame and a guess form, then the
    reveal. The token names the round; the answer only ever lives
    server-side in the pool hash."""

    form = GuessFrameForm()

    if request.method == "POST" and form.validate_on_submit():
        difficulty = (
            form.difficulty.data if form.difficulty.data in DIFFICULTIES else "easy"
        )
        token = form.token.data or ""
        entry = pool_entries().get(token)
        movie = db.session.get(Movie, entry["movie_id"]) if entry else None
        if movie is None:
            # The round's frame rotated out of the pool mid-game —
            # deal a fresh one rather than erroring
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
            title="Name that Frame",
            difficulty=difficulty,
            difficulties=DIFFICULTIES,
            result={
                "correct": correct,
                "guess": guess_shown,
                "movie_id": movie.id,
                "answer": _display_title(movie),
                "new_best": new_best,
            },
            # The reveal shows the answer as a standard poster tile —
            # popover card plus the ladder and watchlist toggle, so a
            # film worth chasing can be rated or banked on the spot
            # (Glenn's ask, Aug 20 2026)
            answer_movie=movie,
            token=token,
            options=None,
            stage=None,
            rated_only=_rated_only(difficulty),
            streak=score.current_streak,
            best=score.best_streak,
            points=(score.points or 0),
            seen=(score.rounds_seen or 0),
            won=(score.rounds_won or 0),
            form=form,
            pool_size=len(pool_entries()),
        )

    difficulty = request.args.get("difficulty", "easy")
    if difficulty not in DIFFICULTIES:
        difficulty = "easy"

    # The rated-films switch: the checkbox always submits a hidden
    # rated=0 alongside a checked rated=1, so an absent "1" is a
    # deliberate un-tick, not a bare visit
    if difficulty in RATED_FILTER_DIFFICULTIES and "rated" in request.args:
        _set_rated_only(difficulty, "1" in request.args.getlist("rated"))
    rated_only = _rated_only(difficulty)

    tokens = _round_tokens(difficulty, rated_only)
    stage = None
    dealt = False
    if difficulty == "extra":
        # A visit resumes the live round at its stage rather than
        # dealing — a refresh mustn't be a free zoom reset — so Skip
        # abandons it explicitly. But only an untouched round skips:
        # past the first zoom-out the round is won, lost, or given up
        # (Glenn's rule, Aug 27 2026), so a hand-typed ?skip=1 can't
        # dodge the loss either
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
        # Every dealt frame counts as seen (Glenn's win-rate rule,
        # Aug 27 2026): a skipped or refreshed-away round is a frame
        # the player looked at and didn't name
        score = _score_row(difficulty)
        score.rounds_seen = (score.rounds_seen or 0) + 1
        db.session.commit()
    else:
        score = UserFrameScore.query.filter_by(
            user_id=int(current_user.id), difficulty=difficulty
        ).first()

    return render_template(
        "game.html",
        title="Name that Frame",
        difficulty=difficulty,
        difficulties=DIFFICULTIES,
        result=None,
        token=token,
        options=options,
        stage=stage,
        stage_points=(EXTRA_STAGES + 1 - stage) if stage else None,
        missed_guess=None,
        rated_only=rated_only,
        streak=score.current_streak if score else 0,
        best=score.best_streak if score else 0,
        points=(score.points or 0) if score else 0,
        seen=(score.rounds_seen or 0) if score else 0,
        won=(score.rounds_won or 0) if score else 0,
        form=form,
        pool_size=len(pool_entries()),
    )


@bp.route("/game/frame/<token>")
@login_required
def game_frame(token):
    """One pooled frame, by its opaque token. Auth-gated — library
    frames never ride the public static path — and only tokens the
    pool actually holds resolve. While the token is the user's live
    Extra Difficult round, the server-side stage decides how much of
    the frame serves (#202) — the ?stage in the page's URL is only a
    cache-buster, so asking for a later stage yields nothing extra."""

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
