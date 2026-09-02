"""The credit filmography at /library/movie?credit= shows the person's
entire TMDB career — owned films with quality badges, seen films, and
films with no local record at all — and the movie library page badges
each film's quality by upgrade eligibility."""

from app import db
from app.models import Movie, MovieCast, MovieCrew, TMDBCredit, User, UserMovieReview
from app.videos import star_rating_fields
from tests.factories import make_movie, make_movie_file


def make_cast(person, movie, character="Self", order=0):
    cast = MovieCast(
        movie_id=movie.id,
        credit_id=person.id,
        character=character,
        billing_order=order,
    )
    db.session.add(cast)
    db.session.flush()
    return cast


def test_filmography_includes_unowned_films_without_tmdb(app, admin_client):
    """With no TMDB key configured the filmography still lists every
    locally credited film, owned or not."""

    with app.app_context():
        person = TMDBCredit(id=424242, name="Filmography Actor")
        db.session.add(person)
        owned = make_movie("Owned Credit Film", 1990)
        make_movie_file(owned, "DVD")
        unowned = make_movie("Unowned Credit Film", 1992)
        db.session.flush()
        make_cast(person, owned)
        make_cast(person, unowned)
        db.session.commit()

    page = admin_client.get("/library/movie?credit=424242").get_data(as_text=True)
    assert "Filmography Actor" in page
    # No stored profile path, so the header shows the silhouette placeholder
    assert "bi-person-fill" in page
    assert "Owned Credit Film" in page
    # Ownership and quality moved into each poster's popover (Aug
    # 2026); the tiles carry the actions instead
    assert 'title="In your Fitzflix library"' not in page
    assert ">DVD<" not in page
    assert page.count("data-state-movie=") == 2
    assert "Unowned Credit Film" in page
    assert "Not in library" not in page
    assert "shows only the films that have local records" in page


def test_filmography_merges_full_tmdb_career(app, admin_client, monkeypatch):
    """TMDB's credit list fills in films with no local record; local rows
    carry their badges and the unknown films link to the review page."""

    import app.main.library as library

    with app.app_context():
        user_id = User.query.first().id
        person = TMDBCredit(
            id=535353, name="Career Actor", tmdb_profile_path="/career.jpg"
        )
        db.session.add(person)
        owned = make_movie("Career Owned Film", 1980, tmdb_id=100)
        make_movie_file(owned, "Bluray-1080p")
        seen = make_movie("Career Seen Film", 1985, tmdb_id=150)
        db.session.add(
            UserMovieReview(
                user_id=user_id,
                movie_id=seen.id,
                review="",
                liked=True,
                **star_rating_fields(4),
            )
        )
        db.session.flush()
        make_cast(person, owned)
        make_cast(person, seen)
        db.session.commit()

    class FakeTMDB:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self.payload

    def fake_tmdb_get(url, **kwargs):
        if url.endswith("/movie_credits"):
            return FakeTMDB(
                {
                    "cast": [
                        {
                            "id": 100,
                            "title": "Career Owned Film",
                            "release_date": "1980-05-01",
                            "character": "The Lead",
                        },
                        {
                            "id": 150,
                            "title": "Career Seen Film",
                            "release_date": "1985-02-01",
                            "character": "The Friend",
                        },
                        {
                            "id": 200,
                            "title": "Career Unknown Film",
                            "release_date": "1999-09-09",
                            "character": "The Cameo",
                            "poster_path": "/unknown.jpg",
                            "overview": "A cameo-laden curiosity from 1999.",
                        },
                    ]
                }
            )
        return FakeTMDB(
            {
                "name": "Career Actor",
                "profile_path": "/career.jpg",
                "biography": "Worked steadily for decades.",
                "birthday": "1920-01-10",
                "deathday": "1999-05-02",
            }
        )

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(library, "tmdb_get", fake_tmdb_get)

    page = admin_client.get("/library/movie?credit=535353").get_data(as_text=True)
    assert "/w185/career.jpg" in page
    # A dead person's age is figured against the death date
    assert "Born January 10, 1920" in page
    assert "Died May 2, 1999 (aged 79)" in page
    assert "Worked steadily for decades." in page
    # Ownership, seen-ness, and the liked heart all moved off the
    # tiles (Aug 2026): the popover badges ownership, the hydrated
    # ladder shows the verdict
    assert 'title="In your Fitzflix library"' not in page
    assert "Bluray-1080p" not in page
    assert 'text-bg-info me-1">Seen' not in page
    assert "bi-heart-fill" not in page
    # No local record at all: listed from TMDB, linking to the review form
    assert "Career Unknown Film" in page
    assert "/review/tmdb/200" in page
    assert "The Cameo" in page

    # The synopsis lives in the poster popover now (#45d): tiles are
    # armed with data-card-url — by movie_id for records, tmdb_id
    # for the record-less TMDB credits — and every tile carries the
    # actions, keyed the same way

    assert "A cameo-laden curiosity from 1999." not in page
    assert 'data-card-url="/movie_card?tmdb_id=200"' in page
    assert page.count('data-card-url="/movie_card') >= 3
    assert 'data-state-tmdb="200"' in page
    assert page.count("data-state-movie=") == 2


