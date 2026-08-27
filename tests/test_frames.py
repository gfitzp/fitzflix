"""Name That Frame: the nightly frame pool — pruning, top-up,
rotation, and extraction — and the game's four difficulties, the
fuzzy Siracusa matcher, the Extra Difficult zoom-out rounds, and the
authenticated frame route."""

import json
import os
import time

from tests.factories import make_movie as _make_movie, make_movie_file

_tmdb_seq = iter(range(700000, 800000))


def make_movie(title, year, **kwargs):
    """Every frames-test film carries a TMDB id by default — pool
    candidacy and the option list require one since #205; pass
    tmdb_id=None explicitly to model a home movie."""

    kwargs.setdefault("tmdb_id", next(_tmdb_seq))
    return _make_movie(title, year, **kwargs)


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
        # A home movie (#205): has a playable file but no TMDB entry, so
        # it must neither stay pooled nor be queued for extraction
        home = make_movie("Frame Home Movie", 1993, tmdb_id=None)
        make_movie_file(home, "Bluray-1080p")
        db.session.commit()
        kept_id, fresh_id, fileless_id = kept.id, fresh.id, fileless.id
        home_id = home.id

    kept_token = seed_frame(app, kept_id)
    dead_token = seed_frame(app, fileless_id)  # no main-feature file
    home_token = seed_frame(app, home_id)  # no TMDB id (#205)
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
    assert not app.redis.hexists(POOL_KEY, home_token)


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

    page = admin_client.get("/game?difficulty=difficult&unrated=1").get_data(
        as_text=True
    )
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

    # Standings live in the DB now: the miss reset the run, the best
    # from the earlier hit survives

    from app.models import UserFrameScore

    with app.app_context():
        score = UserFrameScore.query.filter_by(difficulty="difficult").one()
        assert score.current_streak == 0
        assert score.best_streak == 1


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
    page = admin_client.get("/game?difficulty=siracusa&unrated=1").get_data(
        as_text=True
    )
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
        page = admin_client.get("/game?difficulty=difficult&unrated=1").get_data(
            as_text=True
        )
        return re.search(r'src="/game/frame/([A-Za-z0-9_-]+)"', page).group(1)

    first_lap = [deal() for _ in range(3)]
    assert set(first_lap) == tokens  # all three served, no repeats

    # The next deal starts a fresh lap without echoing the last frame

    fourth = deal()
    assert fourth in tokens
    assert fourth != first_lap[-1]


def test_high_scores_persist_per_difficulty(app, admin_client):
    """Two hits set a best of 2; a miss resets the run but never the
    best, and each difficulty keeps its own standings row."""

    import re

    from app import db
    from app.models import UserFrameScore

    with app.app_context():
        answer = make_movie("Frame Score Film", 1998)
        make_movie_file(answer, "Bluray-1080p")
        for n in range(8):
            extra = make_movie(f"Frame Score Distractor {n}", 1960 + n)
            make_movie_file(extra, "Bluray-1080p")
        db.session.commit()
        answer_id = answer.id

    token = seed_frame(app, answer_id)
    page = admin_client.get("/game?difficulty=difficult&unrated=1").get_data(
        as_text=True
    )
    csrf = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page).group(1)

    def guess(difficulty, choice):
        return admin_client.post(
            "/game",
            data={
                "csrf_token": csrf,
                "token": token,
                "difficulty": difficulty,
                "choice": choice,
                "guess_submit": "y",
            },
        ).get_data(as_text=True)

    guess("difficult", str(answer_id))
    body = guess("difficult", str(answer_id))
    assert "That&rsquo;s 2 in a row &mdash; a new personal best." in body
    guess("difficult", "999999")

    # A hit on another difficulty starts its own row

    guess("siracusa", "")  # a miss — but creates the row
    with app.app_context():
        difficult = UserFrameScore.query.filter_by(difficulty="difficult").one()
        assert (difficult.current_streak, difficult.best_streak) == (0, 2)
        assert difficult.date_best is not None
        siracusa = UserFrameScore.query.filter_by(difficulty="siracusa").one()
        assert (siracusa.current_streak, siracusa.best_streak) == (0, 0)

    # The round page shows the standing best even after the reset

    page = admin_client.get("/game?difficulty=difficult&unrated=1").get_data(
        as_text=True
    )
    assert "Best: 2" in page


def test_refresh_guarantees_the_rated_floor(app, monkeypatch):
    """Each reviewer gets at least FRAME_POOL_MIN_RATED of their rated
    films queued into the pool before the general fill."""

    from app import db
    from app.models import UserMovieReview
    from app.frames import refresh_frame_pool_task
    from app.videos import star_rating_fields
    from tests.test_recommendations import admin_id

    monkeypatch.setitem(app.config, "FRAME_POOL_SIZE", 3)
    monkeypatch.setitem(app.config, "FRAME_POOL_ROTATE", 1)
    monkeypatch.setitem(app.config, "FRAME_POOL_MIN_RATED", 2)

    with app.app_context():
        rated_ids = []
        for n in range(2):
            movie = make_movie(f"Frame Floor Rated {n}", 1950 + n)
            make_movie_file(movie, "Bluray-1080p")
            db.session.add(
                UserMovieReview(
                    user_id=admin_id(),
                    movie_id=movie.id,
                    liked=True,
                    **star_rating_fields(4.0),
                )
            )
            rated_ids.append(movie.id)
        for n in range(3):
            unrated = make_movie(f"Frame Floor Unrated {n}", 1970 + n)
            make_movie_file(unrated, "Bluray-1080p")
        db.session.commit()

        summary = refresh_frame_pool_task()
        assert summary["queued"] == 3
        job_ids = app.transcode_queue.get_job_ids()
        jobs = [app.transcode_queue.fetch_job(job_id) for job_id in job_ids]
        queued = [
            job.args[0] for job in jobs if "extract_frame_task" in (job.func_name or "")
        ]
        # Both rated films made the cut despite five candidates for
        # three slots
        assert set(rated_ids) <= set(queued)


def test_reveal_offers_the_answer_as_an_action_tile(app, admin_client):
    """After a guess, the answer renders as a standard poster tile:
    popover-armed anchor, hydration container, ladder, and the
    watchlist toggle — so the film can be rated or banked in place."""

    import re

    from app import db

    with app.app_context():
        answer = make_movie("Frame Reveal Film", 1999)
        make_movie_file(answer, "Bluray-1080p")
        for n in range(8):
            extra = make_movie(f"Frame Reveal Distractor {n}", 1960 + n)
            make_movie_file(extra, "Bluray-1080p")
        db.session.commit()
        answer_id = answer.id

    token = seed_frame(app, answer_id)
    page = admin_client.get("/game?difficulty=difficult&unrated=1").get_data(
        as_text=True
    )
    csrf = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page).group(1)
    body = admin_client.post(
        "/game",
        data={
            "csrf_token": csrf,
            "token": token,
            "difficulty": "difficult",
            "choice": str(answer_id),
            "guess_submit": "y",
        },
    ).get_data(as_text=True)

    assert f'data-card-url="/movie_card?movie_id={answer_id}"' in body
    assert f'data-state-movie="{answer_id}"' in body
    assert 'name="add_watchlist_submit"' in body
    assert 'data-ladder-live="1"' in body
    assert f'href="/movie/{answer_id}"' in body


