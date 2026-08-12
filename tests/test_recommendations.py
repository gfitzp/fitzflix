"""The content-based recommendation engine: diary-derived taste
profiles, the nightly recompute into Redis, the landing page, and the
filmography interest markers."""

import json

import pytest

from tests.factories import make_movie, make_movie_file


def make_person(person_id, name):
    """A TMDBCredit row."""

    from app import db
    from app.models import TMDBCredit

    person = TMDBCredit(id=person_id, name=name)
    db.session.add(person)
    db.session.flush()
    return person


def make_cast(person, movie, character="Self", order=0):
    """A cast join row."""

    from app import db
    from app.models import MovieCast

    cast = MovieCast(
        movie_id=movie.id,
        credit_id=person.id,
        character=character,
        billing_order=order,
    )
    db.session.add(cast)
    db.session.flush()
    return cast


def genre(genre_id, name):
    """A TMDBGenre row, reused if the id already exists."""

    from app import db
    from app.models import TMDBGenre

    existing = db.session.get(TMDBGenre, genre_id)
    if existing:
        return existing
    row = TMDBGenre(id=genre_id, name=name)
    db.session.add(row)
    db.session.flush()
    return row


def log_watch(user_id, movie, rating=None, liked=False):
    """A diary row for the given movie."""

    from app import db
    from app.models import UserMovieReview
    from app.videos import star_rating_fields

    row = UserMovieReview(
        user_id=user_id,
        movie_id=movie.id,
        liked=liked,
        **star_rating_fields(rating),
    )
    db.session.add(row)
    db.session.flush()
    return row


def admin_id():
    """The seeded admin user's id."""

    from app.models import User

    return User.query.filter_by(admin=True).first().id


def test_user_movie_weights_math(app):
    from app.recommendations import user_movie_weights

    with app.app_context():
        user_id = admin_id()
        loved = make_movie("Weights Loved", 1990)
        meh = make_movie("Weights Meh", 1991)
        watched_twice = make_movie("Weights Watched Twice", 1992)
        log_watch(user_id, loved, rating=5, liked=True)
        log_watch(user_id, meh, rating=3)
        log_watch(user_id, watched_twice)
        log_watch(user_id, watched_twice)

        weights = user_movie_weights(user_id)

    # Mean rating is 4: the 5 centers to +0.4 plus the 1.0 like bonus;
    # the 3 centers to -0.4; two unrated watches are a bare watch plus
    # one rewatch increment

    assert weights[loved.id] == pytest.approx(1.4)
    assert weights[meh.id] == pytest.approx(-0.4)
    assert weights[watched_twice.id] == pytest.approx(0.55)


def test_recommendations_prefer_matching_features_and_say_why(app):
    from app.recommendations import compute_user_recommendations

    with app.app_context():
        user_id = admin_id()
        comedy = genre(35, "Comedy")
        drama = genre(18, "Drama")

        liked_comedy = make_movie("Rec Liked Comedy", 1994)
        liked_comedy.genres.append(comedy)
        log_watch(user_id, liked_comedy, liked=True)

        candidate_comedy = make_movie("Rec Candidate Comedy", 1995)
        candidate_comedy.genres.append(comedy)
        make_movie_file(candidate_comedy, "Bluray-1080p")

        candidate_drama = make_movie("Rec Candidate Drama", 1953)
        candidate_drama.genres.append(drama)
        make_movie_file(candidate_drama, "Bluray-1080p")

        profile, ranked = compute_user_recommendations(user_id)

    assert profile["affinities"]["genre:35"]["score"] > 0
    ranked_ids = [rec["movie_id"] for rec in ranked]
    assert ranked_ids == [candidate_comedy.id]
    # The strongest contributing feature explains the pick
    assert ranked[0]["because"][0] == "Comedy"


