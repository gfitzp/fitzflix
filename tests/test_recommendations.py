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
        hearted = make_movie("Weights Hearted Only", 1993)
        log_watch(user_id, loved, rating=5, liked=True)
        log_watch(user_id, meh, rating=3)
        log_watch(user_id, watched_twice)
        log_watch(user_id, watched_twice)
        log_watch(user_id, hearted, liked=True)

        weights = user_movie_weights(user_id)

    # Mean rating is 4 (imputed ratings never move the mean): the 5
    # centers to +0.4 plus the 1.0 like bonus; the 3 centers to -0.4;
    # two unrated watches are a bare watch plus one rewatch increment;
    # a liked-only row imputes 3 stars (-0.4) plus the like bonus

    assert weights[loved.id] == pytest.approx(1.4)
    assert weights[meh.id] == pytest.approx(-0.4)
    assert weights[watched_twice.id] == pytest.approx(0.55)
    assert weights[hearted.id] == pytest.approx(0.6)


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

        profile, ranked, _ = compute_user_recommendations(user_id)

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

        profile, ranked, _ = compute_user_recommendations(user_id)

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


def test_watch_again_shelf_picks_stale_favorites(app):
    """The rewatch shelf keeps owned films the user liked whose last
    watch is at least the staleness bar ago — date-less rows count as
    the oldest — and drops fresh watches, below-mean films, and films
    without a local file."""

    from datetime import datetime, timedelta

    from app import db
    from app.models import UserMovieReview
    from app.recommendations import watch_again_shelf
    from app.videos import star_rating_fields

    with app.app_context():
        user_id = admin_id()

        def diary_row(movie, rating=None, liked=False, watched=None):
            """One viewing row with an explicit watch date."""

            db.session.add(
                UserMovieReview(
                    user_id=user_id,
                    movie_id=movie.id,
                    liked=liked,
                    date_watched=watched,
                    **star_rating_fields(rating),
                )
            )

        stale_liked = make_movie("Again Stale Liked", 1980)
        make_movie_file(stale_liked, "Bluray-1080p")
        diary_row(
            stale_liked, liked=True, watched=datetime.now() - timedelta(days=1200)
        )

        fresh_liked = make_movie("Again Fresh Liked", 1981)
        make_movie_file(fresh_liked, "Bluray-1080p")
        diary_row(fresh_liked, liked=True, watched=datetime.now() - timedelta(days=180))

        dateless = make_movie("Again Dateless Favorite", 1982)
        make_movie_file(dateless, "Bluray-1080p")
        diary_row(dateless, rating=5)

        meh = make_movie("Again Meh", 1983)
        make_movie_file(meh, "Bluray-1080p")
        diary_row(meh, rating=2, watched=datetime.now() - timedelta(days=1200))

        unowned = make_movie("Again Unowned Liked", 1984)
        diary_row(unowned, liked=True, watched=datetime.now() - timedelta(days=1200))

        db.session.commit()

        items = watch_again_shelf(user_id)
        ids = [item["movie_id"] for item in items]

        # Under the liked-only-imputes-3-stars rule the dateless
        # 5-star favorite now outranks the stale bare like (its centered
        # rating beats the imputed 3's), and nothing else qualifies

        assert ids == [dateless.id, stale_liked.id]
        assert items[0]["last_watched"] is None
        assert items[1]["last_watched"] is not None


