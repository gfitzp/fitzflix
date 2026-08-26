"""The Recommendations page (#235): criteria-keyed shelves anchored by
two interest films, the eligibility pool (owned or streaming), the
fresh-per-reload draw, and the tile endpoint that refills a slot after
a rating, watchlist add, or wave-off."""

import json
import random

from app import db
from app.models import (
    MovieAward,
    UserMovieStatus,
    UserWatchlist,
)
from tests.factories import make_movie
from tests.test_elicitation import make_candidate
from tests.test_recommendations import (
    admin_id,
    genre,
    log_watch,
    make_person,
)


def make_award(movie, award_id="Q102427", name="Academy Award for Best Picture"):
    """An award WIN for the film."""

    row = MovieAward(
        movie_id=movie.id, award_id=award_id, award_name=name, win=True, year=None
    )
    db.session.add(row)
    db.session.flush()
    return row


def admin_user(app):
    """The seeded admin User object (shelf builders take the user, not
    the id — provider picks ride on it)."""

    from app.models import User

    return User.query.filter_by(admin=True).first()


def test_shelf_features_cover_awards_and_skip_language(app):
    """The shelves' feature space: the engine's classes minus
    language, with sentence-ready labels, plus one feature per award
    WON — nominations never key a shelf."""

    from app.models import TMDBKeyword

    with app.app_context():
        western = genre(37, "Western")
        director = make_person(801001, "Shelf Director")
        movie = make_candidate(
            "Shelf Featured", 1972, genre_row=western, director=director
        )
        movie.tmdb_original_language = "en"
        heist = TMDBKeyword(id=9717, name="heist")
        stinger = TMDBKeyword(id=179431, name="duringcreditsstinger")
        db.session.add_all([heist, stinger])
        movie.keywords.append(heist)
        movie.keywords.append(stinger)
        make_award(movie)
        nominated = MovieAward(
            movie_id=movie.id,
            award_id="Q103360",
            award_name="Academy Award for Best Director",
            win=False,
        )
        db.session.add(nominated)
        db.session.commit()

        from app.rec_shelves import shelf_features

        rows = shelf_features([movie.id])[movie.id]
        by_key = {key: (cls, label) for cls, key, label in rows}

        assert by_key["genre:37"] == ("genre", "Western films")
        assert by_key["decade:1970"] == ("decade", "the 1970s")
        assert by_key["director:801001"] == (
            "director",
            "films directed by Shelf Director",
        )
        assert by_key["award:Q102427"] == (
            "award",
            "Academy Award for Best Picture winners",
        )
        assert by_key["keyword:9717"] == ("keyword", "“heist” films")
        assert "award:Q103360" not in by_key
        # TMDB's bookkeeping keywords never key a shelf
        assert "keyword:179431" not in by_key
        assert not any(cls == "language" for cls, _ in by_key.values())


def test_eligible_films_need_local_files_or_streaming(app):
    """A shelf may suggest owned unseen films, and record-backed films
    the availability cache says are streaming on the user's services —
    never watchlisted films, seen films, or unavailable records."""

    from app.models import UserStreamingProvider

    with app.app_context():
        user = admin_user(app)
        user_id = int(user.id)
        owned = make_candidate("Pool Owned", 1990)
        seen = make_candidate("Pool Seen", 1991)
        log_watch(user_id, seen, rating=4)
        wanted = make_candidate("Pool Wanted", 1992)
        db.session.add(UserWatchlist(user_id=user_id, movie_id=wanted.id))

        # Record-backed, file-less films with TMDB data: one streaming
        # on the user's service, one not, one whose availability is
        # still unfetched
        from datetime import datetime

        streaming = make_movie("Pool Streaming", 1993, tmdb_id=910001)
        unavailable = make_movie("Pool Unavailable", 1994, tmdb_id=910002)
        unfetched = make_movie("Pool Unfetched", 1995, tmdb_id=910003)
        for record in (streaming, unavailable, unfetched):
            record.tmdb_data_as_of = datetime.now()
        db.session.add(UserStreamingProvider(user_id=user_id, provider_id=337))
        db.session.commit()

        app.redis.set(
            "fitzflix:tmdb:watch-providers:movie:910001",
            json.dumps(
                {"flatrate": [{"provider_id": 337, "provider_name": "Disney+"}]}
            ),
        )
        app.redis.set(
            "fitzflix:tmdb:watch-providers:movie:910002",
            json.dumps({"flatrate": [{"provider_id": 8, "provider_name": "Netflix"}]}),
        )

        from app.rec_shelves import eligible_films

        pool = set(eligible_films(user))
        assert owned.id in pool
        assert streaming.id in pool
        assert seen.id not in pool
        assert wanted.id not in pool
        assert unavailable.id not in pool
        assert unfetched.id not in pool


