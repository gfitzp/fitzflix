"""Name that Frame (GitHub #52): the guessing game itself.

Rounds draw from the pre-extracted pool (app/frames.py) — the page
never touches ffmpeg. Three difficulties, per Glenn's issue: Easy
serves only films the current user has rated, with four choices;
Hard (slug "difficult") serves the whole pooled library with eight;
Difficult (slug "siracusa", renamed per #203)
serves the whole library and takes free text, fuzzy-matched against
the film's titles. Frames are served through an authenticated route
keyed by the pool's opaque tokens, so neither the image URL nor the
page markup leaks the answer before a guess lands."""

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
    send_from_directory,
    url_for,
)
from flask_login import current_user, login_required
from unidecode import unidecode

from app import db
from app.frames import DEALT_TTL, POOL_KEY, dealt_key, pool_entries
from app.main import bp
from app.main.forms import GuessFrameForm
from app.models import Movie, UserFrameScore, UserMovieReview

# Difficulty → number of multiple-choice options (None = free text)

DIFFICULTIES = {"easy": 4, "difficult": 8, "siracusa": None}

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


def _display_year(year, tmdb_title, tmdb_release_date):
    """The year the site displays for a film — TMDb's when it rules
    the title, the local one otherwise."""

    return tmdb_release_date.year if tmdb_title and tmdb_release_date else year


def _build_options(answer_id, difficulty):
    """The round's shuffled multiple-choice list: the answer plus
    random distractors from its own era — the tight year window
    first, the widened one when the library runs thin there (±5→±10
    on Easy, ±2→±5 on Hard; Glenn's rule, Aug 20 2026). Easy
    prefers the user's rated films within each window, but the era
    always outranks ratedness — an out-of-era option is the
    deduction giveaway this exists to close. Anything-goes is the
    last resort so a round can always fill its slots."""

    count = DIFFICULTIES[difficulty]
    answer = db.session.get(Movie, answer_id)
    answer_year = _display_year(
        answer.year, answer.tmdb_title, answer.tmdb_release_date
    )
    years = {
        movie_id: _display_year(year, tmdb_title, tmdb_release_date)
        for movie_id, year, tmdb_title, tmdb_release_date in db.session.query(
            Movie.id, Movie.year, Movie.tmdb_title, Movie.tmdb_release_date
        ).filter(Movie.id != answer_id)
    }
    rated = _rated_movie_ids() - {answer_id} if difficulty == "easy" else set()

    def in_window(span):
        return [
            movie_id
            for movie_id, year in years.items()
            if year is not None
            and answer_year is not None
            and abs(year - answer_year) <= span
        ]

    # Easy's universe stays the rated films, widening its window per
    # Glenn's fallback, before padding from in-era unrated films;
    # Difficult widens over the whole library the same way

    tight, wide = YEAR_WINDOWS[difficulty]
    tiers = []
    if rated:
        tiers += [
            [m for m in in_window(tight) if m in rated],
            [m for m in in_window(wide) if m in rated],
        ]
    tiers += [in_window(tight), in_window(wide), list(years)]

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

        score = _score_row(difficulty)
        score.current_streak = score.current_streak + 1 if correct else 0
        new_best = correct and score.current_streak > score.best_streak
        if new_best:
            score.best_streak = score.current_streak
            score.date_best = datetime.now()
        db.session.commit()

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
            streak=score.current_streak,
            best=score.best_streak,
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
        streak=score.current_streak if score else 0,
        best=score.best_streak if score else 0,
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