def test_filmography_badges_recommended_owned_films(app, admin_client, monkeypatch):
    """An owned unwatched film in the stored recommendations carries the
    might-interest badge on filmographies, alongside its library badge —
    the same claim the library rail makes."""

    import json

    import app.main.library as library

    from app.recommendations import RECS_KEY

    with app.app_context():
        user_id = User.query.first().id
        person = TMDBCredit(id=636363, name="Ranked Actor")
        db.session.add(person)
        recommended = make_movie("Ranked Owned Film", 1946, tmdb_id=300)
        make_movie_file(recommended, "Bluray-1080p")
        unranked = make_movie("Unranked Owned Film", 1950, tmdb_id=301)
        make_movie_file(unranked, "Bluray-1080p")
        db.session.flush()
        make_cast(person, recommended)
        make_cast(person, unranked)
        db.session.commit()
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

    class FakeTMDB:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self.payload

    def fake_tmdb_get(url, **kwargs):
        if url.endswith("/movie_credits"):
            return FakeTMDB(
                {
                    "cast": [
                        {
                            "id": 300,
                            "title": "Ranked Owned Film",
                            "release_date": "1946-08-23",
                            "character": "The Detective",
                        },
                        {
                            "id": 301,
                            "title": "Unranked Owned Film",
                            "release_date": "1950-03-01",
                            "character": "The Heavy",
                        },
                    ]
                }
            )
        return FakeTMDB({"name": "Ranked Actor"})

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(library, "tmdb_get", fake_tmdb_get)

    # The badge rides the recommended film's anchor as a card label
    # (Aug 2026) — one film carries it, and it's the ranked one

    page = admin_client.get("/library/movie?credit=636363").get_data(as_text=True)
    assert page.count("Might interest you") == 1
    assert (
        f'data-card-url="/movie_card?movie_id={recommended_id}" '
        "data-card-reasons='[\"Might interest you\"]'"
    ) in page


def test_filmography_owned_rows_show_seen_and_watchlist(app, admin_client, monkeypatch):
    """Owned rows answer the funnel through the widgets since the Aug
    2026 revision: no badges in the tile markup — the hydrated ladder
    speaks for Seen, the toggle and the popover for the watchlist —
    and /movie_states supplies both films' answers."""

    import app.main.library as library

    from app.models import UserWatchlist

    with app.app_context():
        user_id = User.query.first().id
        person = TMDBCredit(id=737373, name="Funnel Actor")
        db.session.add(person)
        owned_seen = make_movie("Funnel Owned Seen Film", 1960, tmdb_id=400)
        make_movie_file(owned_seen, "Bluray-1080p")
        owned_wanted = make_movie("Funnel Owned Wanted Film", 1965, tmdb_id=401)
        make_movie_file(owned_wanted, "Bluray-1080p")
        db.session.add(
            UserMovieReview(
                user_id=user_id,
                movie_id=owned_seen.id,
                review="",
                **star_rating_fields(4),
            )
        )
        db.session.add(UserWatchlist(user_id=user_id, movie_id=owned_wanted.id))
        db.session.flush()
        make_cast(person, owned_seen)
        make_cast(person, owned_wanted)
        db.session.commit()

    class FakeTMDB:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self.payload

    def fake_tmdb_get(url, **kwargs):
        if url.endswith("/movie_credits"):
            return FakeTMDB(
                {
                    "cast": [
                        {
                            "id": 400,
                            "title": "Funnel Owned Seen Film",
                            "release_date": "1960-01-01",
                            "character": "The Lead",
                        },
                        {
                            "id": 401,
                            "title": "Funnel Owned Wanted Film",
                            "release_date": "1965-01-01",
                            "character": "The Rival",
                        },
                    ]
                }
            )
        return FakeTMDB({"name": "Funnel Actor"})

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(library, "tmdb_get", fake_tmdb_get)

    page = admin_client.get("/library/movie?credit=737373").get_data(as_text=True)
    assert 'title="In your Fitzflix library"' not in page
    assert 'text-bg-info me-1">Seen' not in page
    assert "On your watchlist" not in page
    assert page.count("data-state-movie=") == 2

    with app.app_context():
        seen_id = Movie.query.filter_by(tmdb_id=400).one().id
        wanted_id = Movie.query.filter_by(tmdb_id=401).one().id
    states = admin_client.get(
        f"/movie_states?movie_ids={seen_id},{wanted_id}"
    ).get_json()
    assert states["movies"][str(seen_id)]["rating"] == 4.0
    assert states["movies"][str(wanted_id)]["on_watchlist"] is True


