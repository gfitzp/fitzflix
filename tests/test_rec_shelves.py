"""Test the Recommendations page (#235).

The tests cover the criteria-keyed shelves that 2 interest films anchor
(or sometimes 1 film, #249), the eligibility pool (owned or streaming),
the new draw on each reload, and the tile endpoint. The tile endpoint
refills a slot after a rating, a watchlist add, or a wave-off."""

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
    """Create an award WIN for the film."""

    row = MovieAward(
        movie_id=movie.id, award_id=award_id, award_name=name, win=True, year=None
    )
    db.session.add(row)
    db.session.flush()
    return row


def admin_user(app):
    """Return the seeded admin User object.

    The shelf builders take the user, not the id. The provider picks go
    with the user."""

    from app.models import User

    return User.query.filter_by(admin=True).first()


def test_shelf_features_cover_awards_and_skip_language(app):
    """Make sure the shelf feature space is correct.

    The space is the engine classes without language, with labels that
    fit in a sentence. It has 1 feature for each award WON. A nomination
    never keys a shelf."""

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
        # Fitzflix stores the keyword names in lowercase. The label makes
        # the first character uppercase (Glenn: "“Poker” films", never
        # "“poker”").
        assert by_key["keyword:9717"] == ("keyword", "“Heist” films")
        assert "award:Q103360" not in by_key
        # A TMDB bookkeeping keyword never keys a shelf.
        assert "keyword:179431" not in by_key
        assert not any(cls == "language" for cls, _ in by_key.values())