def test_crew_roles_are_separate_feature_classes(app):
    """A shared cinematographer builds a cinematographer affinity, scores
    the candidate, and explains itself in role terms."""

    from app import db
    from app.models import MovieCrew
    from app.recommendations import compute_user_recommendations

    with app.app_context():
        user_id = admin_id()
        dp = make_person(888001, "Famous DP")
        liked = make_movie("Crew Liked", 1990)
        log_watch(user_id, liked, liked=True)
        candidate = make_movie("Crew Candidate", 1991)
        make_movie_file(candidate, "Bluray-1080p")
        candidate_id = candidate.id
        for movie in (liked, candidate):
            db.session.add(
                MovieCrew(
                    movie_id=movie.id,
                    credit_id=dp.id,
                    department="Camera",
                    job="Director of Photography",
                )
            )
        db.session.flush()

        profile, ranked = compute_user_recommendations(user_id)

    assert profile["affinities"]["cinematographer:888001"]["score"] > 0
    assert ranked[0]["movie_id"] == candidate_id
    assert "shot by Famous DP" in ranked[0]["because"]


def test_seen_and_extras_only_films_are_not_candidates(app):
    from app.recommendations import local_candidates

    with app.app_context():
        user_id = admin_id()
        seen = make_movie("Cand Seen", 1990)
        make_movie_file(seen, "Bluray-1080p")
        log_watch(user_id, seen, rating=4)

        extras_only = make_movie("Cand Extras Only", 1991)
        make_movie_file(extras_only, "Bluray-1080p", "Behind The Scenes")

        available = make_movie("Cand Available", 1992)
        make_movie_file(available, "Bluray-1080p")

        assert local_candidates(user_id) == [available.id]


def test_recompute_task_stores_recs_and_profile(app):
    from app.recommendations import PROFILE_KEY, RECS_KEY, recompute_recommendations

    with app.app_context():
        user_id = admin_id()
        comedy = genre(35, "Comedy")
        liked = make_movie("Store Liked", 1994)
        liked.genres.append(comedy)
        log_watch(user_id, liked, liked=True)
        candidate = make_movie("Store Candidate", 1995)
        candidate.genres.append(comedy)
        make_movie_file(candidate, "Bluray-1080p")
        candidate_id = candidate.id
        from app import db

        db.session.commit()

    assert recompute_recommendations() is True

    stored = json.loads(app.redis.get(RECS_KEY.format(user_id=user_id)))
    assert stored["computed_at"]
    assert [item["movie_id"] for item in stored["items"]] == [candidate_id]

    profile = json.loads(app.redis.get(PROFILE_KEY.format(user_id=user_id)))
    assert profile["affinities"]["genre:35"]["score"] > 0


def test_landing_page_shows_recommendations(app, admin_client):
    from app.recommendations import RECS_KEY

    with app.app_context():
        user_id = admin_id()
        pick = make_movie("Landing Pick", 1995, tmdb_poster_path="/pick.jpg")
        make_movie_file(pick, "Bluray-1080p")
        seen_since = make_movie("Landing Seen Since", 1996)
        make_movie_file(seen_since, "Bluray-1080p")
        log_watch(user_id, seen_since, rating=4)
        pick_id, seen_since_id = pick.id, seen_since.id
        from app import db

        db.session.commit()

    app.redis.set(
        RECS_KEY.format(user_id=user_id),
        json.dumps(
            {
                "computed_at": "2026-08-10 01:45",
                "items": [
                    {"movie_id": pick_id, "score": 1.0, "because": ["Comedy", "1990s"]},
                    # Logged since the nightly run: must drop out today
                    {"movie_id": seen_since_id, "score": 0.9, "because": ["Comedy"]},
                    # Deleted since the nightly run: must not error
                    {"movie_id": 999999, "score": 0.8, "because": ["Comedy"]},
                ],
            }
        ),
    )

    body = admin_client.get("/").get_data(as_text=True)
    assert "Pick something to watch" in body
    assert "Landing Pick (1995)" in body
    assert "Comedy" in body
    assert "last run 2026-08-10 01:45" in body
    assert "Landing Seen Since" not in body