def test_options_stay_within_the_answers_era(app, admin_client, monkeypatch):
    """Distractors come from the answer's era: Difficult tries ±2 then
    ±5 (an out-of-era title never appears while in-era ones remain),
    and Easy's rated universe widens ±5 to ±10 the same way."""

    import app.main.game as game

    from app import db
    from app.models import UserMovieReview
    from app.videos import star_rating_fields
    from tests.test_recommendations import admin_id

    # Difficult at 3 options so two distractors decide the tiers

    monkeypatch.setitem(game.DIFFICULTIES, "difficult", 3)

    with app.app_context():
        answer = make_movie("Era Answer", 1960)
        make_movie_file(answer, "Bluray-1080p")
        make_movie("Era Near", 1961)  # inside ±2
        make_movie("Era Mid", 1964)  # inside ±5 only
        make_movie("Era Far", 1990)  # out of every window
        db.session.commit()
        answer_id = answer.id

    seed_frame(app, answer_id)
    page = admin_client.get("/game?difficulty=difficult&unrated=1").get_data(
        as_text=True
    )
    assert "Era Near (1961)" in page
    assert "Era Mid (1964)" in page  # ±2 alone can't fill two slots
    assert "Era Far (1990)" not in page

    # Easy: the rated universe widens before unrated films pad in

    with app.app_context():
        rated_near = make_movie("Era Rated Near", 1963)  # rated, ±5
        rated_wide = make_movie("Era Rated Wide", 1968)  # rated, ±10 only
        rated_far = make_movie("Era Rated Far", 1990)  # rated, out of era
        for movie in (rated_near, rated_wide, rated_far, answer):
            db.session.add(
                UserMovieReview(
                    user_id=admin_id(),
                    movie_id=movie.id,
                    liked=True,
                    **star_rating_fields(4.0),
                )
            )
        db.session.commit()

    page = admin_client.get("/game?difficulty=easy").get_data(as_text=True)
    assert "Era Rated Near (1963)" in page
    assert "Era Rated Wide (1968)" in page
    assert "Era Rated Far (1990)" not in page
    # The fourth slot pads from in-era unrated films, not the far one
    assert "Era Far (1990)" not in page


def test_a_lapped_difficulty_replays_least_recently_seen_first(app, admin_client):
    """Once a difficulty has dealt every frame it holds, the pool comes
    back round least-recently-seen first rather than at random, so the
    whole pool cycles before anything shows twice (#200)."""

    import re

    from app import db

    with app.app_context():
        movie_ids = []
        for n in range(6):
            movie = make_movie(f"Frame Lap {n}", 1990 + n)
            make_movie_file(movie, "Bluray-1080p")
            movie_ids.append(movie.id)
        db.session.commit()
        movie_ids = [movie_id for movie_id in movie_ids]

    for movie_id in movie_ids:
        seed_frame(app, movie_id)

    def deal():
        page = admin_client.get("/game?difficulty=difficult&unrated=1").get_data(
            as_text=True
        )
        return re.search(r'src="/game/frame/([A-Za-z0-9_-]+)"', page).group(1)

    first_lap = [deal() for _ in range(6)]
    assert len(set(first_lap)) == 6

    # The second lap replays the first one in the same order: the frame
    # seen longest ago always comes back first

    assert [deal() for _ in range(6)] == first_lap


def test_rotation_retires_played_frames_before_merely_old_ones(
    app, admin_client, monkeypatch
):
    """A frame the game has already dealt is spent: the nightly pass
    retires it ahead of an older frame nobody has seen, and forgets
    its token once it has left the pool (#200)."""

    import re

    from app import db
    from app.frames import POOL_KEY, dealt_key, refresh_frame_pool_task

    with app.app_context():
        movie_ids = []
        for n in range(3):
            movie = make_movie(f"Frame Spent {n}", 1990 + n)
            make_movie_file(movie, "Bluray-1080p")
            movie_ids.append(movie.id)
        db.session.commit()
        movie_ids = [movie_id for movie_id in movie_ids]

    # Ages run oldest-first; only the youngest frame ever gets played

    tokens = [
        seed_frame(app, movie_id, extracted_at=100 * (n + 1))
        for n, movie_id in enumerate(movie_ids)
    ]
    oldest, played = tokens[0], tokens[-1]

    with app.app_context():
        monkeypatch.setitem(app.config, "FRAME_POOL_SIZE", 3)
        monkeypatch.setitem(app.config, "FRAME_POOL_ROTATE", 1)
        # Deal until the youngest frame comes up, so it lands in the
        # user's dealt record while the others stay unseen
        keys = set()
        while played not in keys:
            page = admin_client.get("/game?difficulty=difficult&unrated=1").get_data(
                as_text=True
            )
            keys.add(re.search(r'src="/game/frame/([A-Za-z0-9_-]+)"', page).group(1))
        app.redis.delete(dealt_key(1))
        app.redis.zadd(dealt_key(1), {played: 1})

        refresh_frame_pool_task()

        # Spent beats old: the played frame goes, the oldest stays
        assert not app.redis.hexists(POOL_KEY, played)
        assert app.redis.hexists(POOL_KEY, oldest)
        # …and its token no longer counts as spent
        assert app.redis.zscore(dealt_key(1), played) is None


