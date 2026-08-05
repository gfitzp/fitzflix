"""Criterion Collection refresh from Wikidata: SPARQL response parsing, the
access etiquette headers, TMDb-id-first matching with title/year fallback,
preservation of hand-set fields, and the Redis cache.
"""

import json

from app import db
from app.models import Movie
from app.videos import refresh_criterion_collection_info

from tests.factories import make_movie

import app.videos as videos

SPARQL_RESPONSE = {
    "results": {
        "bindings": [
            {
                # Matched by TMDb id, even though the library title differs
                "spine": {"value": "1"},
                "tmdbId": {"value": "1863"},
                "filmLabel": {"value": "La Grande Illusion"},
                "year": {"value": "1937"},
                "criterionId": {"value": "336-grand-illusion"},
            },
            {
                # No TMDb id on Wikidata: matched by title and year
                "spine": {"value": "2"},
                "filmLabel": {"value": "Seven Samurai"},
                "year": {"value": "1954"},
            },
            {
                # Unparseable spine rows are skipped, not fatal
                "spine": {"value": "n/a"},
                "filmLabel": {"value": "Broken Row"},
            },
        ]
    }
}


SETS_RESPONSE = {
    "results": {
        "bindings": [
            {
                # A box set: the spine and title belong to the set item,
                # the film details to the member
                "spine": {"value": "1000"},
                "setLabel": {"value": "Godzilla: The Showa-Era Films, 1954-1975"},
                "tmdbId": {"value": "39462"},
                "filmLabel": {"value": "All Monsters Attack"},
                "year": {"value": "1969"},
                "criterionId": {"value": "29306"},
            },
        ]
    }
}


def fake_sparql(monkeypatch, calls=None):
    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    def fake_get(url, params=None, headers=None, timeout=None):
        if calls is not None:
            calls.append({"url": url, "params": params, "headers": headers})
        if "P527" in (params or {}).get("query", ""):
            return FakeResponse(SETS_RESPONSE)
        return FakeResponse(SPARQL_RESPONSE)

    monkeypatch.setattr(videos.requests, "get", fake_get)


def test_refresh_matches_by_tmdb_id_and_title_year(app, monkeypatch):
    calls = []
    fake_sparql(monkeypatch, calls)

    with app.app_context():
        # The library's title differs from Wikidata's label; only the TMDb
        # id can connect them

        by_id = make_movie("Grand Illusion", 1937, tmdb_id=1863)

        # No TMDb match yet: falls back to title and year

        by_title = make_movie("Seven Samurai", 1954)

        # Not a Criterion release: untouched

        other = make_movie("Sharknado", 2013, tmdb_id=119283)
        db.session.commit()
        ids = (by_id.id, by_title.id, other.id)

        assert refresh_criterion_collection_info() is True

        db.session.expire_all()
        assert db.session.get(Movie, ids[0]).criterion_spine_number == 1
        assert db.session.get(Movie, ids[1]).criterion_spine_number == 2
        assert db.session.get(Movie, ids[2]).criterion_spine_number is None

        # The Criterion film-page id rides along when Wikidata has it, and
        # its absence doesn't clear anything

        assert db.session.get(Movie, ids[0]).criterion_film_id == "336-grand-illusion"
        assert db.session.get(Movie, ids[1]).criterion_film_id is None

        # New matches get optimistic defaults for fields Wikidata lacks

        assert db.session.get(Movie, ids[0]).criterion_in_print is True
        assert db.session.get(Movie, ids[0]).criterion_disc_owned is False

    # Wikidata access etiquette: identifying User-Agent with a contact,
    # SPARQL JSON accept header, and compression

    assert len(calls) == 2
    headers = calls[0]["headers"]
    assert headers == calls[1]["headers"]
    assert headers["User-Agent"].startswith("FitzflixBot/")
    assert "mailto:" in headers["User-Agent"]
    assert headers["Accept"] == "application/sparql-results+json"
    assert "gzip" in headers["Accept-Encoding"]


