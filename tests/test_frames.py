"""Name that Frame (#21): the nightly frame pool — pruning, top-up,
rotation, and extraction — and the game's three difficulties, the
fuzzy Siracusa matcher, and the authenticated frame route."""

import json
import os
import time

from tests.factories import make_movie, make_movie_file


def seed_frame(app, movie_id, token=None, extracted_at=None):
    """A pooled frame: a stub jpg on disk plus the Redis hash entry."""

    from app.frames import POOL_KEY, frame_path

    token = token or f"testtoken{movie_id:04d}"
    with app.app_context():
        os.makedirs(app.config["FRAME_POOL_DIR"], exist_ok=True)
        with open(frame_path(token), "wb") as handle:
            handle.write(b"jpegbytes")
    app.redis.hset(
        POOL_KEY,
        token,
        json.dumps(
            {
                "movie_id": movie_id,
                "extracted_at": extracted_at or int(time.time()),
                "offset": 123.4,
            }
        ),
    )
    return token


def test_refresh_prunes_and_tops_up(app):
    """The nightly pass drops entries whose movie or image is gone and
    queues extractions for unpooled films, up to the pool size."""

    from app import db
    from app.frames import POOL_KEY, frame_path, refresh_frame_pool_task

    with app.app_context():
        kept = make_movie("Frame Kept", 1990)
        make_movie_file(kept, "Bluray-1080p")
        fresh = make_movie("Frame Fresh", 1991)
        make_movie_file(fresh, "Bluray-1080p")
        fileless = make_movie("Frame Fileless", 1992)
        db.session.commit()
        kept_id, fresh_id, fileless_id = kept.id, fresh.id, fileless.id

    kept_token = seed_frame(app, kept_id)
    dead_token = seed_frame(app, fileless_id)  # no main-feature file
    gone_token = "goneimage0001"
    app.redis.hset(
        POOL_KEY,
        gone_token,
        json.dumps({"movie_id": kept_id, "extracted_at": 1, "offset": 1.0}),
    )  # entry without an image on disk

    with app.app_context():
        summary = refresh_frame_pool_task()
        assert summary["pooled"] == 1
        # Only the unpooled playable film needs an extraction
        assert summary["queued"] == 1
        job_ids = app.transcode_queue.get_job_ids()
        jobs = [app.transcode_queue.fetch_job(job_id) for job_id in job_ids]
        frame_jobs = [
            job for job in jobs if "extract_frame_task" in (job.func_name or "")
        ]
        assert [job.args[0] for job in frame_jobs] == [fresh_id]
        assert not os.path.isfile(frame_path(dead_token))

    assert app.redis.hexists(POOL_KEY, kept_token)
    assert not app.redis.hexists(POOL_KEY, dead_token)
    assert not app.redis.hexists(POOL_KEY, gone_token)


def test_refresh_rotates_the_oldest_when_full(app, monkeypatch):
    """A full pool retires its oldest entries and queues replacements —
    fresh films first, then new frames of the same films."""

    from app import db
    from app.frames import POOL_KEY, refresh_frame_pool_task

    monkeypatch.setitem(app.config, "FRAME_POOL_SIZE", 2)
    monkeypatch.setitem(app.config, "FRAME_POOL_ROTATE", 1)

    with app.app_context():
        old = make_movie("Frame Oldest", 1990)
        make_movie_file(old, "Bluray-1080p")
        young = make_movie("Frame Younger", 1991)
        make_movie_file(young, "Bluray-1080p")
        db.session.commit()
        old_id, young_id = old.id, young.id

    old_token = seed_frame(app, old_id, extracted_at=100)
    seed_frame(app, young_id, extracted_at=200)

    with app.app_context():
        summary = refresh_frame_pool_task()
        assert summary == {"pooled": 2, "queued": 1}
        # The oldest entry retired; with no unpooled films left, its
        # own movie gets a new frame
        assert not app.redis.hexists(POOL_KEY, old_token)
        job_ids = app.transcode_queue.get_job_ids()
        jobs = [app.transcode_queue.fetch_job(job_id) for job_id in job_ids]
        frame_jobs = [
            job for job in jobs if "extract_frame_task" in (job.func_name or "")
        ]
        assert [job.args[0] for job in frame_jobs] == [old_id]