def test_eligible_films_need_local_files_or_streaming(app):
    """Make sure the pool holds only owned or streaming films.

    A shelf can suggest an owned unseen film. It can suggest a film with
    a record that the availability cache reports as streaming on a
    service of the user. It never suggests a film on the watchlist, a
    seen film, or an unavailable record."""

    from app.models import UserStreamingProvider

    with app.app_context():
        user = admin_user(app)
        user_id = int(user.id)
        owned = make_candidate("Pool Owned", 1990)
        seen = make_candidate("Pool Seen", 1991)
        log_watch(user_id, seen, rating=4)
        wanted = make_candidate("Pool Wanted", 1992)
        db.session.add(UserWatchlist(user_id=user_id, movie_id=wanted.id))

        # These films have a record, no files, and TMDB data. One streams
        # on the service of the user. One does not. One has no fetched
        # availability yet.
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
    """Make sure the anchors and the criteria of a shelf agree.

    The 2 anchors of a shelf are interest films that carry the seed. The
    criteria extend only with features that both anchors share. Each
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
        assert shelf["kind"] == "criteria"
        assert set(shelf["anchor_ids"]) <= anchor_ids
        assert len(shelf["anchor_ids"]) == 2
        assert set(shelf["movie_ids"]) <= suggestion_ids
        assert outsider_id not in shelf["movie_ids"]
        # Both 1960s anchors and each 1960s pick share the decade. Thus,
        # the greedy extension can add it. Each criterion must hold for
        # the anchors and for the picks.
        for key, label in shelf["criteria"]:
            assert key in ("genre:37", "decade:1960")
            assert label in ("Western films", "the 1960s")


def test_single_holder_genre_or_decade_never_seeds(app):
    """Make sure 1 liked film with only a genre and a decade anchors nothing.

    One film is too weak an evidence base for the broad classes. A
    single-anchor shelf (#249) needs a specific feature (a person, a
    keyword, or an award) or copref coverage."""

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


def test_single_anchor_criteria_shelf_from_specific_feature(app):
    """Make sure a director loved exactly 1 time can key a single-anchor shelf.

    This is #249. The shelf has kind criteria. The 1 holder is the only
    anchor. The director is a criterion. The shelf suggests only films
    by that director."""

    with app.app_context():
        user = admin_user(app)
        director = make_person(801249, "Solo Director")
        only = make_candidate("Solo Anchor", 1961, director=director)
        log_watch(int(user.id), only, rating=5, liked=True)
        picks = [
            make_candidate(f"Solo Pick {n}", 1965 + n, director=director)
            for n in range(4)
        ]
        outsider = make_candidate("Solo Outsider", 1999)
        db.session.commit()
        only_id = only.id
        pick_ids = {movie.id for movie in picks}
        outsider_id = outsider.id

        from app.rec_shelves import build_shelves

        shelves = build_shelves(user, rng=random.Random(7))
        assert shelves, "no shelf from a single-holder director seed"
        shelf = next(
            s
            for s in shelves
            if any(key == "director:801249" for key, _ in s["criteria"])
        )
        assert shelf["kind"] == "criteria"
        assert shelf["anchor_ids"] == [only_id]
        assert set(shelf["movie_ids"]) <= pick_ids
        assert outsider_id not in shelf["movie_ids"]


def test_recommendations_page_renders_shelves(app, admin_client):
    """Make sure the page renders a shelf.

    The page shows the heading of the shelf, the fanned anchor slot that
    names both anchors under a "Because you liked" eyebrow, and the
    suggestion tiles."""

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
    assert "Western films" in page
    assert 'class="anchor-slot"' in page
    assert "Because you liked" in page
    # Only the tooltip and the alt text of the poster links name the
    # anchors. There is no visible title list under the fan (Glenn,
    # 2026-08-26).
    assert 'title="Page Anchor A (1961)"' in page
    assert 'title="Page Anchor B (1963)"' in page
    assert "anchor-slot-titles" not in page
    assert "anchor-fan-back" in page and "anchor-fan-front" in page
    assert 'data-criteria="' in page
    assert any(title in page for title in pick_titles)
    # The old prose caption is gone. The slot carries the anchors now.
    assert "Based on your interest in" not in page
    # An anchor is evidence. It is never a suggestion.
    assert "data-suggestion-cell" in page
    # The films go in their own shelf-films row. The phone layout makes
    # that row the one-card swipe strip next to the fixed anchor slot
    # (Glenn, 2026-08-26).
    assert "shelf-films" in page


def test_recommendations_page_empty_states(app, admin_client):
    """Make sure the page shows the correct empty state.

    With no diary, the page shows the log-some-films prompt. With a
    diary but no unseen films in common, the page shows the
    not-enough-overlap prompt."""

    page = admin_client.get("/recommendations").get_data(as_text=True)
    assert "Log some films to get recommendations." in page

    with app.app_context():
        user_id = admin_id()
        lone = make_candidate("Empty State Watched", 1980)
        log_watch(user_id, lone, rating=4)
        db.session.commit()

    page = admin_client.get("/recommendations").get_data(as_text=True)
    assert "There is not enough common interest" in page


def test_tile_endpoint_refills_and_exhausts(app, admin_client):
    """Make sure the tile endpoint refills a slot and then reports 204.

    The endpoint returns the best remaining film that matches the
    criteria. It excludes the films that show. When the criteria set is
    used up, it returns 204. Malformed criteria return 400."""

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
    """Make sure a refill honors each criterion and each new verdict.

    A slot with more than 1 criterion refills only with a film that
    carries ALL the criteria. A film that the user rated, added to the
    watchlist, or waved off never comes back as its own replacement."""

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
    """Make sure 2 draws with different rngs can be different.

    The build is random, not frozen for the day. The build is
    deterministic for the same rng. Thus, the test asserts the
    mechanism, not luck."""

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


def make_copref(tmdb_a, tmdb_b, similarity):
    """Create 1 directed co-preference similarity row."""

    from app.models import MovieCopref

    row = MovieCopref(tmdb_id_a=tmdb_a, tmdb_id_b=tmdb_b, similarity=similarity)
    db.session.add(row)
    db.session.flush()
    return row


def copref_fixture(app):
    """Create 2 liked anchors (tmdb 501/502) with a joint neighbor list.

    The neighbor lists together cover 4 owned candidates (tmdb 601-604).
    One more film is close to only 1 anchor. Return (user, anchor ids,
    candidate ids)."""

    user = admin_user(app)
    user_id = int(user.id)
    anchor_a = make_candidate("Copref Anchor A", 1977)
    anchor_a.tmdb_id = 501
    anchor_b = make_candidate("Copref Anchor B", 1980)
    anchor_b.tmdb_id = 502
    log_watch(user_id, anchor_a, rating=5, liked=True)
    log_watch(user_id, anchor_b, rating=5, liked=True)

    candidates = []
    for n in range(4):
        movie = make_candidate(f"Copref Joint {n}", 1984 + n)
        movie.tmdb_id = 601 + n
        candidates.append(movie)
        make_copref(501, movie.tmdb_id, 0.30 - n * 0.02)
        make_copref(502, movie.tmdb_id, 0.28 - n * 0.02)
    lonely = make_candidate("Copref One Sided", 1990)
    lonely.tmdb_id = 699
    make_copref(501, lonely.tmdb_id, 0.9)
    db.session.commit()
    return user, (anchor_a.id, anchor_b.id), [movie.id for movie in candidates]


def test_build_shelves_draws_a_copref_pair_shelf(app):
    """Make sure 2 liked films with a deep joint neighbor list give a copref shelf.

    The shelf has kind "copref", the pair as anchors, and a
    copref:{tmdbA}:{tmdbB} criteria key. It suggests only JOINT
    neighbors. A film similar to only 1 anchor never shows."""

    with app.app_context():
        user, anchor_ids, candidate_ids = copref_fixture(app)

        from app.rec_shelves import build_shelves

        shelves = build_shelves(user, rng=random.Random(3))
        copref = [s for s in shelves if s["kind"] == "copref"]
        assert copref, "no copref shelf from a covered pair"
        shelf = copref[0]
        assert set(shelf["anchor_ids"]) == set(anchor_ids)
        key = shelf["criteria"][0][0]
        assert sorted(key.split(":")[1:]) == ["501", "502"]
        assert set(shelf["movie_ids"]) <= set(candidate_ids)
        lonely = [m for m in shelf["movie_ids"] if m not in candidate_ids]
        assert lonely == []


def test_parse_criteria_copref_key(app):
    from app.rec_shelves import parse_criteria

    assert parse_criteria("copref:501:502") == [("copref", (501, 502))]
    # This is the key of a single-anchor shelf (#249).
    assert parse_criteria("copref:501") == [("copref", (501,))]
    assert parse_criteria("copref:") is None
    assert parse_criteria("copref:abc") is None
    assert parse_criteria("copref:501:abc") is None
    assert parse_criteria("copref:501:502:503") is None
    assert parse_criteria("copref:501:502,genre:37") is None


def test_copref_tile_refills_by_joint_similarity(app, admin_client):
    """Make sure the tile endpoint refills a copref slot by joint similarity.

    The endpoint refills the slot with the next film most similar to
    BOTH anchors. It skips the films that show. When the joint list is
    used up, it returns 204."""

    with app.app_context():
        user, _, candidate_ids = copref_fixture(app)
        best, runner_up = candidate_ids[0], candidate_ids[1]

    response = admin_client.get(
        f"/recommendations/tile?criteria=copref:501:502&exclude={best}"
    )
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Copref Joint 1" in body
    assert f'data-state-movie="{runner_up}"' in body
    assert "Copref One Sided" not in body

    exclude = ",".join(str(movie_id) for movie_id in candidate_ids)
    response = admin_client.get(
        f"/recommendations/tile?criteria=copref:501:502&exclude={exclude}"
    )
    assert response.status_code == 204


def test_copref_shelf_page_render(app, admin_client):
    """Make sure a copref shelf renders under its own heading.

    The shelf has the anchor slot and no criteria-style caption."""

    with app.app_context():
        copref_fixture(app)

    page = admin_client.get("/recommendations").get_data(as_text=True)
    assert "More like these two" in page
    assert "The people who loved the two films also loved these" in page
    assert 'title="Copref Anchor A (1977)"' in page
    assert "Copref Anchor B (1980)" in page
    assert 'data-criteria="copref:501:502"' in page or (
        'data-criteria="copref:502:501"' in page
    )


def copref_single_fixture(app, count=4, second_anchor=False):
    """Create 1 liked anchor (tmdb 501) with NO partner anchor.

    Its neighbor list covers `count` owned candidates (tmdb 601+). The
    fixture can add a second anchor with no partner (tmdb 502, neighbors
    701+). Return (user, anchor ids, candidate ids)."""

    user = admin_user(app)
    user_id = int(user.id)
    anchor_a = make_candidate("Copref Solo A", 1977)
    anchor_a.tmdb_id = 501
    log_watch(user_id, anchor_a, rating=5, liked=True)
    anchor_ids = [anchor_a.id]

    candidates = []
    for n in range(count):
        movie = make_candidate(f"Copref Solo Pick {n}", 1984 + n)
        movie.tmdb_id = 601 + n
        candidates.append(movie)
        make_copref(501, movie.tmdb_id, 0.30 - n * 0.02)

    if second_anchor:
        anchor_b = make_candidate("Copref Solo B", 1996)
        anchor_b.tmdb_id = 502
        log_watch(user_id, anchor_b, rating=5, liked=True)
        anchor_ids.append(anchor_b.id)
        for n in range(count):
            movie = make_candidate(f"Copref Solo Other {n}", 2004 + n)
            movie.tmdb_id = 701 + n
            candidates.append(movie)
            make_copref(502, movie.tmdb_id, 0.30 - n * 0.02)

    db.session.commit()
    return user, anchor_ids, [movie.id for movie in candidates]


def test_single_anchor_copref_shelf_for_pairless_anchor(app):
    """Make sure an anchor with no partner can front a single-anchor copref shelf.

    This is #249. The shelf has 1 anchor id, a copref:{tmdb} key, and
    suggestions from the neighbor list of the anchor. Two anchors with no
    partner still give only ONE single-anchor shelf for each load."""

    with app.app_context():
        user, anchor_ids, candidate_ids = copref_single_fixture(app, second_anchor=True)

        from app.rec_shelves import build_shelves

        shelves = build_shelves(user, rng=random.Random(3))
        copref = [s for s in shelves if s["kind"] == "copref"]
        assert len(copref) == 1, "the single-anchor budget is one per load"
        shelf = copref[0]
        assert len(shelf["anchor_ids"]) == 1
        assert shelf["anchor_ids"][0] in anchor_ids
        key = shelf["criteria"][0][0]
        assert key in ("copref:501", "copref:502")
        assert set(shelf["movie_ids"]) <= set(candidate_ids)


def test_single_anchor_budget_spans_both_kinds(app):
    """Make sure 1 load never fronts 2 single-anchor shelves.

    This holds even when a copref anchor with no partner and a one-holder
    criteria seed both qualify. The copref single draws first and uses
    up the budget."""

    with app.app_context():
        user = admin_user(app)
        user_id = int(user.id)
        director = make_person(801250, "Budget Director")
        anchor = make_candidate("Budget Anchor", 1977, director=director)
        anchor.tmdb_id = 501
        log_watch(user_id, anchor, rating=5, liked=True)
        # There are 10 candidates that carry BOTH signals. Thus, after 5
        # copref picks, the director seed still has enough films for a
        # shelf. Only the budget can block it.
        for n in range(10):
            movie = make_candidate(f"Budget Pick {n}", 1984 + n, director=director)
            movie.tmdb_id = 601 + n
            make_copref(501, movie.tmdb_id, 0.40 - n * 0.02)
        db.session.commit()

        from app.rec_shelves import build_shelves

        shelves = build_shelves(user, rng=random.Random(3))
        singles = [s for s in shelves if len(s["anchor_ids"]) == 1]
        assert len(singles) == 1
        assert singles[0]["kind"] == "copref"
        assert [s["kind"] for s in shelves] == ["copref"]


def test_single_anchor_copref_tile_refill(app, admin_client):
    """Make sure the tile endpoint refills a single-anchor copref slot.

    The endpoint uses the neighbor list of the anchor. When the list is
    used up, it returns 204."""

    with app.app_context():
        _, _, candidate_ids = copref_single_fixture(app)
        best, runner_up = candidate_ids[0], candidate_ids[1]

    response = admin_client.get(
        f"/recommendations/tile?criteria=copref:501&exclude={best}"
    )
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Copref Solo Pick 1" in body
    assert f'data-state-movie="{runner_up}"' in body

    exclude = ",".join(str(movie_id) for movie_id in candidate_ids)
    response = admin_client.get(
        f"/recommendations/tile?criteria=copref:501&exclude={exclude}"
    )
    assert response.status_code == 204


def test_single_anchor_shelf_page_render(app, admin_client):
    """Make sure a single-anchor copref shelf renders correctly.

    The shelf has the singular heading and caption, a centered solo card
    in the anchor slot, and the one-id criteria key."""

    with app.app_context():
        copref_single_fixture(app)

    page = admin_client.get("/recommendations").get_data(as_text=True)
    assert "More like this one" in page
    assert "The people who loved it also loved these" in page
    assert 'title="Copref Solo A (1977)"' in page
    assert 'class="anchor-fan-solo"' in page
    # The pair classes name only CSS rules here, never a card
    assert 'class="anchor-fan-back"' not in page
    assert 'class="anchor-fan-front"' not in page
    assert 'data-criteria="copref:501"' in page


def test_anchors_require_a_rated_or_liked_verdict(app):
    """Make sure only a rated or liked film can be an anchor.

    A watchlist add or a bare unrated watch shows interest. Both pass
    the weight bar. But neither can front a "Because you liked" shelf.
    Anchors are rated-or-liked films only (rule set by Glenn,
    2026-08-26)."""

    with app.app_context():
        user = admin_user(app)
        user_id = int(user.id)
        western = genre(37, "Western")

        bare = make_candidate("Verdict Bare Watch", 1961, genre_row=western)
        log_watch(user_id, bare)  # seen, not rated, not liked
        wanted = make_candidate("Verdict Watchlisted", 1963, genre_row=western)
        db.session.add(UserWatchlist(user_id=user_id, movie_id=wanted.id))
        for n in range(5):
            make_candidate(f"Verdict Pick {n}", 1965 + n, genre_row=western)
        db.session.commit()

        from app.rec_shelves import build_shelves

        assert build_shelves(user, rng=random.Random(7)) == []

        # The same films with real verdicts anchor immediately.

        log_watch(user_id, bare, rating=4)
        rewatch = make_candidate("Verdict Liked", 1962, genre_row=western)
        log_watch(user_id, rewatch, liked=True)
        db.session.commit()
        bare_id, liked_id = bare.id, rewatch.id

        shelves = build_shelves(user, rng=random.Random(7))
        assert shelves
        assert set(shelves[0]["anchor_ids"]) <= {bare_id, liked_id}


def test_keyword_display_name_learns_from_overviews(app):
    """Make sure the keyword case comes from mid-sentence overview prose.

    A proper noun or an acronym adopts its majority case. A common noun
    gets an uppercase first character. A capital at the start of a
    sentence never votes."""

    with app.app_context():
        make_movie(
            "Case Bureau One",
            1990,
            tmdb_overview="An agent of the FBI goes undercover.",
        )
        make_movie(
            "Case Bureau Two",
            1991,
            tmdb_overview="Chased by the FBI across three states.",
        )
        make_movie(
            "Case Common",
            1992,
            tmdb_overview="A tense poker game turns deadly. Poker was his life.",
        )
        db.session.commit()

        from app.rec_shelves import keyword_display_name

        assert keyword_display_name("fbi") == "FBI"
        # "Poker" opens a sentence. Thus, it does not vote. The
        # mid-sentence lowercase wins, and the fallback capitalizes.
        assert keyword_display_name("poker") == "Poker"
        # There are no occurrences. The plain fallback applies.
        assert keyword_display_name("new york city") == "New york city"

        # Fitzflix caches the resolutions. New contrary evidence does not
        # change a cached answer until the TTL expires.
        make_movie(
            "Case Bureau Three",
            1993,
            tmdb_overview="He mocked the fbi in lowercase repeatedly.",
        )
        db.session.commit()
        assert keyword_display_name("fbi") == "FBI"


def test_shelf_criteria_use_resolved_keyword_casing(app):
    """Make sure a keyword criterion uses the resolved case.

    The label uses the case from the overviews, not the raw lowercase
    name."""

    from app.models import TMDBKeyword

    with app.app_context():
        user = admin_user(app)
        user_id = int(user.id)
        nyc = TMDBKeyword(id=18426, name="new york city")
        db.session.add(nyc)
        movies = []
        for n, title in enumerate(["Casing Anchor A", "Casing Anchor B"]):
            movie = make_candidate(title, 1971 + n)
            movie.keywords.append(nyc)
            movie.tmdb_overview = f"A story set in New York City, part {n}."
            log_watch(user_id, movie, rating=5, liked=True)
            movies.append(movie)
        for n in range(5):
            movie = make_candidate(f"Casing Pick {n}", 1980 + n)
            movie.keywords.append(nyc)
            movie.tmdb_overview = f"Crime in New York City, chapter {n}."
        db.session.commit()

        from app.rec_shelves import build_shelves

        shelves = build_shelves(user, rng=random.Random(5))
        labels = [
            label
            for shelf in shelves
            for key, label in shelf["criteria"]
            if key == "keyword:18426"
        ]
        assert labels and labels[0] == "“New York City” films"


def test_shelf_kinds_mix_positions_across_draws(app):
    """Make sure the shelf kinds mix positions across draws.

    The copref shelves draw first to claim their films. But they never
    own the top of the page. The final order shuffles. Thus, across
    reseeded draws, both kinds appear in the lead position."""

    with app.app_context():
        user, _, _ = copref_fixture(app)
        user_id = int(user.id)
        western = genre(37, "Western")
        anchor_a = make_candidate("Mix Anchor A", 1961, genre_row=western)
        anchor_b = make_candidate("Mix Anchor B", 1963, genre_row=western)
        log_watch(user_id, anchor_a, rating=5, liked=True)
        log_watch(user_id, anchor_b, rating=4, liked=True)
        for n in range(5):
            make_candidate(f"Mix Pick {n}", 1965 + n, genre_row=western)
        db.session.commit()

        from app.rec_shelves import build_shelves

        leaders = set()
        for seed in range(12):
            shelves = build_shelves(user, rng=random.Random(seed))
            kinds = {shelf["kind"] for shelf in shelves}
            assert kinds == {"copref", "criteria"}, "fixture must yield both kinds"
            leaders.add(shelves[0]["kind"])
            if leaders == {"copref", "criteria"}:
                break
        assert leaders == {"copref", "criteria"}
