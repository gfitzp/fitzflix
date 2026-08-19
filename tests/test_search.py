"""Global search: the /search results tiers (owned, seen-but-unowned, not
found), the /search.json type-ahead endpoint, and the /search/tmdb lookup
with in-library annotations.
"""

from app import db
from app.models import MovieCast, TMDBCredit, User, UserMovieReview

from tests.factories import (
    make_movie,
    make_movie_file,
    make_tv_file,
    make_tv_series,
)


def make_person(person_id, name, movies, character="Self"):
    """A TMDBCredit with a cast row in each of the given movies."""

    person = TMDBCredit(id=person_id, name=name)
    db.session.add(person)
    db.session.flush()
    for order, movie in enumerate(movies):
        db.session.add(
            MovieCast(
                movie_id=movie.id,
                credit_id=person.id,
                character=character,
                billing_order=order,
            )
        )
    return person


def build_library(app):
    """A small library: an upgradable movie, a topped-out movie, a reviewed
    movie with no files (which the library search must omit), and a TV
    series with one episode."""

    dvd_movie = make_movie(
        "Jaws", 1975, tmdb_overview="A giant shark terrorizes a beach town."
    )
    make_movie_file(dvd_movie, "DVD")

    remux_movie = make_movie("Jurassic Park", 1993)
    make_movie_file(remux_movie, "Bluray-2160p Remux")

    seen_movie = make_movie("Jacob's Ladder", 1990)
    admin = User.query.filter_by(admin=True).first()
    db.session.add(
        UserMovieReview(
            user_id=admin.id,
            movie_id=seen_movie.id,
            rating=8,
            modified_rating=8,
            whole_stars=4,
            half_stars=0,
        )
    )

    series = make_tv_series("Jeopardy (1984)")
    make_tv_file(series, 1, 1, "SDTV")

    db.session.commit()
    return dvd_movie, remux_movie, seen_movie, series


def test_search_requires_login(app):
    response = app.test_client().get("/search?q=jaws")
    assert response.status_code == 302


def test_search_tiers_owned_upgradable_and_topped_out(app, admin_client):
    with app.app_context():
        build_library(app)

    # Upgradability is conveyed by badge color alone: amber for an
    # upgrade candidate, green for a topped-out copy

    page = admin_client.get("/search?q=jaws").get_data(as_text=True)
    assert "Jaws (1975)" in page
    assert 'text-bg-warning">DVD' in page

    # The synopsis lives in the poster popover now (#45d); the tile
    # is armed with data-card-url

    assert "A giant shark terrorizes a beach town." not in page
    assert "data-card-url" in page

    page = admin_client.get("/search?q=jurassic").get_data(as_text=True)
    assert "Jurassic Park (1993)" in page
    assert 'text-bg-success">Bluray-2160p Remux' in page
    # The chip-color map in base.html's poll script mentions the
    # class on every page, so assert on rendered badges only
    assert 'text-bg-warning">' not in page


def test_search_omits_reviewed_movies_without_files(app, admin_client):
    """A review-only record (a logged unowned film) stays out of the
    library search — it belongs to the TMDb search instead."""

    with app.app_context():
        build_library(app)

    page = admin_client.get("/search?q=jacob").get_data(as_text=True)
    assert "Jacob" not in page
    assert "No copy in library" not in page


def test_search_badges_recommended_owned_films(app, admin_client):
    """An owned film the nightly recompute ranked in the stored
    recommendations badges "Might interest you" on the library search —
    the rail and the search pages agree on what's recommended. Unranked
    films and films logged since the run don't badge."""

    import json

    from app.recommendations import RECS_KEY

    with app.app_context():
        recommended = make_movie("Jaws", 1975)
        make_movie_file(recommended, "DVD")
        logged_since = make_movie("Jaws 2", 1978)
        make_movie_file(logged_since, "DVD")
        unranked = make_movie("Jaws 3-D", 1983)
        make_movie_file(unranked, "DVD")
        admin = User.query.filter_by(admin=True).first()
        db.session.add(
            UserMovieReview(
                user_id=admin.id,
                movie_id=logged_since.id,
                rating=6,
                modified_rating=6,
                whole_stars=3,
                half_stars=0,
            )
        )
        db.session.commit()
        user_id = admin.id
        rec_ids = [recommended.id, logged_since.id]

    app.redis.set(
        RECS_KEY.format(user_id=user_id),
        json.dumps(
            {
                "computed_at": "2026-08-12 01:45",
                "items": [
                    {"movie_id": movie_id, "score": 1.0, "because": []}
                    for movie_id in rec_ids
                ],
            }
        ),
    )

    page = admin_client.get("/search?q=jaws").get_data(as_text=True)
    assert page.count("Might interest you") == 1
    assert (
        page.index("Jaws (1975)")
        < page.index("Might interest you")
        < page.index("Jaws 2 (1978)")
    )