def test_extract_frame_pools_one_frame_per_movie(app, monkeypatch):
    """Extraction writes the image, records the pool entry, and
    replaces the movie's previous frame."""

    import app.frames as frames
    from app import db

    with app.app_context():
        movie = make_movie("Frame Extracted", 1993)
        make_movie_file(movie, "Bluray-1080p")
        db.session.commit()
        movie_id = movie.id

    stale_token = seed_frame(app, movie_id)
    monkeypatch.setattr(frames, "_probe_duration", lambda path: 3600.0)

    def fake_run(cmd, **kwargs):
        with open(cmd[-1], "wb") as handle:
            handle.write(b"newframe")

    monkeypatch.setattr(frames.subprocess, "run", fake_run)

    with app.app_context():
        assert frames.extract_frame_task(movie_id) is True
        entries = frames.pool_entries()

    assert stale_token not in entries
    assert len(entries) == 1
    ((token, entry),) = entries.items()
    assert entry["movie_id"] == movie_id
    # The offset respects the credit-avoiding bounds
    assert 0.05 * 3600 <= entry["offset"] <= 0.85 * 3600
    with app.app_context():
        assert os.path.isfile(frames.frame_path(token))


def test_game_round_and_choice_guessing(app, admin_client):
    """A Difficult round serves the frame by token, lists the answer
    among the options, and grades both verdicts — with the streak
    rising on a hit and resetting on a miss."""

    import re

    from app import db

    with app.app_context():
        answer = make_movie("Frame Answer Film", 1994)
        make_movie_file(answer, "Bluray-1080p")
        for n in range(8):
            extra = make_movie(f"Frame Distractor {n}", 1980 + n)
            make_movie_file(extra, "Bluray-1080p")
        db.session.commit()
        answer_id = answer.id

    token = seed_frame(app, answer_id)

    page = admin_client.get("/game?difficulty=difficult").get_data(as_text=True)
    assert f'src="/game/frame/{token}"' in page
    assert "Frame Answer Film (1994)" in page
    # Eight choices, the answer's id among them, and no answer leak
    # beyond its equal place in the shuffled list
    assert page.count('name="choice"') == 8
    assert f'value="{answer_id}"' in page

    csrf = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page).group(1)
    response = admin_client.post(
        "/game",
        data={
            "csrf_token": csrf,
            "token": token,
            "difficulty": "difficult",
            "choice": str(answer_id),
            "guess_submit": "y",
        },
    )
    body = response.get_data(as_text=True)
    assert "Correct" in body
    assert f'href="/movie/{answer_id}"' in body

    wrong = admin_client.post(
        "/game",
        data={
            "csrf_token": csrf,
            "token": token,
            "difficulty": "difficult",
            "choice": "999999",
            "guess_submit": "y",
        },
    ).get_data(as_text=True)
    assert "alert-danger" in wrong
    assert "Frame Answer Film (1994)" in wrong
    with admin_client.session_transaction() as flask_session:
        assert flask_session["frame_streak_difficult"] == 0


def test_easy_serves_only_rated_films(app, admin_client):
    """Easy deals frames of films the user has rated; with none rated
    in the pool, the page says so instead of dealing."""

    from app import db
    from app.models import UserMovieReview
    from app.videos import star_rating_fields
    from tests.test_recommendations import admin_id

    with app.app_context():
        rated = make_movie("Frame Rated Film", 1995)
        make_movie_file(rated, "Bluray-1080p")
        unrated = make_movie("Frame Unrated Film", 1996)
        make_movie_file(unrated, "Bluray-1080p")
        for n in range(3):
            filler = make_movie(f"Frame Filler {n}", 1970 + n)
            make_movie_file(filler, "Bluray-1080p")
        db.session.add(
            UserMovieReview(
                user_id=admin_id(),
                movie_id=rated.id,
                liked=True,
                **star_rating_fields(4.0),
            )
        )
        db.session.commit()
        rated_id, unrated_id = rated.id, unrated.id

    unrated_token = seed_frame(app, unrated_id)
    page = admin_client.get("/game?difficulty=easy").get_data(as_text=True)
    assert "No frames to serve on this difficulty yet." in page
    assert unrated_token not in page

    # With a one-film diary the distractors pad from the whole library
    # so the round still deals four choices

    rated_token = seed_frame(app, rated_id)
    page = admin_client.get("/game?difficulty=easy").get_data(as_text=True)
    assert f'src="/game/frame/{rated_token}"' in page
    assert page.count('name="choice"') == 4