def test_rotation_cannot_undercut_the_rated_floor(app, monkeypatch):
    """Rotation runs before the reviewer floors are measured, so a
    retired rated frame is made good the same night — the floor used
    to be counted against a pool rotation then ate into (#200)."""

    from app import db
    from app.models import UserMovieReview
    from app.frames import refresh_frame_pool_task
    from app.videos import star_rating_fields
    from tests.test_recommendations import admin_id

    monkeypatch.setitem(app.config, "FRAME_POOL_SIZE", 3)
    monkeypatch.setitem(app.config, "FRAME_POOL_ROTATE", 1)
    monkeypatch.setitem(app.config, "FRAME_POOL_MIN_RATED", 2)

    with app.app_context():
        rated_ids = []
        for n in range(2):
            movie = make_movie(f"Frame Floor Keeps {n}", 1950 + n)
            make_movie_file(movie, "Bluray-1080p")
            db.session.add(
                UserMovieReview(
                    user_id=admin_id(),
                    movie_id=movie.id,
                    liked=True,
                    **star_rating_fields(4.0),
                )
            )
            rated_ids.append(movie.id)
        unrated = make_movie("Frame Floor Spare", 1970)
        make_movie_file(unrated, "Bluray-1080p")
        for n in range(2):
            spare = make_movie(f"Frame Floor Unpooled {n}", 1975 + n)
            make_movie_file(spare, "Bluray-1080p")
        db.session.commit()
        unrated_id = unrated.id

    # A full pool whose oldest entry is one of the rated films
    seed_frame(app, rated_ids[0], extracted_at=100)
    seed_frame(app, rated_ids[1], extracted_at=300)
    seed_frame(app, unrated_id, extracted_at=400)

    with app.app_context():
        refresh_frame_pool_task()
        job_ids = app.transcode_queue.get_job_ids()
        jobs = [app.transcode_queue.fetch_job(job_id) for job_id in job_ids]
        queued = {
            job.args[0] for job in jobs if "extract_frame_task" in (job.func_name or "")
        }
        from app.frames import pool_entries

        pooled = {entry["movie_id"] for entry in pool_entries().values()}
        # Rotation retired the oldest rated frame, so the floor of two
        # rated films is restored by a queued extraction
        assert len(set(rated_ids) & (pooled | queued)) == 2


def test_the_pool_never_grows_past_its_configured_size(app, monkeypatch):
    """FRAME_POOL_SIZE is the ceiling and the reviewer floors are a
    composition rule inside it, not licence to grow past it — a big
    floor deficit fills the room rotation freed and no more."""

    from app import db
    from app.models import UserMovieReview
    from app.frames import pool_entries, refresh_frame_pool_task
    from app.videos import star_rating_fields
    from tests.test_recommendations import admin_id

    monkeypatch.setitem(app.config, "FRAME_POOL_SIZE", 4)
    monkeypatch.setitem(app.config, "FRAME_POOL_ROTATE", 1)
    monkeypatch.setitem(app.config, "FRAME_POOL_MIN_RATED", 3)

    with app.app_context():
        # Three rated films, none of them pooled: a floor deficit far
        # wider than the single slot rotation frees
        for n in range(3):
            movie = make_movie(f"Frame Cap Rated {n}", 1950 + n)
            make_movie_file(movie, "Bluray-1080p")
            db.session.add(
                UserMovieReview(
                    user_id=admin_id(),
                    movie_id=movie.id,
                    liked=True,
                    **star_rating_fields(4.0),
                )
            )
        pooled_ids = []
        for n in range(4):
            movie = make_movie(f"Frame Cap Pooled {n}", 1970 + n)
            make_movie_file(movie, "Bluray-1080p")
            pooled_ids.append(movie.id)
        for n in range(2):
            spare = make_movie(f"Frame Cap Spare {n}", 1980 + n)
            make_movie_file(spare, "Bluray-1080p")
        db.session.commit()
        pooled_ids = [movie_id for movie_id in pooled_ids]

    for n, movie_id in enumerate(pooled_ids):
        seed_frame(app, movie_id, extracted_at=100 * (n + 1))

    with app.app_context():
        refresh_frame_pool_task()
        jobs = [
            app.transcode_queue.fetch_job(job_id)
            for job_id in app.transcode_queue.get_job_ids()
        ]
        queued = [
            job.args[0] for job in jobs if "extract_frame_task" in (job.func_name or "")
        ]
        # One slot freed, one slot filled — the pool stays at four
        assert len(pool_entries()) + len(queued) == 4


def seed_image_frame(app, movie_id, size=(120, 80), token=None):
    """A pooled frame whose image is a real JPEG, for the crop tests."""

    from PIL import Image

    from app.frames import POOL_KEY, frame_path

    token = token or f"imagetoken{movie_id:04d}"
    with app.app_context():
        os.makedirs(app.config["FRAME_POOL_DIR"], exist_ok=True)
        Image.new("RGB", size, (40, 90, 160)).save(frame_path(token), "JPEG")
    app.redis.hset(
        POOL_KEY,
        token,
        json.dumps(
            {"movie_id": movie_id, "extracted_at": int(time.time()), "offset": 12.3}
        ),
    )
    return token


def test_options_prefer_shared_cast_then_genre(app, admin_client, monkeypatch):
    """Distractors walk Glenn's ladder (#201): an in-era film sharing
    the answer's top-billed cast fills a slot first, then an in-era
    film sharing a genre — a plain in-era film only pads after both."""

    import app.main.game as game

    from app import db
    from app.models import MovieCast, TMDBCredit, TMDBGenre

    # Three options, so two distractor slots decide the ladder

    monkeypatch.setitem(game.DIFFICULTIES, "difficult", 3)

    with app.app_context():
        answer = make_movie("Ladder Answer", 1960)
        make_movie_file(answer, "Bluray-1080p")
        castmate = make_movie("Ladder Castmate", 1961)
        genremate = make_movie("Ladder Genremate", 1961)
        make_movie("Ladder Plain", 1961)
        lead = TMDBCredit(id=911911, name="Ladder Lead")
        db.session.add(lead)
        db.session.flush()
        for movie, order in ((answer, 0), (castmate, 3)):
            db.session.add(
                MovieCast(
                    movie_id=movie.id,
                    credit_id=lead.id,
                    character="Lead",
                    billing_order=order,
                )
            )
        genre = TMDBGenre(id=987654, name="Ladder Drama")
        db.session.add(genre)
        db.session.flush()
        answer.genres.append(genre)
        genremate.genres.append(genre)
        db.session.commit()
        answer_id = answer.id

    seed_frame(app, answer_id)
    page = admin_client.get("/game?difficulty=difficult&unrated=1").get_data(
        as_text=True
    )
    assert "Ladder Castmate (1961)" in page
    assert "Ladder Genremate (1961)" in page
    assert "Ladder Plain (1961)" not in page


def test_options_fall_back_to_shared_genre_outside_the_era(
    app, admin_client, monkeypatch
):
    """With nothing in the answer's era, a same-genre film outside it
    beats a random out-of-era one — the ladder's last real rung before
    anything-goes (#201)."""

    import app.main.game as game

    from app import db
    from app.models import TMDBGenre

    # Two options: one distractor slot

    monkeypatch.setitem(game.DIFFICULTIES, "difficult", 2)

    with app.app_context():
        answer = make_movie("Ladder Era Answer", 1960)
        make_movie_file(answer, "Bluray-1080p")
        far_genre = make_movie("Ladder Far Genremate", 1990)
        make_movie("Ladder Far Plain", 1991)
        genre = TMDBGenre(id=987655, name="Ladder Noir")
        db.session.add(genre)
        db.session.flush()
        answer.genres.append(genre)
        far_genre.genres.append(genre)
        db.session.commit()
        answer_id = answer.id

    seed_frame(app, answer_id)
    page = admin_client.get("/game?difficulty=difficult&unrated=1").get_data(
        as_text=True
    )
    assert "Ladder Far Genremate (1990)" in page
    assert "Ladder Far Plain (1991)" not in page