def test_index_watch_again_shelf_renders_and_pins(app, admin_client):
    """The landing page's rewatch shelf shows stale favorites with
    last-watched badges, watchlisted ones pinned first."""

    from datetime import datetime, timedelta

    from app import db
    from app.models import UserMovieReview, UserWatchlist
    from app.videos import star_rating_fields

    with app.app_context():
        user_id = admin_id()
        favorite = make_movie("Shelf Old Favorite", 1975)
        make_movie_file(favorite, "Bluray-1080p")
        db.session.add(
            UserMovieReview(
                user_id=user_id,
                movie_id=favorite.id,
                liked=True,
                date_watched=datetime.now() - timedelta(days=1500),
                **star_rating_fields(4.0),
            )
        )

        # Seen, liked, and re-watchlisted: declared rewatch intent

        wanted_again = make_movie("Shelf Wanted Again", 1976)
        make_movie_file(wanted_again, "Bluray-1080p")
        db.session.add(
            UserMovieReview(
                user_id=user_id,
                movie_id=wanted_again.id,
                liked=True,
                date_watched=None,
                **star_rating_fields(5.0),
            )
        )
        db.session.add(UserWatchlist(user_id=user_id, movie_id=wanted_again.id))
        db.session.commit()
        old_year = (datetime.now() - timedelta(days=1500)).strftime("%Y")

    body = admin_client.get("/").get_data(as_text=True)
    assert "Watch it again" in body
    assert "Shelf Old Favorite (1975)" in body
    assert f"Last watched {old_year}" in body
    assert "Shelf Wanted Again (1976)" in body
    assert "Seen ages ago" in body
    assert "haven't watched in at least two years" in body

    # The re-watchlisted film holds a badged slot (positions vary
    # daily since the shuffle)

    assert "On your watchlist" in body


def test_copref_value_math(app):
    """The co-preference term is a weighted average of anchor sentiment
    over the K most similar neighbors, honoring the exclusion used for
    leave-one-out purity."""

    from app.recommendations import COPREF_WEIGHT, _copref_value

    entries = [(0.5, 101, 2.0), (0.3, 102, 1.0), (0.1, 103, -1.0)]
    expected = COPREF_WEIGHT * (0.5 * 2.0 + 0.3 * 1.0 + 0.1 * -1.0) / 0.9
    assert _copref_value(entries) == pytest.approx(expected)

    # Excluding the strongest anchor removes it from the average

    excluded = COPREF_WEIGHT * (0.3 * 1.0 + 0.1 * -1.0) / 0.4
    assert _copref_value(entries, excluded=101) == pytest.approx(excluded)
    assert _copref_value([]) == 0.0


def test_copref_reranks_and_explains(app):
    """Between two candidates the profile scores identically, the one
    co-preferred with a liked film ranks first and says why."""

    from app import db
    from app.models import MovieCopref
    from app.recommendations import compute_user_recommendations

    with app.app_context():
        user_id = admin_id()
        comedy = genre(35, "Comedy")

        liked = make_movie("Copref Liked", 1990, tmdb_id=701)
        liked.genres.append(comedy)
        log_watch(user_id, liked, liked=True)

        similar = make_movie("Copref Similar", 1991, tmdb_id=702)
        similar.genres.append(comedy)
        make_movie_file(similar, "Bluray-1080p")

        plain = make_movie("Copref Plain", 1992, tmdb_id=703)
        plain.genres.append(comedy)
        make_movie_file(plain, "Bluray-1080p")

        db.session.add_all(
            [
                MovieCopref(tmdb_id_a=701, tmdb_id_b=702, similarity=0.4),
                MovieCopref(tmdb_id_a=702, tmdb_id_b=701, similarity=0.4),
            ]
        )
        db.session.commit()

        _, ranked, _ = compute_user_recommendations(user_id)
        ranked_ids = [rec["movie_id"] for rec in ranked]

        assert ranked_ids == [similar.id, plain.id]
        because = ranked[0]["because"]
        assert "liked by people who liked Copref Liked" in because
        assert all("liked by people" not in chip for chip in ranked[1]["because"])