def test_landing_page_requests_compute_once(app, admin_client):
    with app.app_context():
        user_id = admin_id()
        log_watch(user_id, make_movie("Landing History Row", 1990), rating=4)
        from app import db

        db.session.commit()

    body = admin_client.get("/").get_data(as_text=True)
    assert "being computed" in body

    recompute_jobs = [
        job
        for job in app.maintenance_queue.jobs
        if job.func_name == "app.recommendations.recompute_recommendations"
    ]
    assert len(recompute_jobs) == 1

    # A second load must not enqueue a second job

    admin_client.get("/")
    recompute_jobs = [
        job
        for job in app.maintenance_queue.jobs
        if job.func_name == "app.recommendations.recompute_recommendations"
    ]
    assert len(recompute_jobs) == 1


def test_landing_page_onboards_users_without_history(app, user_client):
    body = user_client.get("/").get_data(as_text=True)
    assert "Log a few films" in body
    assert app.maintenance_queue.jobs == []


def test_recently_added_lives_at_its_own_route(app, admin_client):
    body = admin_client.get("/recently-added").get_data(as_text=True)
    assert "Recently Added" in body

    # And the nav links to it from every page

    landing = admin_client.get("/").get_data(as_text=True)
    assert 'href="/recently-added"' in landing


def test_filmography_marks_films_that_might_interest(app, admin_client, monkeypatch):
    """Unowned films on a filmography get a modest marker when their
    cached genre ids and decade match the stored taste profile."""

    import app.main.routes as main_routes

    from app import db
    from app.recommendations import PROFILE_KEY

    with app.app_context():
        user_id = admin_id()
        person = make_person(777001, "Marker Actor")
        owned = make_movie("Marker Owned", 1980, tmdb_id=300)
        make_movie_file(owned, "Bluray-1080p")
        make_cast(person, owned)
        db.session.commit()

    class FakeTMDb:
        """Canned TMDb response."""

        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            """Never an HTTP error."""

        def json(self):
            """The canned payload."""

            return self.payload

    def fake_tmdb_get(url, **kwargs):
        """Person-details and movie-credits payloads without the network."""

        if url.endswith("/movie_credits"):
            return FakeTMDb(
                {
                    "cast": [
                        {
                            "id": 300,
                            "title": "Marker Owned",
                            "release_date": "1980-05-01",
                            "character": "Lead",
                            "genre_ids": [35],
                        },
                        {
                            "id": 400,
                            "title": "Marker Matching Unowned",
                            "release_date": "1999-09-09",
                            "character": "Cameo",
                            "genre_ids": [35],
                        },
                        {
                            "id": 500,
                            "title": "Marker Unmatching Unowned",
                            "release_date": "2001-01-01",
                            "character": "Cameo",
                            "genre_ids": [18],
                        },
                    ]
                }
            )
        return FakeTMDb({"name": "Marker Actor"})

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(main_routes, "tmdb_get", fake_tmdb_get)

    app.redis.set(
        PROFILE_KEY.format(user_id=user_id),
        json.dumps(
            {
                "affinities": {
                    "genre:35": {
                        "class": "genre",
                        "label": "Comedy",
                        "count": 3,
                        "score": 0.5,
                    }
                },
                "movies": 3,
            }
        ),
    )

    page = admin_client.get("/library/movie?credit=777001").get_data(as_text=True)
    assert page.count("Might interest you") == 1
    # The marker sits on the matching unowned film only: the page order
    # is chronological, so the marked row is between the two known titles
    assert page.index("Marker Matching Unowned") < page.index("Might interest you")
    assert page.index("Might interest you") < page.index("Marker Unmatching Unowned")