def test_extra_difficult_zooms_out_and_scores_points(app, admin_client):
    """The Extra Difficult round (#202): opens on a stage-one crop
    worth 3 points, Zoom Out widens it to 2, a mid-round miss widens
    it again to the full frame, and a hit there banks 1 — while a
    first-look hit on the next round banks 3. The image route serves
    the server-side stage's crop, never more. The film is rated, so
    the points stay at their base values — the unrated 2x bonus has
    its own test."""

    import io
    import re

    from PIL import Image

    from app import db
    from app.models import UserFrameScore, UserMovieReview
    from app.videos import star_rating_fields
    from tests.test_recommendations import admin_id

    with app.app_context():
        movie = make_movie("Extra Round Film", 2001)
        make_movie_file(movie, "Bluray-1080p")
        db.session.add(
            UserMovieReview(
                user_id=admin_id(),
                movie_id=movie.id,
                liked=True,
                **star_rating_fields(4.0),
            )
        )
        db.session.commit()
        movie_id = movie.id

    token = seed_image_frame(app, movie_id, size=(120, 80))

    page = admin_client.get("/game?difficulty=extra&unrated=1").get_data(as_text=True)
    assert 'name="guess"' in page
    assert 'name="zoom_out"' in page
    assert "3 points" in page
    assert "Extra Round Film" not in page  # no answer leak
    csrf = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page).group(1)

    def frame_size(query=""):
        response = admin_client.get(f"/game/frame/{token}{query}")
        return Image.open(io.BytesIO(response.data)).size

    # Stage one serves a 30% crop — even when the URL asks for more

    assert frame_size("?stage=1") == (36, 24)
    assert frame_size("?stage=3") == (36, 24)

    def post(data):
        return admin_client.post(
            "/game",
            data={"csrf_token": csrf, "token": token, "difficulty": "extra", **data},
        ).get_data(as_text=True)

    # Zoom Out widens the crop and drops the round to 2 points; a
    # plain revisit resumes the stage instead of resetting it

    body = post({"zoom_out": "y"})
    assert "2 points" in body
    assert frame_size("?stage=2") == (72, 48)
    assert "2 points" in admin_client.get("/game?difficulty=extra&unrated=1").get_data(
        as_text=True
    )

    # A mid-round miss zooms out to the full frame instead of ending
    # the round, and stage three offers no further Zoom Out

    body = post({"guess": "Wrong Film", "guess_submit": "y"})
    assert "Not <em>Wrong Film</em>" in body
    assert "1 point" in body
    assert 'name="zoom_out"' not in body
    assert frame_size() == (120, 80)

    # A full-frame hit banks one point and starts the streak

    body = post({"guess": "Extra Round Film", "guess_submit": "y"})
    assert "Correct" in body
    assert "(+1 point)" in body
    with app.app_context():
        score = UserFrameScore.query.filter_by(difficulty="extra").one()
        assert (score.points, score.current_streak) == (1, 1)

    # The next round opens fresh at 3 points; a first-look hit banks
    # all three

    page = admin_client.get("/game?difficulty=extra&unrated=1").get_data(as_text=True)
    assert "3 points" in page
    body = post({"guess": "extra round film", "guess_submit": "y"})
    assert "(+3 points)" in body
    with app.app_context():
        score = UserFrameScore.query.filter_by(difficulty="extra").one()
        assert (score.points, score.current_streak) == (4, 2)

    # The standings badge carries the running total

    page = admin_client.get("/game?difficulty=extra&unrated=1").get_data(as_text=True)
    assert "Points: 4" in page


def test_extra_difficult_full_frame_miss_ends_the_round(app, admin_client):
    """Missing on the full frame ends the round: no points, the streak
    resets, and the reveal shows the answer (#202)."""

    import re

    from app import db
    from app.models import UserFrameScore

    with app.app_context():
        movie = make_movie("Extra Miss Film", 2002)
        make_movie_file(movie, "Bluray-1080p")
        db.session.commit()
        movie_id = movie.id

    token = seed_image_frame(app, movie_id)
    page = admin_client.get("/game?difficulty=extra&unrated=1").get_data(as_text=True)
    csrf = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page).group(1)

    def post(data):
        return admin_client.post(
            "/game",
            data={"csrf_token": csrf, "token": token, "difficulty": "extra", **data},
        ).get_data(as_text=True)

    post({"zoom_out": "y"})
    post({"zoom_out": "y"})
    body = post({"guess": "Still Wrong", "guess_submit": "y"})
    assert "alert-danger" in body
    assert "Extra Miss Film" in body  # the reveal
    with app.app_context():
        score = UserFrameScore.query.filter_by(difficulty="extra").one()
        assert (score.points, score.current_streak) == (0, 0)


def test_extra_skip_abandons_the_round(app, admin_client):
    """Skip clears an untouched round — but past the first zoom-out
    the round must be won, lost, or given up, so a hand-typed ?skip=1
    is ignored (Glenn's rule, Aug 27 2026)."""

    import re

    from app import db

    with app.app_context():
        movie = make_movie("Extra Skip Film", 2003)
        make_movie_file(movie, "Bluray-1080p")
        db.session.commit()
        movie_id = movie.id

    token = seed_image_frame(app, movie_id)
    page = admin_client.get("/game?difficulty=extra&unrated=1").get_data(as_text=True)
    assert "skip=1" in page
    csrf = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page).group(1)
    admin_client.post(
        "/game",
        data={
            "csrf_token": csrf,
            "token": token,
            "difficulty": "extra",
            "zoom_out": "y",
        },
    )

    assert "2 points" in admin_client.get("/game?difficulty=extra&unrated=1").get_data(
        as_text=True
    )
    # Zoomed out already: the skip parameter no longer resets the round
    assert "2 points" in admin_client.get("/game?difficulty=extra&skip=1").get_data(
        as_text=True
    )