def test_filmography_serves_people_without_local_credit_rows(
    app, admin_client, monkeypatch
):
    """A person from a not-in-library film's cast has no TMDBCredit row;
    their filmography still renders, with the name and career from TMDB."""

    import app.main.library as library

    class FakeTMDB:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self.payload

    def fake_tmdb_get(url, **kwargs):
        if url.endswith("/movie_credits"):
            return FakeTMDB(
                {
                    "cast": [
                        {
                            "id": 300,
                            "title": "Wanderer Unknown Film",
                            "release_date": "2005-03-03",
                            "character": "The Stranger",
                        }
                    ]
                }
            )
        return FakeTMDB(
            {
                "name": "Uncredited Wanderer",
                "profile_path": "/wanderer.jpg",
                "biography": "Wandered into pictures by accident.",
                "birthday": "1970-03-03",
                "place_of_birth": "Butte, Montana, USA",
            }
        )

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(library, "tmdb_get", fake_tmdb_get)

    page = admin_client.get("/library/movie?credit=808080").get_data(as_text=True)
    assert "Uncredited Wanderer" in page
    # The header portrait and biography come from the same TMDB person
    # lookup; a living person's born line carries their current age
    assert "/w185/wanderer.jpg" in page
    assert "Born March 3, 1970 in Butte, Montana, USA (age" in page
    assert "Wandered into pictures by accident." in page
    assert "Wanderer Unknown Film" in page
    assert "/review/tmdb/300" in page
    assert "Not in library" not in page


def test_filmography_includes_key_crew_credits(app, admin_client, monkeypatch):
    """A director's TMDB career renders: key crew credits become rows,
    share a row with acting credits on the same film, non-key jobs stay
    out, and owned crew films attach their local record through
    MovieCrew."""

    import app.main.library as library

    from app.models import MovieCrew

    with app.app_context():
        person = TMDBCredit(id=838383, name="Career Director")
        db.session.add(person)
        owned = make_movie("Directed Owned Film", 1970, tmdb_id=500)
        make_movie_file(owned, "Bluray-1080p")
        db.session.flush()
        db.session.add(
            MovieCrew(
                movie_id=owned.id,
                credit_id=person.id,
                department="Directing",
                job="Director",
            )
        )
        db.session.commit()

    class FakeTMDB:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self.payload

    def fake_tmdb_get(url, **kwargs):
        if url.endswith("/movie_credits"):
            return FakeTMDB(
                {
                    "cast": [
                        {
                            "id": 500,
                            "title": "Directed Owned Film",
                            "release_date": "1970-01-01",
                            "character": "The Cameo",
                        }
                    ],
                    "crew": [
                        {
                            "id": 500,
                            "title": "Directed Owned Film",
                            "release_date": "1970-01-01",
                            "department": "Directing",
                            "job": "Director",
                        },
                        # Deliberately out of closing-credit order: the
                        # credit line must sort to Director · Writer ·
                        # Composer regardless of payload order
                        {
                            "id": 501,
                            "title": "Directed Unknown Film",
                            "release_date": "1975-01-01",
                            "department": "Sound",
                            "job": "Original Music Composer",
                        },
                        {
                            "id": 501,
                            "title": "Directed Unknown Film",
                            "release_date": "1975-01-01",
                            "department": "Writing",
                            "job": "Screenplay",
                        },
                        {
                            "id": 501,
                            "title": "Directed Unknown Film",
                            "release_date": "1975-01-01",
                            "department": "Directing",
                            "job": "Director",
                        },
                        {
                            "id": 502,
                            "title": "Thanked Film",
                            "release_date": "1980-01-01",
                            "department": "Crew",
                            "job": "Thanks",
                        },
                    ],
                }
            )
        return FakeTMDB({"name": "Career Director"})

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(library, "tmdb_get", fake_tmdb_get)

    page = admin_client.get("/library/movie?credit=838383").get_data(as_text=True)

    # The owned film merges its cast and crew credits into one row,
    # attaching its local record through the MovieCrew join — visible
    # as the movie-keyed state container (ownership badges live in the
    # popover since Aug 2026)

    assert "Directed Owned Film (1970)" in page
    assert "Director &middot; as The Cameo" in page
    assert page.count("data-state-movie=") == 1

    # A crew-only film rows up with its role labels in closing-credit
    # order (the payload listed Composer, Writer, Director); non-key
    # jobs don't appear at all

    assert "Directed Unknown Film (1975)" in page
    assert "Director &middot; Writer &middot; Composer" in page
    assert "Thanked Film" not in page