def test_refresh_preserves_hand_set_fields(app, monkeypatch):
    fake_sparql(monkeypatch)

    with app.app_context():
        movie = make_movie(
            "Grand Illusion",
            1937,
            tmdb_id=1863,
            criterion_in_print=False,
            criterion_disc_owned=True,
            criterion_set_title="A Set I Typed Myself",
        )
        db.session.commit()
        movie_id = movie.id

        assert refresh_criterion_collection_info() is True

        db.session.expire_all()
        movie = db.session.get(Movie, movie_id)
        assert movie.criterion_spine_number == 1
        assert movie.criterion_in_print is False
        assert movie.criterion_disc_owned is True
        assert movie.criterion_set_title == "A Set I Typed Myself"


def test_single_movie_refresh_uses_cache(app, monkeypatch):
    """A one-movie refresh (the per-import path) reads the cached list
    instead of re-querying Wikidata."""

    calls = []
    fake_sparql(monkeypatch, calls)

    with app.app_context():
        movie = make_movie("Seven Samurai", 1954)
        db.session.commit()
        movie_id = movie.id

        # Prime the cache with a full (forced) fetch

        assert refresh_criterion_collection_info() is True
        assert len(calls) == 2

        # A single-movie refresh is served from Redis

        assert refresh_criterion_collection_info(movie_id=movie_id) is True
        assert len(calls) == 2

        db.session.expire_all()
        assert db.session.get(Movie, movie_id).criterion_spine_number == 2

        # The cached payload is well-formed JSON with the parsed rows

        cached = json.loads(app.redis.get(videos.CRITERION_CACHE_KEY))
        assert {release["spine_number"] for release in cached} == {1, 2, 1000}


def test_refresh_survives_wikidata_outage(app, monkeypatch):
    """A failed fetch rolls back and logs rather than raising."""

    def explode(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(videos.requests, "get", explode)

    with app.app_context():
        movie = make_movie("Seven Samurai", 1954)
        db.session.commit()
        movie_id = movie.id

        assert refresh_criterion_collection_info() is not True

        db.session.expire_all()
        assert db.session.get(Movie, movie_id).criterion_spine_number is None


def test_shopping_list_links_direct_to_criterion_film_page(
    app, admin_client, monkeypatch
):
    """A movie with a stored Criterion film id gets a direct film-page link
    instead of a criterion.com search."""

    from tests.factories import make_movie_file

    with app.app_context():
        movie = make_movie(
            "Grand Illusion",
            1937,
            criterion_spine_number=1,
            criterion_film_id="336-grand-illusion",
            criterion_in_print=True,
        )
        make_movie_file(movie, "DVD")
        db.session.commit()

    page = admin_client.get("/shopping-list/movie").get_data(as_text=True)
    assert "https://www.criterion.com/films/336-grand-illusion" in page


def test_box_set_members_get_set_spine_and_title(app, monkeypatch):
    """A film inside a box set matches through the set's P527 membership:
    it takes the set's spine and title, and its own film-page id — but a
    hand-curated set title is never overwritten."""

    fake_sparql(monkeypatch)

    with app.app_context():
        member = make_movie("All Monsters Attack", 1969, tmdb_id=39462)
        curated = make_movie(
            "Godzilla's Revenge",
            1969,
            tmdb_id=39462,
            criterion_set_title="My Own Set Name",
        )
        db.session.commit()
        member_id, curated_id = member.id, curated.id

        assert refresh_criterion_collection_info() is True

        db.session.expire_all()
        member = db.session.get(Movie, member_id)
        assert member.criterion_spine_number == 1000
        assert member.criterion_set_title == "Godzilla: The Showa-Era Films, 1954-1975"
        assert member.criterion_film_id == "29306"

        curated = db.session.get(Movie, curated_id)
        assert curated.criterion_spine_number == 1000
        assert curated.criterion_set_title == "My Own Set Name"