def test_extra_give_up_ends_the_round_as_a_miss(app, admin_client):
    """After the first zoom-out, Skip gives way to I-give-up: the
    round ends with the reveal, the streak resets, no points bank,
    and the played frame's replacement queues (Glenn's ask, Aug 27
    2026)."""

    import re

    from app import db
    from app.models import UserFrameScore

    with app.app_context():
        movie = make_movie("Surrender Film", 2007)
        make_movie_file(movie, "Bluray-1080p")
        db.session.commit()
        movie_id = movie.id

    token = seed_image_frame(app, movie_id)
    page = admin_client.get("/game?difficulty=extra&unrated=1").get_data(as_text=True)
    csrf = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page).group(1)

    # An untouched round offers Skip, not surrender

    assert 'name="give_up"' not in page
    assert "skip=1" in page

    def post(data):
        return admin_client.post(
            "/game",
            data={"csrf_token": csrf, "token": token, "difficulty": "extra", **data},
        ).get_data(as_text=True)

    body = post({"zoom_out": "y"})
    assert 'name="give_up"' in body
    assert "skip=1" not in body

    # Give a streak to lose

    with app.app_context():
        score = UserFrameScore.query.filter_by(difficulty="extra").one()
        score.current_streak = 3
        db.session.commit()

    body = post({"give_up": "y"})
    assert "alert-danger" in body
    assert "Surrender Film" in body  # the reveal
    with app.app_context():
        score = UserFrameScore.query.filter_by(difficulty="extra").one()
        assert (score.current_streak, score.points, score.rounds_won) == (0, 0, 0)
    jobs = [
        app.transcode_queue.fetch_job(job_id)
        for job_id in app.transcode_queue.get_job_ids()
    ]
    assert [
        job.args[0] for job in jobs if "replace_frame_task" in (job.func_name or "")
    ] == [movie_id]

    # The round is over: the next visit deals fresh at three points

    assert "3 points" in admin_client.get("/game?difficulty=extra&unrated=1").get_data(
        as_text=True
    )


def test_crop_boxes_roam_the_whole_frame():
    """Crop windows land anywhere on the frame, not just its middle
    (Glenn's report, Aug 27 2026) — while staying in bounds and
    nested, so zooming out still reveals more of the same spot."""

    from app.main.game import _crop_box

    width, height = 1000, 600
    lefts, tops = [], []
    for n in range(200):
        token = f"roamtoken{n:04d}"
        s1 = _crop_box(token, 1, width, height)
        s2 = _crop_box(token, 2, width, height)
        assert 0 <= s1[0] and s1[2] <= width and 0 <= s1[1] and s1[3] <= height
        assert 0 <= s2[0] and s2[2] <= width and 0 <= s2[1] and s2[3] <= height
        # Stage one's window sits inside stage two's
        assert s2[0] <= s1[0] and s1[2] <= s2[2]
        assert s2[1] <= s1[1] and s1[3] <= s2[3]
        lefts.append(s1[0])
        tops.append(s1[1])
    # The centres range across the frame — some crops hug the edges
    assert min(lefts) == 0 and max(lefts) == width - int(width * 0.3)
    assert min(tops) == 0 and max(tops) == height - int(height * 0.3)


def test_rated_films_are_the_default_world(app, admin_client):
    """The library-wide difficulties deal only rated films by default
    (inverted per Glenn's ask, Aug 27 2026) — the switch now *widens*
    the deals to unrated films, and persists across plain visits."""

    from app import db
    from app.models import UserMovieReview
    from app.videos import star_rating_fields
    from tests.test_recommendations import admin_id

    with app.app_context():
        rated = make_movie("Filter Rated Film", 1990)
        make_movie_file(rated, "Bluray-1080p")
        unrated = make_movie("Filter Unrated Film", 1991)
        make_movie_file(unrated, "Bluray-1080p")
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

    rated_token = seed_frame(app, rated_id)
    unrated_token = seed_frame(app, unrated_id)

    # Bare visits deal only the rated film, visit after visit

    for _ in range(3):
        page = admin_client.get("/game?difficulty=difficult").get_data(as_text=True)
        assert 'id="include-unrated" checked' not in page
        assert f'src="/game/frame/{rated_token}' in page
        assert unrated_token not in page

    # Ticking the switch widens the deals — the unrated frame is the
    # only unseen one, so it comes straight up — and it persists

    page = admin_client.get("/game?difficulty=difficult&unrated=1").get_data(
        as_text=True
    )
    assert 'id="include-unrated" checked' in page
    assert f'src="/game/frame/{unrated_token}' in page
    page = admin_client.get("/game?difficulty=difficult").get_data(as_text=True)
    assert 'id="include-unrated" checked' in page

    # Un-ticking narrows the deals back to rated films

    page = admin_client.get("/game?difficulty=difficult&unrated=0").get_data(
        as_text=True
    )
    assert 'id="include-unrated" checked' not in page
    assert f'src="/game/frame/{rated_token}' in page


def test_rated_default_empty_state_offers_the_way_out(app, admin_client):
    """With nothing rated in the pool, the default rated-only world
    explains itself and keeps the include-unrated switch on screen."""

    from app import db

    with app.app_context():
        unrated = make_movie("Filter Only Unrated", 1992)
        make_movie_file(unrated, "Bluray-1080p")
        db.session.commit()
        unrated_id = unrated.id

    seed_frame(app, unrated_id)
    page = admin_client.get("/game?difficulty=siracusa").get_data(as_text=True)
    assert "No frames to serve on this difficulty yet." in page
    assert "Include films I haven&rsquo;t rated" in page
    assert "rate a few more" in page
    assert 'id="include-unrated" checked' not in page


def test_win_rate_counts_skips_as_frames_seen(app, admin_client):
    """Every dealt frame counts as seen — a skipped one included — and
    only correct guesses count as won, so the badge reads wins over
    deals."""

    import re

    from app import db
    from app.models import UserFrameScore

    with app.app_context():
        answer = make_movie("Winrate Film", 1993)
        make_movie_file(answer, "Bluray-1080p")
        for n in range(8):
            make_movie(f"Winrate Distractor {n}", 1985 + n)
        db.session.commit()
        answer_id = answer.id

    token = seed_frame(app, answer_id)
    page = admin_client.get("/game?difficulty=difficult&unrated=1").get_data(
        as_text=True
    )
    csrf = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page).group(1)

    def guess(choice):
        return admin_client.post(
            "/game",
            data={
                "csrf_token": csrf,
                "token": token,
                "difficulty": "difficult",
                "choice": choice,
                "guess_submit": "y",
            },
        ).get_data(as_text=True)

    body = guess(str(answer_id))
    assert "Win rate: 100%" in body  # one seen, one won

    # Dealing again (a skip, effectively) counts as another frame seen

    page = admin_client.get("/game?difficulty=difficult&unrated=1").get_data(
        as_text=True
    )
    assert "Win rate: 50%" in page

    guess("999999")
    with app.app_context():
        score = UserFrameScore.query.filter_by(difficulty="difficult").one()
        assert (score.rounds_seen, score.rounds_won) == (2, 1)