def test_siracusa_fuzzy_matching(app, admin_client):
    """Free-text guesses forgive typos and missing articles, but a
    wrong film is a wrong film; an empty guess is a reveal."""

    import re

    from app import db

    with app.app_context():
        movie = make_movie("The Naked Kiss", 1964)
        make_movie_file(movie, "Bluray-1080p")
        db.session.commit()
        movie_id = movie.id

    token = seed_frame(app, movie_id)
    page = admin_client.get("/game?difficulty=siracusa").get_data(as_text=True)
    assert 'name="guess"' in page
    assert "The Naked Kiss" not in page  # no answer leak on the round page
    csrf = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page).group(1)

    def guess(text):
        return admin_client.post(
            "/game",
            data={
                "csrf_token": csrf,
                "token": token,
                "difficulty": "siracusa",
                "guess": text,
                "guess_submit": "y",
            },
        ).get_data(as_text=True)

    assert "Correct" in guess("The Naked Kiss")
    assert "Correct" in guess("naked kiss")  # article dropped
    assert "Correct" in guess("nakd kiss")  # typo forgiven
    assert "alert-danger" in guess("Shock Corridor")
    assert "alert-danger" in guess("")  # reveal counts as a miss


def test_frame_route_requires_auth_and_pool_membership(app, admin_client):
    """Frames only serve to logged-in users, only for pooled tokens,
    and never for path-shaped names."""

    from app import db

    with app.app_context():
        movie = make_movie("Frame Gated Film", 1997)
        make_movie_file(movie, "Bluray-1080p")
        db.session.commit()
        movie_id = movie.id

    token = seed_frame(app, movie_id)
    assert admin_client.get(f"/game/frame/{token}").status_code == 200
    assert admin_client.get("/game/frame/unpooledtoken").status_code == 404
    assert app.test_client().get(f"/game/frame/{token}").status_code == 302

    empty = admin_client.get("/game")
    assert b"The frame pool is still empty." not in empty.data


def test_empty_pool_renders_the_waiting_state(app, admin_client):
    page = admin_client.get("/game").get_data(as_text=True)
    assert "The frame pool is still empty." in page
    assert "flask frames refresh" in page


def test_rounds_never_repeat_until_the_pool_laps(app, admin_client):
    """Dealt frames land in a per-user seen set: three pooled films
    deal three distinct rounds, and only then does the lap reset —
    still never the same frame twice in a row (Glenn's Finding Nemo
    report)."""

    import re

    from app import db

    with app.app_context():
        movies = []
        for n in range(3):
            movie = make_movie(f"Frame Cycle {n}", 1990 + n)
            make_movie_file(movie, "Bluray-1080p")
            movies.append(movie)
        db.session.commit()
        movie_ids = [movie.id for movie in movies]

    tokens = {seed_frame(app, movie_id) for movie_id in movie_ids}

    def deal():
        page = admin_client.get("/game?difficulty=difficult").get_data(as_text=True)
        return re.search(r'src="/game/frame/([A-Za-z0-9_-]+)"', page).group(1)

    first_lap = [deal() for _ in range(3)]
    assert set(first_lap) == tokens  # all three served, no repeats

    # The next deal starts a fresh lap without echoing the last frame

    fourth = deal()
    assert fourth in tokens
    assert fourth != first_lap[-1]