def test_no_markers_without_a_stored_profile(app, admin_client, monkeypatch):
    import app.main.routes as main_routes

    from app import db

    with app.app_context():
        person = make_person(777002, "Profileless Actor")
        owned = make_movie("Profileless Owned", 1980, tmdb_id=310)
        make_movie_file(owned, "Bluray-1080p")
        make_cast(person, owned)
        db.session.commit()

    class FakeTMDb:
        """Canned TMDb response."""

        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            """Never an HTTP error."""

        def json(self):
            """The canned payload."""

            return self.payload

    def fake_tmdb_get(url, **kwargs):
        """Minimal person + credits payloads."""

        if url.endswith("/movie_credits"):
            return FakeTMDb(
                {
                    "cast": [
                        {
                            "id": 410,
                            "title": "Profileless Unowned",
                            "release_date": "1999-09-09",
                            "genre_ids": [35],
                        }
                    ]
                }
            )
        return FakeTMDb({"name": "Profileless Actor"})

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(main_routes, "tmdb_get", fake_tmdb_get)

    page = admin_client.get("/library/movie?credit=777002").get_data(as_text=True)
    assert "Might interest you" not in page


def test_evaluate_user_reports_ranking_metrics(app):
    from app.recommendations import evaluate_user

    with app.app_context():
        user_id = admin_id()
        comedy = genre(35, "Comedy")
        for n in range(3):
            liked = make_movie(f"Eval Liked {n}", 1990 + n)
            liked.genres.append(comedy)
            make_movie_file(liked, "Bluray-1080p")
            log_watch(user_id, liked, rating=5, liked=True)
        drama = genre(18, "Drama")
        for n in range(3):
            candidate = make_movie(f"Eval Candidate {n}", 1950 + n)
            candidate.genres.append(drama)
            make_movie_file(candidate, "Bluray-1080p")

        metrics = evaluate_user(user_id)

    assert metrics["positives"] == 3
    assert 0.0 <= metrics["mean_percentile"] <= 1.0
    assert 0.0 <= metrics["hit_at_10"] <= 1.0
    assert 0.0 <= metrics["hit_at_25"] <= 1.0


def test_evaluate_applies_trial_class_weights(app):
    """Trial weights must actually steer the leave-one-out ranking (they
    once didn't reach the scorer)."""

    from app.recommendations import evaluate_user

    with app.app_context():
        user_id = admin_id()
        comedy = genre(35, "Comedy")
        drama = genre(18, "Drama")

        # Two 1990s comedies and one 1950s comedy as positives: held out,
        # the 1950s film matches the profile on genre but not decade

        for title, year in (
            ("Trial Comedy A", 1994),
            ("Trial Comedy B", 1995),
            ("Trial Comedy Old", 1953),
        ):
            positive = make_movie(title, year)
            positive.genres.append(comedy)
            make_movie_file(positive, "Bluray-1080p")
            log_watch(user_id, positive, rating=5, liked=True)

        # A 1990s drama candidate: matches on decade but not genre

        candidate = make_movie("Trial Drama", 1990)
        candidate.genres.append(drama)
        make_movie_file(candidate, "Bluray-1080p")

        zeros = {cls: 0.0 for cls in ("director", "actor", "keyword", "language")}
        genre_led = evaluate_user(
            user_id, class_weights={"genre": 1.0, "decade": 0.0, **zeros}
        )
        decade_led = evaluate_user(
            user_id, class_weights={"genre": 0.0, "decade": 1.0, **zeros}
        )

    assert genre_led["mean_percentile"] != decade_led["mean_percentile"]


def test_runtime_filter_trims_the_library_rail(app, admin_client):
    """?minutes=N is a view filter: long films and unknown runtimes drop
    out while it's set, and the default view ignores length entirely."""

    from app import db
    from app.recommendations import RECS_KEY
    from app.models import User

    with app.app_context():
        user_id = User.query.filter_by(admin=True).first().id
        short = make_movie("Filter Short", 1990, tmdb_runtime=95)
        make_movie_file(short, "Bluray-1080p")
        long = make_movie("Filter Long", 1991, tmdb_runtime=200)
        make_movie_file(long, "Bluray-1080p")
        unknown = make_movie("Filter Unknown", 1992)
        make_movie_file(unknown, "Bluray-1080p")
        ids = [short.id, long.id, unknown.id]
        db.session.commit()

    app.redis.set(
        RECS_KEY.format(user_id=user_id),
        json.dumps(
            {
                "computed_at": "2026-08-12 01:45",
                "items": [
                    {"movie_id": movie_id, "score": 1.0, "because": ["Comedy"]}
                    for movie_id in ids
                ],
            }
        ),
    )

    body = admin_client.get("/?minutes=100").get_data(as_text=True)
    assert "Filter Short (1990)" in body
    assert "95 min" in body
    assert "Filter Long" not in body
    assert "Filter Unknown" not in body
    assert "films with unknown runtimes are hidden" in body
    assert ">Clear</a>" in body

    body = admin_client.get("/").get_data(as_text=True)
    assert "Filter Short (1990)" in body
    assert "Filter Long (1991)" in body
    assert "Filter Unknown (1992)" in body
    assert "95 min" not in body