def test_extra_resume_counts_one_frame_seen(app, admin_client):
    """Resuming a live Extra round on a plain visit is not another
    frame seen — only a fresh deal (a skip included) counts."""

    from app import db
    from app.models import UserFrameScore

    with app.app_context():
        movie = make_movie("Extra Seen Film", 2004)
        make_movie_file(movie, "Bluray-1080p")
        db.session.commit()
        movie_id = movie.id

    seed_image_frame(app, movie_id)
    admin_client.get("/game?difficulty=extra&unrated=1")
    admin_client.get("/game?difficulty=extra&unrated=1")  # resumes, no new deal
    with app.app_context():
        score = UserFrameScore.query.filter_by(difficulty="extra").one()
        assert score.rounds_seen == 1

    admin_client.get("/game?difficulty=extra&skip=1")  # abandons and deals
    with app.app_context():
        score = UserFrameScore.query.filter_by(difficulty="extra").one()
        assert score.rounds_seen == 2


def test_profile_reset_wipes_frame_standings(app, admin_client):
    """The profile's reset button deletes every one of the user's
    standings rows — streaks, points, and win stats together."""

    import re

    from app import db
    from app.models import UserFrameScore
    from tests.test_recommendations import admin_id

    with app.app_context():
        for difficulty, best in (("difficult", 4), ("extra", 2)):
            db.session.add(
                UserFrameScore(
                    user_id=admin_id(),
                    difficulty=difficulty,
                    current_streak=1,
                    best_streak=best,
                    points=7,
                    rounds_seen=10,
                    rounds_won=5,
                )
            )
        db.session.commit()

    page = admin_client.get("/profile").get_data(as_text=True)
    assert 'name="reset_frames_submit"' in page
    csrf = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page).group(1)
    body = admin_client.post(
        "/profile",
        data={"csrf_token": csrf, "reset_frames_submit": "Reset game scores"},
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "have been reset" in body
    with app.app_context():
        assert UserFrameScore.query.count() == 0


def test_finished_rounds_queue_a_frame_replacement(app, admin_client):
    """A graded reveal queues the per-round top-up for the played film;
    dealing and skipping alone never do (Glenn's ask, Aug 27 2026)."""

    import re

    from app import db

    with app.app_context():
        answer = make_movie("Topup Answer Film", 1994)
        make_movie_file(answer, "Bluray-1080p")
        for n in range(8):
            make_movie(f"Topup Distractor {n}", 1985 + n)
        db.session.commit()
        answer_id = answer.id

    token = seed_frame(app, answer_id)

    def replacement_jobs():
        jobs = [
            app.transcode_queue.fetch_job(job_id)
            for job_id in app.transcode_queue.get_job_ids()
        ]
        return [
            job.args[0] for job in jobs if "replace_frame_task" in (job.func_name or "")
        ]

    # Dealing (and re-dealing, a skip) queues nothing

    page = admin_client.get("/game?difficulty=difficult&unrated=1").get_data(
        as_text=True
    )
    admin_client.get("/game?difficulty=difficult&unrated=1")
    assert replacement_jobs() == []

    csrf = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page).group(1)
    admin_client.post(
        "/game",
        data={
            "csrf_token": csrf,
            "token": token,
            "difficulty": "difficult",
            "choice": str(answer_id),
            "guess_submit": "y",
        },
    )
    assert replacement_jobs() == [answer_id]


def test_extra_round_queues_replacement_only_at_the_end(app, admin_client):
    """Zoom-outs and mid-round misses keep the round alive — the
    top-up fires once, when the round actually ends."""

    import re

    from app import db

    with app.app_context():
        movie = make_movie("Topup Extra Film", 2005)
        make_movie_file(movie, "Bluray-1080p")
        db.session.commit()
        movie_id = movie.id

    token = seed_image_frame(app, movie_id)
    page = admin_client.get("/game?difficulty=extra&unrated=1").get_data(as_text=True)
    csrf = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page).group(1)

    def post(data):
        return admin_client.post(
            "/game",
            data={"csrf_token": csrf, "token": token, "difficulty": "extra", **data},
        )

    def replacement_jobs():
        jobs = [
            app.transcode_queue.fetch_job(job_id)
            for job_id in app.transcode_queue.get_job_ids()
        ]
        return [
            job.args[0] for job in jobs if "replace_frame_task" in (job.func_name or "")
        ]

    post({"zoom_out": "y"})
    post({"guess": "Wrong Film", "guess_submit": "y"})  # mid-round miss
    assert replacement_jobs() == []

    post({"guess": "Topup Extra Film", "guess_submit": "y"})
    assert replacement_jobs() == [movie_id]


def test_replace_frame_task_swaps_in_an_unpooled_film(app, monkeypatch):
    """The top-up extracts a film the pool doesn't hold and retires the
    played film's frame on success; with the whole library pooled it
    re-extracts the played film itself instead."""

    import app.frames as frames

    from app import db
    from app.frames import POOL_KEY, replace_frame_task

    with app.app_context():
        played = make_movie("Swap Played Film", 1990)
        make_movie_file(played, "Bluray-1080p")
        fresh = make_movie("Swap Fresh Film", 1991)
        make_movie_file(fresh, "Bluray-1080p")
        db.session.commit()
        played_id, fresh_id = played.id, fresh.id

    played_token = seed_frame(app, played_id)
    extracted = []
    monkeypatch.setattr(
        frames,
        "extract_frame_task",
        lambda movie_id: extracted.append(movie_id) or True,
    )

    with app.app_context():
        assert replace_frame_task(played_id) == fresh_id
    assert extracted == [fresh_id]
    assert not app.redis.hexists(POOL_KEY, played_token)

    # Whole library pooled: the played film comes back on a new frame,
    # and its current frame is left for the real extraction to replace

    played_token = seed_frame(app, played_id)
    seed_frame(app, fresh_id)
    with app.app_context():
        assert replace_frame_task(played_id) == played_id
    assert extracted == [fresh_id, played_id]
    assert app.redis.hexists(POOL_KEY, played_token)


def test_frame_pool_tasks_stay_off_the_running_banners(app):
    """Frame extractions and per-round replacements never surface as
    top-of-page task alerts — mid-game they disrupt the round and name
    films about to become answers — but the queue page still lists
    them."""

    from rq.registry import StartedJobRegistry

    from app.models import User
    from tests.conftest import ADMIN_EMAIL

    with app.app_context():
        replace_job = app.transcode_queue.enqueue(
            "app.frames.replace_frame_task",
            args=(1,),
            description="Replacing the played frame from 'Secret Answer'",
        )
        extract_job = app.transcode_queue.enqueue(
            "app.frames.extract_frame_task",
            args=(1,),
            description="Extracting a frame from 'Secret Answer'",
        )
        visible_job = app.transcode_queue.enqueue(
            "app.videos.transcode_task",
            args=(1,),
            description="Transcoding a visible file",
        )
        # rq 2's StartedJobRegistry.add raises NotImplementedError —
        # stamp the registry's sorted set directly
        registry = StartedJobRegistry("fitzflix-transcode", connection=app.redis)
        for job in (replace_job, extract_job, visible_job):
            app.redis.zadd(registry.key, {job.id: int(time.time()) + 300})

        details = User.query.filter_by(email=ADMIN_EMAIL).one().get_queue_details()
        running = " ".join(task["description"] for task in details["running"])
        assert "Transcoding a visible file" in running
        assert "Secret Answer" not in running
        listed = " ".join(task["description"] for task in details["all"])
        assert "Replacing the played frame" in listed
        assert "Extracting a frame" in listed