def test_search_funnel_badges_coexist_and_exclude(app, admin_client):
    """The funnel on the library search: watchlist coexists with
    might-interest while unseen, and with Seen after logging — but a
    seen film never badges might-interest, even when it's still in the
    stored recommendations."""

    import json

    from app.models import UserWatchlist
    from app.recommendations import RECS_KEY

    with app.app_context():
        fresh = make_movie("Funnel Fresh Pick", 1990)
        make_movie_file(fresh, "DVD")
        seen = make_movie("Funnel Seen Rewatch", 1991)
        make_movie_file(seen, "DVD")
        admin = User.query.filter_by(admin=True).first()
        db.session.add(
            UserMovieReview(
                user_id=admin.id,
                movie_id=seen.id,
                rating=9,
                modified_rating=9,
                whole_stars=4,
                half_stars=1,
            )
        )
        db.session.add(UserWatchlist(user_id=admin.id, movie_id=fresh.id))
        db.session.add(UserWatchlist(user_id=admin.id, movie_id=seen.id))
        db.session.commit()
        user_id = admin.id
        rec_ids = [fresh.id, seen.id]

    app.redis.set(
        RECS_KEY.format(user_id=user_id),
        json.dumps(
            {
                "computed_at": "2026-08-12 01:45",
                "items": [
                    {"movie_id": movie_id, "score": 1.0, "because": []}
                    for movie_id in rec_ids
                ],
            }
        ),
    )

    page = admin_client.get("/search?q=funnel").get_data(as_text=True)
    assert page.count("On your watchlist") == 2
    assert page.count("Might interest you") == 1
    assert "Seen &mdash; rated 9" in page
    assert page.index("Might interest you") < page.index("Funnel Seen Rewatch (1991)")


def test_search_tmdb_funnel_badges(app, admin_client, monkeypatch):
    """The funnel on TMDb results: a seen film badges Seen and never
    might-interest (even review-only records, whose watch already feeds
    the profile); a watchlisted unowned record badges the watchlist."""

    import json

    from app.models import UserWatchlist
    from app.recommendations import RECS_KEY

    with app.app_context():
        owned_seen = make_movie("Funnel Owned Seen", 1975, tmdb_id=678)
        make_movie_file(owned_seen, "DVD")
        wanted = make_movie("Funnel Wanted", 1978, tmdb_id=679)
        admin = User.query.filter_by(admin=True).first()
        db.session.add(
            UserMovieReview(
                user_id=admin.id,
                movie_id=owned_seen.id,
                rating=7,
                modified_rating=7,
                whole_stars=3,
                half_stars=1,
            )
        )
        db.session.add(UserWatchlist(user_id=admin.id, movie_id=wanted.id))
        db.session.commit()
        user_id = admin.id
        owned_seen_id = owned_seen.id

    # The seen film sits in the stored recommendations, proving the
    # suppression is the diary, not absence from the set

    app.redis.set(
        RECS_KEY.format(user_id=user_id),
        json.dumps(
            {
                "computed_at": "2026-08-12 01:45",
                "items": [{"movie_id": owned_seen_id, "score": 1.0, "because": []}],
            }
        ),
    )

    import app.main.search as search

    class FakeResponse:
        def __init__(self, results):
            self._results = results

        def raise_for_status(self):
            pass

        def json(self):
            return {"results": self._results}

    def fake_get(url, params=None, timeout=None):
        if url.endswith("/search/movie"):
            return FakeResponse(
                [
                    {
                        "id": 678,
                        "title": "Funnel Owned Seen",
                        "release_date": "1975-06-20",
                    },
                    {"id": 679, "title": "Funnel Wanted", "release_date": "1978-06-16"},
                ]
            )
        return FakeResponse([])

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(search, "tmdb_get", fake_get)

    page = admin_client.get("/search/tmdb?q=funnel").get_data(as_text=True)
    assert page.count("Might interest you") == 0
    assert page.count('text-bg-info me-1">Seen') == 1
    assert page.count("On your watchlist") == 1
    assert page.index("Funnel Wanted (1978)") < page.index("On your watchlist")