def test_search_results_mark_might_interest(app, admin_client, monkeypatch):
    """Unowned TMDb search matches run the filmography markers' coarse
    scorer, minus the person term: on-profile films badge, off-profile
    films don't, and owned matches never do."""

    import app.main.routes as main_routes

    from app import db
    from app.recommendations import PROFILE_KEY
    from app.models import User

    with app.app_context():
        user_id = User.query.filter_by(admin=True).first().id
        owned = make_movie(
            "Search Marker Owned",
            1955,
            tmdb_id=9103,
        )
        make_movie_file(owned, "Bluray-1080p")
        db.session.commit()

    app.redis.set(
        PROFILE_KEY.format(user_id=user_id),
        json.dumps(
            {
                "affinities": {
                    "genre:80": {
                        "class": "genre",
                        "label": "Crime",
                        "count": 5,
                        "score": 1.0,
                    },
                    "decade:1950": {
                        "class": "decade",
                        "label": "1950s",
                        "count": 5,
                        "score": 0.9,
                    },
                },
                "movies": 5,
            }
        ),
    )

    class FakeTMDb:
        """Canned TMDb response."""

        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            """Never an HTTP error."""

        def json(self):
            """The canned payload."""

            return self.payload

    def fake_tmdb_get(url, params=None, **kwargs):
        """Search results: a strong match, a non-match, an owned match."""

        if url.endswith("/search/movie"):
            return FakeTMDb(
                {
                    "results": [
                        {
                            "id": 9101,
                            "title": "Search Marker Hit",
                            "release_date": "1955-08-01",
                            "genre_ids": [80],
                        },
                        {
                            "id": 9102,
                            "title": "Search Marker Miss",
                            "release_date": "2005-02-14",
                            "genre_ids": [10749],
                        },
                        {
                            "id": 9103,
                            "title": "Search Marker Owned",
                            "release_date": "1955-08-01",
                            "genre_ids": [80],
                        },
                    ]
                }
            )
        return FakeTMDb({"results": []})

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(main_routes, "tmdb_get", fake_tmdb_get)

    page = admin_client.get("/search/tmdb?q=search+marker").get_data(as_text=True)
    assert page.count("Might interest you") == 1
    assert page.index("Search Marker Hit") < page.index("Might interest you")
    assert page.index("Might interest you") < page.index("Search Marker Miss")


def test_rotate_daily_varies_by_day_and_holds_within_one(app):
    """The rotation is deterministic for a given day (reloads are
    stable) but different days sample different subsets, favoring the
    top of the ranking; short lists pass through untouched."""

    from app.recommendations import rotate_daily

    items = list(range(100))

    monday = rotate_daily(items, 18, "recs:1:2026-08-10")
    monday_again = rotate_daily(items, 18, "recs:1:2026-08-10")
    tuesday = rotate_daily(items, 18, "recs:1:2026-08-11")

    assert monday == monday_again
    assert monday != tuesday
    assert len(monday) == 18
    # Rank order is preserved within a day's selection
    assert monday == sorted(monday)
    # The top of the ranking dominates the sample
    assert sum(1 for rank in monday if rank < 30) >= 12

    # A list no longer than the display count passes through whole

    assert rotate_daily([1, 2, 3], 18, "recs:1:2026-08-10") == [1, 2, 3]