def test_difficulty_picker_is_a_radio_group(app, admin_client):
    """The difficulty switcher renders as one radio button group with
    the current level checked."""

    import re

    page = admin_client.get("/game?difficulty=difficult&unrated=1").get_data(
        as_text=True
    )
    assert 'aria-label="Difficulty"' in page
    assert 'class="btn-check"' in page
    assert re.search(r'id="difficulty-difficult"[^>]*\schecked', page)
    assert not re.search(r'id="difficulty-easy"[^>]*\schecked', page)
    assert 'id="difficulty-extra"' in page


def test_fuzzy_matching_disregards_punctuation(app, admin_client):
    """A punctuation-heavy title matches its plain spelling — 'mash'
    names M*A*S*H (Glenn's report, Aug 27 2026) — while a wrong film
    is still a wrong film."""

    import re

    from app import db

    with app.app_context():
        movie = make_movie("M*A*S*H", 1970)
        make_movie_file(movie, "Bluray-1080p")
        db.session.commit()
        movie_id = movie.id

    token = seed_frame(app, movie_id)
    page = admin_client.get("/game?difficulty=siracusa&unrated=1").get_data(
        as_text=True
    )
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

    assert "Correct" in guess("mash")
    assert "Correct" in guess("M*A*S*H")
    assert "Correct" in guess("m.a.s.h.")
    assert "alert-danger" in guess("Catch-22")


def test_unrated_watches_do_not_count_as_rated(app, admin_client):
    """A diary row without a star rating — a Netflix-import watch,
    say — satisfies neither Easy nor the rated-only default (Glenn's
    Conversation report, Aug 27 2026: 'seen' surfaced films nobody
    remembers, so the filter now means rated)."""

    from app import db
    from app.models import UserMovieReview
    from app.videos import star_rating_fields
    from tests.test_recommendations import admin_id

    with app.app_context():
        watched = make_movie("Unrated Watch Film", 1974)
        make_movie_file(watched, "Bluray-1080p")
        rated = make_movie("Rated Film Proper", 1975)
        make_movie_file(rated, "Bluray-1080p")
        db.session.add(
            UserMovieReview(user_id=admin_id(), movie_id=watched.id, liked=False)
        )
        db.session.add(
            UserMovieReview(
                user_id=admin_id(),
                movie_id=rated.id,
                liked=True,
                **star_rating_fields(4.0),
            )
        )
        db.session.commit()
        watched_id, rated_id = watched.id, rated.id

    watched_token = seed_frame(app, watched_id)
    rated_token = seed_frame(app, rated_id)

    for difficulty in ("easy", "difficult"):
        for _ in range(3):
            page = admin_client.get(f"/game?difficulty={difficulty}").get_data(
                as_text=True
            )
            assert f'src="/game/frame/{rated_token}' in page
            assert watched_token not in page


def test_pool_floor_ignores_unrated_watches(app, monkeypatch):
    """The nightly rated floor matches the game's tightened world: an
    unrated watch can't claim a floor slot, or the floor would pool
    films Easy can no longer deal."""

    from app import db
    from app.models import UserMovieReview
    from app.frames import refresh_frame_pool_task
    from app.videos import star_rating_fields
    from tests.test_recommendations import admin_id

    monkeypatch.setitem(app.config, "FRAME_POOL_SIZE", 1)
    monkeypatch.setitem(app.config, "FRAME_POOL_ROTATE", 1)
    monkeypatch.setitem(app.config, "FRAME_POOL_MIN_RATED", 1)

    with app.app_context():
        watched = make_movie("Floor Unrated Watch", 1976)
        make_movie_file(watched, "Bluray-1080p")
        rated = make_movie("Floor Rated Star", 1977)
        make_movie_file(rated, "Bluray-1080p")
        db.session.add(
            UserMovieReview(user_id=admin_id(), movie_id=watched.id, liked=False)
        )
        db.session.add(
            UserMovieReview(
                user_id=admin_id(),
                movie_id=rated.id,
                liked=True,
                **star_rating_fields(3.5),
            )
        )
        db.session.commit()
        rated_id = rated.id

        refresh_frame_pool_task()
        jobs = [
            app.transcode_queue.fetch_job(job_id)
            for job_id in app.transcode_queue.get_job_ids()
        ]
        queued = [
            job.args[0] for job in jobs if "extract_frame_task" in (job.func_name or "")
        ]
        # The single slot goes to the starred film, not the bare watch
        assert queued == [rated_id]


def test_zoom_crops_avoid_letterbox_bars(app, admin_client):
    """Extra Difficult crops confine themselves to the frame's active
    picture area, so a zoom window never lands on baked-in letterbox
    or pillarbox bars (Glenn's report, Aug 27 2026) — and an all-dark
    frame falls back to the whole frame."""

    import io

    from PIL import Image

    from app import db
    from app.frames import POOL_KEY, frame_path
    from app.main.game import ACTIVE_LUMA, _active_picture_box, _crop_box

    # A letterboxed frame: grey picture between 20px black bars

    frame = Image.new("RGB", (200, 100), (0, 0, 0))
    frame.paste(Image.new("RGB", (200, 60), (128, 128, 128)), (0, 20))
    active = _active_picture_box(frame)
    assert active == (0, 20, 200, 80)

    for n in range(100):
        token = f"barstoken{n:04d}"
        s1 = _crop_box(token, 1, 200, 100, active)
        s2 = _crop_box(token, 2, 200, 100, active)
        for box in (s1, s2):
            assert box[1] >= 20 and box[3] <= 80  # never into the bars
            assert box[0] >= 0 and box[2] <= 200
        # The stages still nest inside the active area
        assert s2[0] <= s1[0] and s1[2] <= s2[2]
        assert s2[1] <= s1[1] and s1[3] <= s2[3]

    # No bright pixels at all: no active box, callers use the frame

    assert _active_picture_box(Image.new("RGB", (50, 40), (0, 0, 0))) is None

    # End to end: a letterboxed pooled frame serves bar-free crops

    with app.app_context():
        movie = make_movie("Letterboxed Film", 2006)
        make_movie_file(movie, "Bluray-1080p")
        db.session.commit()
        movie_id = movie.id
        os.makedirs(app.config["FRAME_POOL_DIR"], exist_ok=True)
        token = "barstokenlive"
        frame.save(frame_path(token), "JPEG", quality=95)
    app.redis.hset(
        POOL_KEY,
        token,
        json.dumps(
            {"movie_id": movie_id, "extracted_at": int(time.time()), "offset": 1.0}
        ),
    )

    admin_client.get("/game?difficulty=extra&unrated=1")
    served = admin_client.get(f"/game/frame/{token}?stage=1")
    crop = Image.open(io.BytesIO(served.data)).convert("L")
    darkest, _ = crop.getextrema()
    assert darkest > ACTIVE_LUMA  # not a single letterbox pixel
    assert crop.size[0] == 60  # 30% of the frame's width
    assert crop.size[1] < 24  # 30% of the ~60px active height, not the 100px frame