def test_build_shelves_anchors_and_criteria_overlap(app):
    """A shelf's two anchors are interest films carrying the seed, its
    criteria extend only with features both anchors share, and every
    suggested film carries the whole criteria set."""

    with app.app_context():
        user = admin_user(app)
        user_id = int(user.id)
        western = genre(37, "Western")

        anchor_a = make_candidate("Build Anchor A", 1961, genre_row=western)
        anchor_b = make_candidate("Build Anchor B", 1963, genre_row=western)
        log_watch(user_id, anchor_a, rating=5, liked=True)
        log_watch(user_id, anchor_b, rating=4, liked=True)

        suggestions = [
            make_candidate(f"Build Pick {n}", 1965 + n, genre_row=western)
            for n in range(5)
        ]
        drama = genre(18, "Drama")
        outsider = make_candidate("Build Outsider", 1999, genre_row=drama)
        db.session.commit()
        suggestion_ids = {movie.id for movie in suggestions}
        outsider_id = outsider.id
        anchor_ids = {anchor_a.id, anchor_b.id}

        from app.rec_shelves import build_shelves

        shelves = build_shelves(user, rng=random.Random(7))
        assert shelves, "no shelf built from a clean Western overlap"
        shelf = next(
            s for s in shelves if any(key == "genre:37" for key, _ in s["criteria"])
        )
        assert set(shelf["anchor_ids"]) <= anchor_ids
        assert len(shelf["anchor_ids"]) == 2
        assert set(shelf["movie_ids"]) <= suggestion_ids
        assert outsider_id not in shelf["movie_ids"]
        # Both 1960s anchors and every 1960s pick share the decade, so
        # the greedy extension may add it — every criterion must hold
        # for anchors and picks alike
        for key, label in shelf["criteria"]:
            assert key in ("genre:37", "decade:1960")
            assert label in ("Western films", "the 1960s")


def test_build_shelves_needs_two_interest_films(app):
    """One liked film can't anchor anything — shelves need two."""

    with app.app_context():
        user = admin_user(app)
        western = genre(37, "Western")
        only = make_candidate("Lonely Anchor", 1961, genre_row=western)
        log_watch(int(user.id), only, rating=5, liked=True)
        for n in range(5):
            make_candidate(f"Lonely Pick {n}", 1965 + n, genre_row=western)
        db.session.commit()

        from app.rec_shelves import build_shelves

        assert build_shelves(user, rng=random.Random(7)) == []


def test_recommendations_page_renders_shelves(app, admin_client):
    """The page shows a shelf's heading, its "Based on your interest
    in" caption naming both anchors, and its suggestion tiles."""

    with app.app_context():
        user_id = admin_id()
        western = genre(37, "Western")
        anchor_a = make_candidate("Page Anchor A", 1961, genre_row=western)
        anchor_b = make_candidate("Page Anchor B", 1963, genre_row=western)
        log_watch(user_id, anchor_a, rating=5, liked=True)
        log_watch(user_id, anchor_b, rating=4, liked=True)
        for n in range(5):
            make_candidate(f"Page Pick {n}", 1975 + n, genre_row=western)
        db.session.commit()
        pick_titles = [f"Page Pick {n}" for n in range(5)]

    page = admin_client.get("/recommendations").get_data(as_text=True)
    assert "Recommendations" in page
    assert "Based on your interest in" in page
    assert "Western films" in page
    assert "Page Anchor A (1961)" in page
    assert "Page Anchor B (1963)" in page
    assert 'data-criteria="' in page
    assert any(title in page for title in pick_titles)
    # Anchors are evidence, never suggestions
    assert "data-suggestion-cell" in page


def test_recommendations_page_empty_states(app, admin_client):
    """No diary yet: the log-a-few-films prompt. A diary but no
    overlapping unseen films: the not-enough-overlap prompt."""

    page = admin_client.get("/recommendations").get_data(as_text=True)
    assert "Log a few films to get recommendations." in page

    with app.app_context():
        user_id = admin_id()
        lone = make_candidate("Empty State Watched", 1980)
        log_watch(user_id, lone, rating=4)
        db.session.commit()

    page = admin_client.get("/recommendations").get_data(as_text=True)
    assert "Not enough overlapping interest" in page