def test_evaluate_user_measures_copref(app):
    """With mutually co-preferred positives the held-out film outranks
    taste-equal candidates, so the metrics improve over the bare
    profile — the shipped term is what the harness measures."""

    from app import db
    from app.models import MovieCopref
    from app.recommendations import evaluate_user

    with app.app_context():
        user_id = admin_id()
        comedy = genre(35, "Comedy")

        positive_tmdb = (711, 712, 713)
        for n, tmdb_id in enumerate(positive_tmdb):
            movie = make_movie(f"Copref Positive {n}", 1990, tmdb_id=tmdb_id)
            movie.genres.append(comedy)
            make_movie_file(movie, "Bluray-1080p")
            log_watch(user_id, movie, liked=True)

        # A liked trainer whose star also carries the fillers: on taste
        # alone the fillers strictly outrank a held-out positive, so
        # the bare metrics have room to improve

        star = make_person(888100, "Copref Star")
        trainer = make_movie("Copref Trainer", 1990, tmdb_id=719)
        trainer.genres.append(comedy)
        log_watch(user_id, trainer, liked=True)
        make_cast(star, trainer)
        for n in range(5):
            movie = make_movie(f"Copref Filler {n}", 1990, tmdb_id=720 + n)
            movie.genres.append(comedy)
            make_movie_file(movie, "Bluray-1080p")
            make_cast(star, movie)
        db.session.commit()

        bare = evaluate_user(user_id)

        for a in positive_tmdb:
            for b in positive_tmdb:
                if a != b:
                    db.session.add(
                        MovieCopref(tmdb_id_a=a, tmdb_id_b=b, similarity=0.5)
                    )
        db.session.commit()

        with_copref = evaluate_user(user_id)

    assert with_copref["mean_percentile"] < bare["mean_percentile"]
    assert with_copref["hit_at_10"] == 1.0


def test_not_interested_excludes_and_weighs(app):
    """A waved-off film leaves the candidate pool and weighs mildly
    negative in the profile — but never on top of a real diary verdict,
    which already carries the sentiment (#45b)."""

    from app import db
    from app.models import UserMovieStatus
    from app.recommendations import (
        NOT_INTERESTED_WEIGHT,
        local_candidates,
        user_movie_weights,
    )

    with app.app_context():
        user_id = admin_id()
        refused = make_movie("Refused Film", 1990)
        make_movie_file(refused, "Bluray-1080p")
        kept = make_movie("Kept Film", 1991)
        make_movie_file(kept, "Bluray-1080p")
        rated_then_refused = make_movie("Rated Then Refused", 1992)
        log_watch(user_id, rated_then_refused, rating=2)
        db.session.add_all(
            [
                UserMovieStatus(
                    user_id=user_id, movie_id=refused.id, kind="not_interested"
                ),
                UserMovieStatus(
                    user_id=user_id,
                    movie_id=rated_then_refused.id,
                    kind="not_interested",
                ),
            ]
        )
        db.session.commit()

        assert local_candidates(user_id) == [kept.id]

        weights = user_movie_weights(user_id)
        assert weights[refused.id] == NOT_INTERESTED_WEIGHT

        # The rated film keeps its diary-derived weight, no stacking

        assert weights[rated_then_refused.id] != NOT_INTERESTED_WEIGHT


def test_estimated_rating_quantile_math(app):
    """The calibration curve reads a score's position among the user's
    own films out at the same position in their sorted ratings —
    half-star rounded, clamped, and absent without a curve."""

    from app.recommendations import estimated_rating

    profile = {
        "calibration": {
            "scores": [0.0, 1.0, 2.0, 3.0],
            "stars": [1.0, 2.0, 4.0, 5.0],
        }
    }

    # Above every known score: the top of the rating distribution;
    # below every known score: the bottom

    assert estimated_rating(profile, 9.0) == 5.0
    assert estimated_rating(profile, -1.0) == 1.0

    # Midway up the scores reads midway up the stars: 2.0 sits at the
    # 0.625 position, interpolating to 3.75 stars, rounded to 4.0

    assert estimated_rating(profile, 2.0) == 4.0
    assert estimated_rating(profile, 0.5) == 2.0

    # No curve, no estimate

    assert estimated_rating({"calibration": None}, 1.0) is None
    assert estimated_rating(None, 1.0) is None


