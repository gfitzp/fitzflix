"""Name that Frame (#21, GitHub #52): the guessing game itself.

Rounds draw from the pre-extracted pool (app/frames.py) — the page
never touches ffmpeg. Three difficulties, per Glenn's issue: Easy
serves only films the current user has rated, with four choices;
Difficult serves the whole pooled library with eight; "Siracusa"
serves the whole library and takes free text, fuzzy-matched against
the film's titles. Frames are served through an authenticated route
keyed by the pool's opaque tokens, so neither the image URL nor the
page markup leaks the answer before a guess lands."""

import random
import re

from difflib import SequenceMatcher

from flask import (
    abort,
    current_app,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from flask_login import current_user, login_required
from unidecode import unidecode

from app import db
from app.frames import POOL_KEY, pool_entries
from app.main import bp
from app.main.forms import GuessFrameForm
from app.models import Movie, UserMovieReview

# Difficulty → number of multiple-choice options (None = free text)

DIFFICULTIES = {"easy": 4, "difficult": 8, "siracusa": None}

# How close a Siracusa guess must come to a real title, after
# normalization — loose enough for a typo, tight enough that a random
# film name doesn't score

FUZZY_THRESHOLD = 0.75

TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{8,64}")


def _display_title(movie):
    """The site-wide display grammar: TMDb title and year when known."""

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
    titles (local or TMDb, with or without the year)."""

    normalized = _normalize(guess)
    if not normalized:
        return False
    candidates = {movie.title, movie.tmdb_title, _display_title(movie)}
    for candidate in filter(None, candidates):
        if (
            SequenceMatcher(None, normalized, _normalize(candidate)).ratio()
            >= FUZZY_THRESHOLD
        ):
            return True
    return False


def _rated_movie_ids():
    """Movies in the current user's diary — Easy mode's world."""

    return {
        movie_id
        for (movie_id,) in db.session.query(UserMovieReview.movie_id)
        .filter(UserMovieReview.user_id == int(current_user.id))
        .filter(UserMovieReview.movie_id.isnot(None))
    }


def _build_options(answer_id, difficulty):
    """The round's shuffled multiple-choice list: the answer plus
    random distractors — drawn from the user's rated films on Easy
    (padded from the whole library when their diary is small), from
    everything on Difficult."""

    count = DIFFICULTIES[difficulty]
    all_ids = [
        movie_id
        for (movie_id,) in db.session.query(Movie.id).filter(Movie.id != answer_id)
    ]
    if difficulty == "easy":
        preferred = list(_rated_movie_ids() - {answer_id})
    else:
        preferred = all_ids
    distractors = random.sample(preferred, min(count - 1, len(preferred)))
    if len(distractors) < count - 1:
        leftovers = [m for m in all_ids if m not in distractors]
        distractors += random.sample(
            leftovers, min(count - 1 - len(distractors), len(leftovers))
        )
    ids = distractors + [answer_id]
    random.shuffle(ids)
    movies = {movie.id: movie for movie in Movie.query.filter(Movie.id.in_(ids)).all()}
    return [(movie_id, _display_title(movies[movie_id])) for movie_id in ids]


def _round_tokens(difficulty):
    """The pooled tokens this difficulty may serve."""

    entries = pool_entries()
    if difficulty == "easy":
        rated = _rated_movie_ids()
        return {
            token: entry
            for token, entry in entries.items()
            if entry.get("movie_id") in rated
        }
    return entries


def _streak_key(difficulty):
    """The session key holding this difficulty's running streak."""

    return f"frame_streak_{difficulty}"


def _deal_token(tokens):
    """Pick this round's frame without repeats (Glenn's Finding Nemo
    report, Aug 20 2026): every dealt frame lands in a per-user seen
    set in Redis, and deals exclude seen frames until the difficulty's
    whole pool has been served — then the lap resets and the frames
    come around again, still never twice in a row (the last-dealt
    frame is remembered server-side, so a plain page visit can't echo
    it either). The set is shared across difficulties (a frame seen
    on Easy is spoiled for Difficult too) and tokens that rotate out
    of the pool age out with the keys' TTL."""

    if not tokens:
        return None
    user_id = int(current_user.id)
    seen_key = f"fitzflix:frames:seen:{user_id}"
    last_key = f"fitzflix:frames:last:{user_id}"
    seen = {member.decode() for member in current_app.redis.smembers(seen_key)}
    last = (current_app.redis.get(last_key) or b"").decode()
    remaining = [token for token in tokens if token not in seen and token != last]
    if not remaining:
        current_app.redis.srem(seen_key, *tokens)
        remaining = [token for token in tokens if token != last] or list(tokens)
    token = random.choice(remaining)
    current_app.redis.sadd(seen_key, token)
    current_app.redis.expire(seen_key, 60 * 24 * 3600)
    current_app.redis.set(last_key, token, ex=60 * 24 * 3600)
    return token


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

        if DIFFICULTIES[difficulty] is None:
            guessed = (form.guess.data or "").strip()
            correct = _fuzzy_match(guessed, movie)
            guess_shown = guessed or None
        else:
            guessed = (form.choice.data or "").strip()
            correct = guessed.isdigit() and int(guessed) == movie.id
            chosen = db.session.get(Movie, int(guessed)) if guessed.isdigit() else None
            guess_shown = _display_title(chosen) if chosen else None

        streak = session.get(_streak_key(difficulty), 0)
        streak = streak + 1 if correct else 0
        session[_streak_key(difficulty)] = streak

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
            },
            token=token,
            options=None,
            streak=streak,
            form=form,
            pool_size=len(pool_entries()),
        )

    difficulty = request.args.get("difficulty", "easy")
    if difficulty not in DIFFICULTIES:
        difficulty = "easy"

    tokens = _round_tokens(difficulty)
    token = _deal_token(tokens)
    options = None
    if token and DIFFICULTIES[difficulty] is not None:
        options = _build_options(tokens[token]["movie_id"], difficulty)

    return render_template(
        "game.html",
        title="Name that Frame",
        difficulty=difficulty,
        difficulties=DIFFICULTIES,
        result=None,
        token=token,
        options=options,
        streak=session.get(_streak_key(difficulty), 0),
        form=form,
        pool_size=len(pool_entries()),
    )


@bp.route("/game/frame/<token>")
@login_required
def game_frame(token):
    """One pooled frame, by its opaque token. Auth-gated — library
    frames never ride the public static path — and only tokens the
    pool actually holds resolve."""

    if not TOKEN_PATTERN.fullmatch(token) or not current_app.redis.hexists(
        POOL_KEY, token
    ):
        abort(404)
    return send_from_directory(
        current_app.config["FRAME_POOL_DIR"], f"{token}.jpg", max_age=3600
    )