def test_tile_endpoint_refills_and_exhausts(app, admin_client):
    """The tile endpoint returns the best remaining film matching the
    criteria (excluding what's showing), then 204 once the criteria
    set is spent; malformed criteria 400."""

    with app.app_context():
        western = genre(37, "Western")
        first = make_candidate("Tile First", 1971, genre_row=western)
        second = make_candidate("Tile Second", 1972, genre_row=western)
        drama = genre(18, "Drama")
        make_candidate("Tile Wrong Genre", 1973, genre_row=drama)
        db.session.commit()
        first_id, second_id = first.id, second.id

    response = admin_client.get(
        f"/recommendations/tile?criteria=genre:37&exclude={first_id}"
    )
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Tile Second (1972)" in body
    assert f'data-state-movie="{second_id}"' in body

    response = admin_client.get(
        f"/recommendations/tile?criteria=genre:37&exclude={first_id},{second_id}"
    )
    assert response.status_code == 204

    assert admin_client.get("/recommendations/tile?criteria=lies:1").status_code == 400
    assert admin_client.get("/recommendations/tile?criteria=genre:x").status_code == 400
    assert admin_client.get("/recommendations/tile").status_code == 400


def test_tile_endpoint_honors_every_criterion_and_fresh_verdicts(app, admin_client):
    """A multi-criteria slot refills only with films carrying ALL the
    criteria, and a film just rated, watchlisted, or waved off never
    comes back as its own replacement."""

    with app.app_context():
        user_id = admin_id()
        western = genre(37, "Western")
        both = make_candidate("Refill Both", 1971, genre_row=western)
        make_award(both, award_id="Q179808", name="Palme d'Or")
        make_candidate("Refill Genre Only", 1972, genre_row=western)
        rated_away = make_candidate("Refill Rated", 1973, genre_row=western)
        make_award(rated_away, award_id="Q179808", name="Palme d'Or")
        flagged_away = make_candidate("Refill Flagged", 1974, genre_row=western)
        make_award(flagged_away, award_id="Q179808", name="Palme d'Or")
        log_watch(user_id, rated_away, rating=2)
        db.session.add(
            UserMovieStatus(
                user_id=user_id, movie_id=flagged_away.id, kind="not_interested"
            )
        )
        db.session.commit()
        both_id = both.id

    response = admin_client.get("/recommendations/tile?criteria=genre:37,award:Q179808")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Refill Both (1971)" in body
    assert "Refill Genre Only" not in body

    response = admin_client.get(
        f"/recommendations/tile?criteria=genre:37,award:Q179808&exclude={both_id}"
    )
    assert response.status_code == 204


def test_reload_reshuffles_shelf_draw(app):
    """Two draws with different rngs may differ — the build is
    randomized, not day-frozen. (Deterministic given the same rng, so
    the test asserts the mechanism, not luck.)"""

    with app.app_context():
        user = admin_user(app)
        user_id = int(user.id)
        western = genre(37, "Western")
        drama = genre(18, "Drama")
        for n in range(3):
            liked_w = make_candidate(f"Draw Anchor W{n}", 1961 + n, genre_row=western)
            log_watch(user_id, liked_w, rating=5, liked=True)
            liked_d = make_candidate(f"Draw Anchor D{n}", 1981 + n, genre_row=drama)
            log_watch(user_id, liked_d, rating=5, liked=True)
        for n in range(8):
            make_candidate(f"Draw Pick W{n}", 1965 + n, genre_row=western)
            make_candidate(f"Draw Pick D{n}", 1985 + n, genre_row=drama)
        db.session.commit()

        from app.rec_shelves import build_shelves

        first = build_shelves(user, rng=random.Random(1))
        again = build_shelves(user, rng=random.Random(1))
        assert [s["criteria"] for s in first] == [s["criteria"] for s in again]
        assert [s["movie_ids"] for s in first] == [s["movie_ids"] for s in again]

        different = any(
            build_shelves(user, rng=random.Random(seed)) != first
            for seed in range(2, 8)
        )
        assert different, "six reseeded draws never varied the shelves"