def test_compute_stores_calibration_curve(app, monkeypatch):
    """The nightly compute attaches the score→stars curve to the
    profile once enough rated films exist, sorted and LOO-derived."""

    import app.recommendations as recommendations

    from app import db

    monkeypatch.setattr(recommendations, "CALIBRATION_MIN_RATED", 3)

    with app.app_context():
        user_id = admin_id()
        comedy = genre(35, "Comedy")
        for n, rating in enumerate((2.0, 3.5, 5.0)):
            movie = make_movie(f"Calib Rated {n}", 1990 + n)
            movie.genres.append(comedy)
            log_watch(user_id, movie, rating=rating, liked=rating >= 3)
        candidate = make_movie("Calib Candidate", 1995)
        candidate.genres.append(comedy)
        make_movie_file(candidate, "Bluray-1080p")
        db.session.commit()

        profile, ranked, _ = recommendations.compute_user_recommendations(user_id)

    curve = profile["calibration"]
    assert curve is not None
    assert curve["stars"] == [2.0, 3.5, 5.0]
    assert len(curve["scores"]) == 3
    assert curve["scores"] == sorted(curve["scores"])


def test_movie_page_shows_estimated_rating(app, admin_client):
    """An unlogged film with a stored score shows the engine's guess as
    paler "estimated" stars in the widget (#58); logging it replaces
    the estimate with the real filled verdict."""

    import json as jsonlib

    from app import db
    from app.recommendations import PROFILE_KEY, SCORES_KEY
    from app.videos import star_rating_fields
    from app.models import UserMovieReview

    with app.app_context():
        user_id = admin_id()
        pick = make_movie("Estimate Pick", 1995)
        make_movie_file(pick, "Bluray-1080p")
        db.session.commit()
        pick_id = pick.id

    app.redis.set(
        SCORES_KEY.format(user_id=user_id), jsonlib.dumps({str(pick_id): 9.0})
    )
    app.redis.set(
        PROFILE_KEY.format(user_id=user_id),
        jsonlib.dumps(
            {
                "affinities": {},
                "movies": 3,
                "calibration": {
                    "scores": [0.0, 1.0, 2.0, 3.0],
                    "stars": [1.0, 2.0, 4.0, 4.5],
                },
            }
        ),
    )

    # Score 9.0 estimates 4.5 stars: four paler "estimated" glyphs and
    # the hint title, no filled ones

    page = admin_client.get(f"/movie/{pick_id}").get_data(as_text=True)
    assert page.count("star estimated") == 4
    assert "star filled" not in page
    assert "Estimated for you" in page

    with app.app_context():
        db.session.add(
            UserMovieReview(
                user_id=user_id,
                movie_id=pick_id,
                liked=True,
                **star_rating_fields(4.0),
            )
        )
        db.session.commit()

    page = admin_client.get(f"/movie/{pick_id}").get_data(as_text=True)
    assert "star estimated" not in page
    assert page.count("star filled") == 4


def test_single_movie_score_matches_the_stored_recipe(app):
    """A film scored live carries exactly the stored ranking's recipe —
    taste + co-preference + award prior — so its estimate reads off the
    same calibration curve as a stored film's."""

    from datetime import datetime

    from app import db
    from app.models import MovieAward, MovieCopref
    from app.recommendations import compute_user_recommendations, single_movie_score

    with app.app_context():
        user_id = admin_id()
        comedy = genre(35, "Comedy")

        liked = make_movie("Recipe Liked", 1990, tmdb_id=711)
        liked.genres.append(comedy)
        log_watch(user_id, liked, rating=5.0, liked=True)
        second = make_movie("Recipe Second", 1991, tmdb_id=712)
        second.genres.append(comedy)
        log_watch(user_id, second, rating=2.0)

        pick = make_movie(
            "Recipe Pick", 1992, tmdb_id=713, tmdb_data_as_of=datetime.utcnow()
        )
        pick.genres.append(comedy)
        make_movie_file(pick, "Bluray-1080p")
        db.session.add(
            MovieAward(
                movie_id=pick.id, award_id="Q1", award_name="Big Prize", win=True
            )
        )
        db.session.add_all(
            [
                MovieCopref(tmdb_id_a=711, tmdb_id_b=713, similarity=0.4),
                MovieCopref(tmdb_id_a=713, tmdb_id_b=711, similarity=0.4),
            ]
        )
        db.session.commit()

        profile, ranked, _ = compute_user_recommendations(user_id)
        item = next(rec for rec in ranked if rec["movie_id"] == pick.id)
        live = single_movie_score(user_id, pick, profile)
        assert live == pytest.approx(item["score"], abs=1e-4)

        # A record whose TMDb data hasn't landed can't be scored — its
        # near-empty feature list would read as a taste mismatch

        bare = make_movie("Recipe Bare", 1993, tmdb_id=714)
        db.session.flush()
        assert single_movie_score(user_id, bare, profile) is None
        assert single_movie_score(user_id, pick, None) is None