def test_search_tmdb_badges_recommended_owned_films(app, admin_client, monkeypatch):
    """An owned TMDb match that sits in the stored recommendations
    carries the might-interest badge next to its library badge."""

    import json

    from app.recommendations import RECS_KEY

    with app.app_context():
        recommended = make_movie("Jaws", 1975, tmdb_id=578)
        make_movie_file(recommended, "DVD")
        unranked = make_movie("Jaws 2", 1978, tmdb_id=579)
        make_movie_file(unranked, "DVD")
        db.session.commit()
        admin = User.query.filter_by(admin=True).first()
        user_id = admin.id
        recommended_id = recommended.id

    app.redis.set(
        RECS_KEY.format(user_id=user_id),
        json.dumps(
            {
                "computed_at": "2026-08-12 01:45",
                "items": [{"movie_id": recommended_id, "score": 1.0, "because": []}],
            }
        ),
    )

    import app.main.search as search

    class FakeResponse:
        def __init__(self, results):
            self._results = results

        def raise_for_status(self):
            pass

        def json(self):
            return {"results": self._results}

    def fake_get(url, params=None, timeout=None):
        if url.endswith("/search/movie"):
            return FakeResponse(
                [
                    {"id": 578, "title": "Jaws", "release_date": "1975-06-20"},
                    {"id": 579, "title": "Jaws 2", "release_date": "1978-06-16"},
                ]
            )
        return FakeResponse([])

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(search, "tmdb_get", fake_get)

    page = admin_client.get("/search/tmdb?q=jaws").get_data(as_text=True)
    assert page.count("Might interest you") == 1
    assert (
        page.index("Jaws (1975)")
        < page.index("Might interest you")
        < page.index("Jaws 2 (1978)")
    )


def test_search_finds_tv_series(app, admin_client):
    with app.app_context():
        build_library(app)

    page = admin_client.get("/search?q=jeopardy").get_data(as_text=True)
    assert "Jeopardy (1984)" in page
    assert "Season 1: SDTV" in page


def test_tv_seasons_summarized_by_worst_rank_one_quality(app, admin_client):
    """Each season badge shows the worst quality among that season's best
    episode copies: a single Unknown episode drags its season to Unknown
    even when another episode is Bluray, while an outranked DVD copy of an
    episode that also has a Bluray copy doesn't count at all."""

    with app.app_context():
        series = make_tv_series("Mixed Bag (2020)")

        # Season 1: two episodes, one Unknown and one Bluray — the season
        # is only as good as its weakest episode

        make_tv_file(series, 1, 1, "Unknown")
        make_tv_file(series, 1, 2, "Bluray-1080p")

        # Season 2: one episode with two copies; the DVD copy is outranked
        # by the Bluray copy, so it doesn't drag the season down

        make_tv_file(series, 2, 1, "DVD")
        make_tv_file(series, 2, 1, "Bluray-1080p")
        db.session.commit()

    page = admin_client.get("/search?q=mixed+bag").get_data(as_text=True)
    assert 'text-bg-warning" title="2 episodes">Season 1: Unknown' in page
    assert 'text-bg-success" title="1 episode">Season 2: Bluray-1080p' in page


