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

    dvd_movie = make_movie("Jaws", 1975)
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
    assert 'badge-warning">DVD' in page

    page = admin_client.get("/search?q=jurassic").get_data(as_text=True)
    assert "Jurassic Park (1993)" in page
    assert 'badge-success">Bluray-2160p Remux' in page
    assert "badge-warning" not in page


def test_search_omits_reviewed_movies_without_files(app, admin_client):
    """A review-only record (a logged unowned film) stays out of the
    library search — it belongs to the TMDb search instead."""

    with app.app_context():
        build_library(app)

    page = admin_client.get("/search?q=jacob").get_data(as_text=True)
    assert "Jacob" not in page
    assert "No copy in library" not in page


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
    assert 'badge-warning" title="2 episodes">Season 1: Unknown' in page
    assert 'badge-success" title="1 episode">Season 2: Bluray-1080p' in page


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
    assert 'badge-success" title="1 episode">Season 1: DVD' in page
    assert 'badge-success" title="1 episode">Season 2: Bluray-480p' in page
    assert 'badge-warning" title="1 episode">Season 3: WEBDL-480p' in page

    # The TV library page uses the same flag on its season badges

    page = admin_client.get("/library/tv").get_data(as_text=True)
    assert 'badge-success">DVD' in page
    assert 'badge-success">Bluray-480p' in page
    assert 'badge-warning">WEBDL-480p' in page


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

    import app.main.routes as main_routes

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
    monkeypatch.setattr(main_routes, "tmdb_get", fake_get)

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
    assert 'badge-success">DVD &mdash; excluded' in page
    assert "badge-warning" not in page


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
    assert 'badge-success" title="1 episode">Season 1: Bluray-1080p' in page


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
    assert people[0]["detail"] == "1 film"
    assert "credit=903" in people[0]["url"]