def test_movie_page_estimates_films_outside_the_stored_ranking(app, admin_client):
    """An unowned refreshed record missing from the stored ranking is
    scored live at render, so a LOW guess can warn off a watchlist add;
    a record still waiting on its TMDb refresh shows no guess."""

    import json as jsonlib
    from datetime import datetime

    from app import db
    from app.recommendations import PROFILE_KEY, RECS_KEY

    with app.app_context():
        user_id = admin_id()
        outsider = make_movie(
            "Estimate Outsider", 1994, tmdb_id=721, tmdb_data_as_of=datetime.utcnow()
        )
        raw = make_movie("Estimate Raw", 1995, tmdb_id=722)
        db.session.commit()
        outsider_id, raw_id = outsider.id, raw.id

    app.redis.set(
        RECS_KEY.format(user_id=user_id),
        jsonlib.dumps({"computed_at": "2026-08-13 01:45", "items": []}),
    )
    app.redis.set(
        PROFILE_KEY.format(user_id=user_id),
        jsonlib.dumps(
            {
                "affinities": {},
                "movies": 3,
                "calibration": {
                    "scores": [0.5, 1.0, 2.0, 3.0],
                    "stars": [1.0, 2.0, 4.0, 4.5],
                },
            }
        ),
    )

    # No affinities, no copref neighbors: the live score is 0.0, which
    # sits below the whole curve and reads out at its lowest star —
    # one paler "estimated" glyph in the widget

    page = admin_client.get(f"/movie/{outsider_id}").get_data(as_text=True)
    assert page.count("star estimated") == 1
    assert "Estimated for you" in page

    page = admin_client.get(f"/movie/{raw_id}").get_data(as_text=True)
    assert "star estimated" not in page


def test_shuffle_daily_is_deterministic_per_seed(app):
    """The day's cards shuffle to a stable arrangement per seed — a
    permutation, identical on reload, different on another day."""

    from app.recommendations import shuffle_daily

    items = list(range(12))
    today = shuffle_daily(items, "mix:recs:1:2026-08-12")
    assert sorted(today) == items
    assert today != items
    assert shuffle_daily(items, "mix:recs:1:2026-08-12") == today
    assert shuffle_daily(items, "mix:recs:1:2026-08-13") != today


def test_stored_cut_deepens_by_watchlisted_candidates(app):
    """The stored ranking keeps `limit` films beyond the watchlisted
    candidates: the pin lane's render-time exclusion must never thin
    the discovery pool below its monthly no-repeat cycle, however big
    the (deliberately uncapped) watchlist grows."""

    from app import db
    from app.models import UserWatchlist
    from app.recommendations import compute_user_recommendations

    with app.app_context():
        user_id = admin_id()
        comedy = genre(35, "Comedy")

        liked = make_movie("Depth Liked", 1990)
        liked.genres.append(comedy)
        log_watch(user_id, liked, liked=True)

        for n in range(5):
            movie = make_movie(f"Depth Candidate {n}", 1991 + n)
            movie.genres.append(comedy)
            make_movie_file(movie, "Bluray-1080p")
            if n == 0:
                db.session.add(UserWatchlist(user_id=user_id, movie_id=movie.id))
        db.session.commit()

        _, ranked, _ = compute_user_recommendations(user_id, limit=2)

    # Five positive candidates; the cut is limit 2 + 1 watchlisted

    assert len(ranked) == 3