def test_physical_media_seasons_are_not_upgrade_candidates(app, admin_client):
    """Seasons whose worst copy came from physical media (DVD, SD/720p
    Blu-ray) show green: they're often the only release that will ever
    exist. Non-physical qualities below the threshold stay amber."""

    with app.app_context():
        series = make_tv_series("Disc Only (1995)")
        make_tv_file(series, 1, 1, "DVD")
        make_tv_file(series, 2, 1, "Bluray-480p")
        make_tv_file(series, 3, 1, "WEBDL-480p")
        db.session.commit()

    page = admin_client.get("/search?q=disc+only").get_data(as_text=True)
    assert 'text-bg-success" title="1 episode">Season 1: DVD' in page
    assert 'text-bg-success" title="1 episode">Season 2: Bluray-480p' in page
    assert 'text-bg-warning" title="1 episode">Season 3: WEBDL-480p' in page

    # The TV library page uses the same flag on its season badges

    page = admin_client.get("/library/tv").get_data(as_text=True)
    assert 'text-bg-success">DVD' in page
    assert 'text-bg-success">Bluray-480p' in page
    assert 'text-bg-warning">WEBDL-480p' in page


def test_search_wildcard_ignores_word_gaps(app, admin_client):
    with app.app_context():
        movie = make_movie("The Three Amigos", 1986)
        make_movie_file(movie, "DVD")
        db.session.commit()

    page = admin_client.get("/search?q=three+amigos").get_data(as_text=True)
    assert "The Three Amigos" in page


def test_search_empty_state_offers_tmdb_lookup(app, admin_client):
    with app.app_context():
        build_library(app)

    page = admin_client.get("/search?q=zzzzzz").get_data(as_text=True)
    assert "Nothing in the library matches" in page
    assert "/search/tmdb?q=zzzzzz" in page


def test_search_json_type_ahead(app, admin_client):
    with app.app_context():
        build_library(app)

    data = admin_client.get("/search.json?q=jaws").get_json()
    assert data["results"]
    movie_hit = data["results"][0]
    assert movie_hit["type"] == "Movie"
    assert movie_hit["title"] == "Jaws (1975)"
    assert movie_hit["detail"] == "DVD"
    assert movie_hit["url"].startswith("/movie/")

    data = admin_client.get("/search.json?q=jeopardy").get_json()
    assert data["results"][0]["type"] == "TV"
    assert data["results"][0]["detail"] == "1 season, worst SDTV"
    assert data["results"][0]["url"].startswith("/tv/")

    # Single characters don't trigger suggestions

    assert admin_client.get("/search.json?q=j").get_json() == {"results": []}


def test_search_tmdb_annotates_library_membership(app, admin_client, monkeypatch):
    """TMDb results the library already has link to their pages; the rest
    are explicitly marked not in the library."""

    with app.app_context():
        owned = make_movie("Jaws", 1975, tmdb_id=578)
        make_movie_file(owned, "DVD")

        # A review-only record: the film was logged but no file exists,
        # so it must not badge as in-library

        make_movie("Jaws 2", 1978, tmdb_id=579)
        db.session.commit()
        owned_id = owned.id

    import app.main.search as search

    class FakeResponse:
        def __init__(self, results):
            self._results = results

        def raise_for_status(self):
            pass

        def json(self):
            return {"results": self._results}

    def fake_get(url, params=None, timeout=None):
        if url.endswith("/search/movie"):
            return FakeResponse(
                [
                    {
                        "id": 578,
                        "title": "Jaws",
                        "release_date": "1975-06-20",
                        "overview": "A giant shark terrorizes a beach town.",
                    },
                    {
                        "id": 579,
                        "title": "Jaws 2",
                        "release_date": "1978-06-16",
                        "overview": "The shark is back.",
                        "poster_path": "/jaws2.jpg",
                    },
                ]
            )
        return FakeResponse([])

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(search, "tmdb_get", fake_get)

    page = admin_client.get("/search/tmdb?q=jaws").get_data(as_text=True)
    assert "Jaws (1975)" in page
    assert f"/movie/{owned_id}" in page
    assert "Jaws 2 (1978)" in page

    # Only the match with a local file carries the Fitzflix badge — the
    # review-only Jaws 2 record doesn't count as in-library, and its row
    # still leads to the log page (which redirects to its movie page)

    assert page.count('title="In your Fitzflix library"') == 1
    assert "Not in library" not in page
    assert "/review/tmdb/579" in page

    # Rows render like the local search: a poster thumbnail, and the
    # whole unowned row links to the log page

    assert "/w185/jaws2.jpg" in page
    assert "/review/tmdb/579" in page


