"""The credit filmography at /library/movie?credit= shows the person's
entire TMDb career — owned films with quality badges, seen films, and
films with no local record at all — and the movie library page badges
each film's quality by upgrade eligibility."""

from app import db
from app.models import MovieCast, TMDBCredit, User, UserMovieReview
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
    """With no TMDb key configured the filmography still lists every
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
    assert "Owned Credit Film" in page
    # DVD is below the Blu-ray threshold, so it badges as an upgrade candidate
    assert 'badge-warning">DVD' in page
    assert "Unowned Credit Film" in page
    assert "Not in library" in page
    assert "only shows films with local records" in page


def test_filmography_merges_full_tmdb_career(app, admin_client, monkeypatch):
    """TMDb's credit list fills in films with no local record; local rows
    carry their badges and the unknown films link to the review page."""

    import app.main.routes as main_routes

    with app.app_context():
        user_id = User.query.first().id
        person = TMDBCredit(id=535353, name="Career Actor")
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

    class FakeCredits:
        def raise_for_status(self):
            pass

        def json(self):
            return {
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
                    },
                ]
            }

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(main_routes, "tmdb_get", lambda *a, **k: FakeCredits())

    page = admin_client.get("/library/movie?credit=535353").get_data(as_text=True)
    # Owned: Blu-ray is at the threshold, so it badges as final
    assert 'badge-success">Bluray-1080p' in page
    # Seen but unowned: info badge plus the liked heart
    assert 'badge-info">Seen' in page
    assert "bi-heart-fill" in page
    # No local record at all: listed from TMDb, linking to the review form
    assert "Career Unknown Film" in page
    assert "/review/tmdb/200" in page
    assert "The Cameo" in page


def test_filmography_unknown_person_is_404(app, admin_client):
    assert admin_client.get("/library/movie?credit=999999999").status_code == 404


def test_library_page_badges_quality_by_upgradability(app, admin_client):
    with app.app_context():
        upgradable = make_movie("Badge Upgradable Film", 2001)
        make_movie_file(upgradable, "DVD")
        final = make_movie("Badge Final Film", 2002)
        make_movie_file(final, "Bluray-1080p")
        excluded = make_movie("Badge Excluded Film", 2003, shopping_list_exclude=True)
        make_movie_file(excluded, "DVD")
        db.session.commit()

    page = admin_client.get("/library/movie").get_data(as_text=True)
    assert 'badge-warning">DVD' in page
    assert 'badge-success">Bluray-1080p' in page
    # An excluded movie's copy counts as final even below the threshold
    assert 'badge-success">DVD' in page