def test_rotate_partition_cycles_the_whole_set_without_repeats(app):
    """One film per quality tier per day, no repeats until the whole
    set has shown, every day quality-mixed, deterministic per day."""

    from app.recommendations import rotate_partition

    items = list(range(372))  # 12 tiers x 31 films

    days = [rotate_partition(items, 12, day) for day in range(31)]

    # Every day serves 12, deterministically

    assert all(len(day) == 12 for day in days)
    assert rotate_partition(items, 12, 5) == days[5]

    # Each day draws one film from each quality tier (tier size 31)

    assert [rank // 31 for rank in days[0]] == list(range(12))

    # A full cycle shows every film exactly once — no repeats

    shown = [rank for day in days for rank in day]
    assert len(shown) == len(set(shown)) == 372

    # Day 32 wraps back to day 1's picks; short lists pass through

    assert rotate_partition(items, 12, 31) == days[0]
    assert rotate_partition([1, 2, 3], 12, 7) == [1, 2, 3]

    # Pools that don't divide evenly still serve a full row daily

    ragged = list(range(100))
    assert all(len(rotate_partition(ragged, 12, day)) == 12 for day in range(20))

    # Even barely-bigger-than-count pools fill every slot (a ceil-based
    # tier size used to leave trailing tiers empty: 12 items in 8 slots
    # returned only 6)

    assert all(len(rotate_partition(list(range(12)), 8, day)) == 8 for day in range(6))


def test_marker_bar_rides_with_the_profile(app):
    """The nightly recompute stores the baseline-percentile marker bar
    on the profile, computed from the user's own candidates."""

    from app import db
    from app.recommendations import PROFILE_KEY, recompute_recommendations

    with app.app_context():
        user_id = admin_id()
        comedy = genre(35, "Comedy")
        liked = make_movie("Bar Liked", 1994)
        liked.genres.append(comedy)
        log_watch(user_id, liked, liked=True)
        candidate = make_movie("Bar Candidate", 1995)
        candidate.genres.append(comedy)
        make_movie_file(candidate, "Bluray-1080p")
        db.session.commit()

    assert recompute_recommendations() is True
    profile = json.loads(app.redis.get(PROFILE_KEY.format(user_id=user_id)))
    assert profile["marker_bar"] > 0


def test_search_markers_respect_the_stored_bar(app, admin_client, monkeypatch):
    """A stored marker bar gates the badge: a film must beat the user's
    own baseline percentile, not just match a liked genre."""

    import app.main.routes as main_routes

    from app.recommendations import PROFILE_KEY
    from app.models import User

    with app.app_context():
        user_id = User.query.filter_by(admin=True).first().id

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
                "marker_bar": 0.7,
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
        """One film over the bar, one merely genre-matched."""

        if url.endswith("/search/movie"):
            return FakeTMDb(
                {
                    "results": [
                        {
                            # Crime + 1950s: 0.5 + 0.27 = 0.77 > 0.7
                            "id": 9201,
                            "title": "Bar Clearing Hit",
                            "release_date": "1955-08-01",
                            "genre_ids": [80],
                        },
                        {
                            # Crime alone: 0.5 < 0.7 — a liked genre is
                            # no longer enough
                            "id": 9202,
                            "title": "Bar Missing Match",
                            "release_date": "2005-02-14",
                            "genre_ids": [80],
                        },
                    ]
                }
            )
        return FakeTMDb({"results": []})

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(main_routes, "tmdb_get", fake_tmdb_get)

    page = admin_client.get("/search/tmdb?q=bar").get_data(as_text=True)
    assert page.count("Might interest you") == 1
    assert page.index("Bar Clearing Hit") < page.index("Might interest you")
    assert page.index("Might interest you") < page.index("Bar Missing Match")