def test_filmography_tolerates_pre_crew_cached_payloads(app, admin_client, monkeypatch):
    """Day-cached credits payloads written before crew credits joined
    the filmography are bare cast lists; the page still renders them."""

    import json

    import app.main.library as library

    with app.app_context():
        person = TMDBCredit(id=848484, name="Cached Actor")
        db.session.add(person)
        db.session.commit()

    app.redis.set(
        "fitzflix:tmdb:person:848484:credits",
        json.dumps(
            [
                {
                    "id": 600,
                    "title": "Old Cache Film",
                    "release_date": "1990-01-01",
                    "character": "The Lead",
                }
            ]
        ),
    )

    class FakeTMDB:
        def raise_for_status(self):
            pass

        def json(self):
            return {"name": "Cached Actor"}

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(library, "tmdb_get", lambda url, **kwargs: FakeTMDB())

    page = admin_client.get("/library/movie?credit=848484").get_data(as_text=True)
    assert "Old Cache Film (1990)" in page
    assert "as The Lead" in page


def test_filmography_unknown_person_is_404(app, admin_client):
    """No local row and no TMDB key to ask: the id can't be resolved."""

    assert admin_client.get("/library/movie?credit=999999999").status_code == 404


def test_filmography_person_unknown_to_tmdb_is_404(app, admin_client, monkeypatch):
    """TMDB errors on the person lookup (no such id), so the page 404s."""

    import requests

    import app.main.library as library

    def raising_tmdb_get(url, **kwargs):
        raise requests.HTTPError("404 Client Error")

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(library, "tmdb_get", raising_tmdb_get)

    assert admin_client.get("/library/movie?credit=888888888").status_code == 404


def test_library_page_badges_quality_by_upgradability(app, admin_client):
    """The library wall's tiles carry the actions; the green/amber
    shopping answer moved into each poster's popover (Aug 2026)."""

    with app.app_context():
        upgradable = make_movie("Badge Upgradable Film", 2001)
        make_movie_file(upgradable, "DVD")
        final = make_movie("Badge Final Film", 2002)
        make_movie_file(final, "Bluray-1080p")
        excluded = make_movie("Badge Excluded Film", 2003, shopping_list_exclude=True)
        make_movie_file(excluded, "DVD")
        db.session.commit()
        upgradable_id, final_id, excluded_id = upgradable.id, final.id, excluded.id

    page = admin_client.get("/library/movie").get_data(as_text=True)
    assert 'text-bg-warning">DVD' not in page
    assert page.count("data-state-movie=") == 3

    card = admin_client.get(f"/movie_card?movie_id={upgradable_id}").get_data(
        as_text=True
    )
    assert 'text-bg-warning align-middle me-1" title="In your Fitzflix library' in card
    card = admin_client.get(f"/movie_card?movie_id={final_id}").get_data(as_text=True)
    assert 'text-bg-success align-middle me-1" title="In your Fitzflix library' in card
    # An excluded movie's copy counts as final even below the threshold
    card = admin_client.get(f"/movie_card?movie_id={excluded_id}").get_data(
        as_text=True
    )
    assert 'text-bg-success align-middle me-1" title="In your Fitzflix library' in card


def test_movie_page_cast_scroller_shows_all_credited_actors(app, admin_client):
    """The movie page's cast pane holds every credited actor in billing
    order — the old page stopped at the top-billed three."""

    from tests.factories import make_movie

    with app.app_context():
        movie = make_movie("Ensemble Film", 1974)
        for order in range(8):
            person = TMDBCredit(id=700000 + order, name=f"Ensemble Actor {order}")
            db.session.add(person)
            db.session.flush()
            make_cast(person, movie, character=f"Passenger {order}", order=order)
        director = TMDBCredit(id=700100, name="Ensemble Director")
        db.session.add(director)
        db.session.flush()
        db.session.add(
            MovieCrew(
                movie_id=movie.id,
                credit_id=director.id,
                department="Directing",
                job="Director",
            )
        )
        db.session.commit()
        movie_id = movie.id

    page = admin_client.get(f"/movie/{movie_id}").get_data(as_text=True)
    assert "cast-scroller" in page
    for order in range(8):
        assert f"Ensemble Actor {order}" in page
        assert f"credit=70000{order}" in page
    assert "Passenger 7" in page  # characters render beneath the names

    # Billing order is preserved left to right
    positions = [page.index(f"Ensemble Actor {order}") for order in range(8)]
    assert positions == sorted(positions)

    # The director line links to the filmography page, muted like the
    # rating drive's featured card

    assert (
        'Directed by <a href="/library/movie?credit=700100" '
        'class="link-secondary text-secondary">Ensemble Director</a>' in page
    )