def test_fuzzy_matching_accepts_the_pre_subtitle_title(app, admin_client):
    """The part of a title before its subtitle stands alone — 'Rogue
    One' names 'Rogue One: A Star Wars Story' (Glenn's report, Aug 27
    2026), and the filename-safe ' - ' form local titles use splits
    the same way — while a different film still misses."""

    import re

    from app import db

    with app.app_context():
        # Local title carries the filename-safe dash; TMDB the colon
        movie = make_movie("Rogue One - A Star Wars Story", 2016)
        movie.tmdb_title = "Rogue One: A Star Wars Story"
        make_movie_file(movie, "Bluray-1080p")
        db.session.commit()
        movie_id = movie.id

    token = seed_frame(app, movie_id)
    page = admin_client.get("/game?difficulty=siracusa&unrated=1").get_data(
        as_text=True
    )
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

    assert "Correct" in guess("Rogue One")
    assert "Correct" in guess("rogue one a star wars story")
    assert "alert-danger" in guess("Solo")
    assert "alert-danger" in guess("A New Hope")


def test_fuzzy_matching_accepts_the_subtitle_alone(app, admin_client):
    """The post-colon half stands alone too — 'Wrath of Khan' is how
    people actually name that film (Glenn's ask, Aug 27 2026)."""

    import re

    from app import db

    with app.app_context():
        movie = make_movie("Star Trek II - The Wrath of Khan", 1982)
        movie.tmdb_title = "Star Trek II: The Wrath of Khan"
        make_movie_file(movie, "Bluray-1080p")
        db.session.commit()
        movie_id = movie.id

    token = seed_frame(app, movie_id)
    page = admin_client.get("/game?difficulty=siracusa&unrated=1").get_data(
        as_text=True
    )
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

    assert "Correct" in guess("wrath of khan")
    assert "Correct" in guess("The Wrath of Khan")
    assert "Correct" in guess("star trek ii")
    assert "alert-danger" in guess("The Search for Spock")


def test_fuzzy_matching_folds_spelled_numbers_to_digits(app, admin_client):
    """Digits and their spelled-out forms interchange — 'Pelham 123'
    names 'The Taking of Pelham One Two Three', and 'apollo thirteen'
    names Apollo 13 (Glenn's report, Aug 27 2026)."""

    import re

    from app import db

    with app.app_context():
        pelham = make_movie("The Taking of Pelham One Two Three", 1974)
        make_movie_file(pelham, "Bluray-1080p")
        apollo = make_movie("Apollo 13", 1995)
        make_movie_file(apollo, "Bluray-1080p")
        db.session.commit()
        pelham_id, apollo_id = pelham.id, apollo.id

    pelham_token = seed_frame(app, pelham_id)
    apollo_token = seed_frame(app, apollo_id)
    page = admin_client.get("/game?difficulty=siracusa&unrated=1").get_data(
        as_text=True
    )
    csrf = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page).group(1)

    def guess(token, text):
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

    assert "Correct" in guess(pelham_token, "The Taking of Pelham 123")
    assert "Correct" in guess(pelham_token, "taking of pelham one two three")
    assert "alert-danger" in guess(pelham_token, "The French Connection")
    assert "Correct" in guess(apollo_token, "apollo thirteen")
    assert "Correct" in guess(apollo_token, "Apollo 13")


def test_extra_unrated_hit_earns_a_hidden_double_bonus(app, admin_client):
    """Naming an unrated film on Extra Difficult doubles the stage's
    points — but the round prompt keeps quoting base points, since
    'guess now for 6 points' would itself mark the film as unrated.
    Only the toggle label and the after-the-fact reveal disclose it."""

    import re

    from app import db
    from app.models import UserFrameScore

    with app.app_context():
        movie = make_movie("Bonus Secret Film", 2008)
        make_movie_file(movie, "Bluray-1080p")
        db.session.commit()
        movie_id = movie.id

    token = seed_image_frame(app, movie_id)
    page = admin_client.get("/game?difficulty=extra&unrated=1").get_data(as_text=True)
    assert "(2x point bonus)" in page  # the toggle label may say so
    assert "3 points" in page  # the prompt quotes base points only
    assert "6 points" not in page
    csrf = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page).group(1)

    def post(data):
        return admin_client.post(
            "/game",
            data={"csrf_token": csrf, "token": token, "difficulty": "extra", **data},
        ).get_data(as_text=True)

    # A first-look hit banks 3 doubled to 6, and only now says why

    body = post({"guess": "Bonus Secret Film", "guess_submit": "y"})
    assert "(+6 points &mdash; 2x bonus for an unrated film)" in body
    with app.app_context():
        score = UserFrameScore.query.filter_by(difficulty="extra").one()
        assert (score.points, score.current_streak) == (6, 1)

    # A stage-two hit banks 2 doubled to 4 the same way

    page = admin_client.get("/game?difficulty=extra").get_data(as_text=True)
    assert "3 points" in page and "6 points" not in page  # no leak
    post({"zoom_out": "y"})
    body = post({"guess": "bonus secret film", "guess_submit": "y"})
    assert "(+4 points &mdash; 2x bonus for an unrated film)" in body
    with app.app_context():
        score = UserFrameScore.query.filter_by(difficulty="extra").one()
        assert (score.points, score.current_streak) == (10, 2)


def test_game_reopens_at_the_last_chosen_difficulty(app, admin_client):
    """A plain /game visit resumes the difficulty the user last chose
    instead of resetting to Easy (Glenn's ask, Aug 27 2026)."""

    import re

    page = admin_client.get("/game").get_data(as_text=True)
    assert re.search(r'id="difficulty-easy"[^>]*\schecked', page)

    admin_client.get("/game?difficulty=siracusa")
    page = admin_client.get("/game").get_data(as_text=True)
    assert re.search(r'id="difficulty-siracusa"[^>]*\schecked', page)
    assert not re.search(r'id="difficulty-easy"[^>]*\schecked', page)

    # An unknown slug falls back to the remembered pick, not Easy

    page = admin_client.get("/game?difficulty=bogus").get_data(as_text=True)
    assert re.search(r'id="difficulty-siracusa"[^>]*\schecked', page)