def test_results_pages_carry_prefilled_search_boxes(app, admin_client):
    """Both results pages re-offer the search box pre-filled, so a
    fruitless query can be reworked in place instead of round-tripping
    through the navbar; the TMDb page's box submits back to TMDb."""

    page = admin_client.get("/search?q=jaws").get_data(as_text=True)
    assert 'value="jaws"' in page

    page = admin_client.get("/search/tmdb?q=jaws").get_data(as_text=True)
    assert 'action="/search/tmdb"' in page
    assert 'value="jaws"' in page
    assert 'placeholder="Search TMDb"' in page


def test_search_tmdb_without_api_key_explains(app, admin_client):
    page = admin_client.get("/search/tmdb?q=jaws").get_data(as_text=True)
    assert "TMDB_API_KEY is not configured" in page


def test_excluded_movie_shows_as_final_not_upgrade_candidate(app, admin_client):
    """A movie removed from the shopping list is final: green badge with an
    'excluded' note, even when its best copy is below the quality threshold."""

    with app.app_context():
        movie = make_movie("Skip It", 2000, shopping_list_exclude=True)
        make_movie_file(movie, "DVD")
        db.session.commit()

    page = admin_client.get("/search?q=skip+it").get_data(as_text=True)
    assert 'text-bg-success">DVD &mdash; excluded' in page
    # The chip-color map in base.html's poll script mentions the
    # class on every page, so assert on rendered badges only
    assert 'text-bg-warning">' not in page


def test_episode_title_edition_does_not_split_tv_ranking(app, admin_client):
    """For TV files the edition field just holds the optional episode title,
    so a titled copy and an untitled copy of the same episode compete in one
    ranking group — the outranked copy can't drag the season down."""

    with app.app_context():
        series = make_tv_series("Titled Episodes (2020)")
        make_tv_file(series, 1, 1, "DVD")
        make_tv_file(series, 1, 1, "Bluray-1080p", edition="Pilot")
        db.session.commit()

    page = admin_client.get("/search?q=titled+episodes").get_data(as_text=True)
    assert 'text-bg-success" title="1 episode">Season 1: Bluray-1080p' in page


def test_search_finds_people(app, admin_client):
    with app.app_context():
        first = make_movie("Ensemble Piece", 1970)
        second = make_movie("Ensemble Piece II", 1972)
        make_person(901, "Prolific Player", [first, second])
        make_person(902, "Prolific Extra", [first], character="Extra (uncredited)")
        db.session.commit()

    page = admin_client.get("/search?q=prolific").get_data(as_text=True)
    assert "People" in page
    assert "Prolific Player" in page
    assert "2 films" in page
    assert "credit=901" in page
    # Uncredited-only people never surface, matching the People page
    assert "Prolific Extra" not in page
    # The widening link to the People page carries the query
    assert "/people?q=prolific" in page


def test_search_json_includes_people(app, admin_client):
    with app.app_context():
        movie = make_movie("Solo Show", 1980)
        make_person(903, "Typeahead Thespian", [movie])
        db.session.commit()

    data = admin_client.get("/search.json?q=typeahead").get_json()
    people = [r for r in data["results"] if r["type"] == "Person"]
    assert len(people) == 1
    assert people[0]["title"] == "Typeahead Thespian"
    assert people[0]["detail"] == "Actor · 1 film"
    assert "credit=903" in people[0]["url"]


def test_exact_title_match_outranks_substring_matches(app, admin_client):
    """Match quality beats the alphabet: searching "Up" surfaces the
    film NAMED Up first, then the "Up…" prefix, then mere substring
    matches — so the result cap can no longer bury an exact title
    behind alphabetically-earlier films that just contain it."""

    with app.app_context():
        for title, year in [
            ("Blow-Up", 1966),
            ("Grown Ups", 2010),
            ("Up", 2009),
            ("Upgrade", 2018),
        ]:
            make_movie_file(make_movie(title, year), "Bluray-1080p")
        db.session.commit()

    page = admin_client.get("/search?q=Up").get_data(as_text=True)
    positions = {
        title: page.find(f">{title} (")
        for title in ["Up", "Upgrade", "Blow-Up", "Grown Ups"]
    }
    assert all(pos >= 0 for pos in positions.values()), positions
    assert positions["Up"] < positions["Upgrade"] < positions["Blow-Up"]
    assert positions["Blow-Up"] < positions["Grown Ups"]
