"""Pin the full result of a TMDB apply and of a TMDB clear.

The apply methods of Movie and TVSeries read their association, scalar
and date tables. These tests apply a full payload, then a second full
payload, then a glitched payload. They compare every stored column and
every link. Thus, a table entry that differs from the old hand-written
code fails here.
"""

from datetime import datetime

from app import db
from app.models import (
    Movie,
    MovieCast,
    MovieCrew,
    RefTMDBCertification,
    TVCast,
    TVCrew,
    TVSeries,
)

from tests.factories import make_movie, make_tv_series


def _names(links):
    return sorted(row.name for row in links)


MOVIE_PAYLOAD = {
    "id": 991001,
    "adult": False,
    "backdrop_path": "/backdrop.jpg",
    "budget": 1000,
    "homepage": "https://example.com/film",
    "original_language": "en",
    "original_title": "Original Title",
    "overview": "An overview.",
    "popularity": 12.5,
    "poster_path": "/poster.jpg",
    "release_date": "1999-05-04",
    "revenue": 5000,
    "runtime": 101,
    "status": "Released",
    "tagline": "A tagline.",
    "title": "Pinned Film",
    "video": False,
    "vote_average": 7.5,
    "vote_count": 321,
    "external_ids": {"imdb_id": "tt0991001"},
    "genres": [{"id": 18, "name": "Drama"}, {"id": 35, "name": "Comedy"}],
    "keywords": {"keywords": [{"id": 9714, "name": "remake"}]},
    "production_companies": [
        {"id": 41, "name": "Studio A", "origin_country": "US", "logo_path": "/a.png"}
    ],
    "production_countries": [{"iso_3166_1": "US", "name": "United States"}],
    "spoken_languages": [{"iso_639_1": "en", "name": "English"}],
    "release_dates": {
        "results": [
            {
                "iso_3166_1": "US",
                "release_dates": [
                    {"type": 1, "certification": ""},
                    {"type": 3, "certification": "PG-13"},
                ],
            }
        ]
    },
    "belongs_to_collection": {
        "id": 8801,
        "name": "Pinned Collection",
        "poster_path": "/c.jpg",
        "backdrop_path": "/cb.jpg",
    },
    "credits": {
        "cast": [
            {
                "id": 7001,
                "name": "Actor One",
                "gender": 2,
                "profile_path": "/p1.jpg",
                "character": "Hero",
                "order": 0,
            }
        ],
        "crew": [
            {
                "id": 7002,
                "name": "Director One",
                "gender": 1,
                "profile_path": "/p2.jpg",
                "department": "Directing",
                "job": "Director",
            }
        ],
    },
}


def test_a_full_movie_apply_stores_every_field_and_link(app):
    with app.app_context():
        db.session.add(RefTMDBCertification(country="US", certification="PG-13"))
        movie = make_movie("Pinned Film Local", 1998)
        db.session.commit()
        movie.tmdb_movie_apply(MOVIE_PAYLOAD)
        db.session.commit()

        stored = db.session.get(Movie, movie.id)
        assert stored.title == "Pinned Film" and stored.year == 1999
        assert stored.imdb_id == "tt0991001"
        assert stored.tmdb_id == 991001
        assert stored.tmdb_adult is False
        assert stored.tmdb_backdrop_path == "/backdrop.jpg"
        assert stored.tmdb_budget == 1000
        assert stored.tmdb_homepage == "https://example.com/film"
        assert stored.tmdb_original_language == "en"
        assert stored.tmdb_original_title == "Original Title"
        assert stored.tmdb_overview == "An overview."
        assert stored.tmdb_popularity == 12.5
        assert stored.tmdb_poster_path == "/poster.jpg"
        assert stored.tmdb_release_date == datetime(1999, 5, 4)
        assert stored.tmdb_revenue == 5000
        assert stored.tmdb_runtime == 101
        assert stored.tmdb_status == "Released"
        assert stored.tmdb_tagline == "A tagline."
        assert stored.tmdb_title == "Pinned Film"
        assert stored.tmdb_video is False
        assert stored.tmdb_vote_average == 7.5
        assert stored.tmdb_vote_count == 321
        assert stored.tmdb_data_as_of is not None
        assert _names(stored.genres) == ["Comedy", "Drama"]
        assert _names(stored.keywords) == ["remake"]
        (company,) = stored.production_companies
        assert (company.name, company.country, company.tmdb_logo_path) == (
            "Studio A",
            "US",
            "/a.png",
        )
        assert [c.id for c in stored.production_countries] == ["US"]
        assert [s.id for s in stored.spoken_languages] == ["en"]
        assert [(c.country, c.certification) for c in stored.certifications] == [
            ("US", "PG-13")
        ]
        (collection,) = stored.collections
        assert (collection.id, collection.name, collection.tmdb_poster_path) == (
            8801,
            "Pinned Collection",
            "/c.jpg",
        )
        assert [
            (c.credit_id, c.character, c.billing_order)
            for c in MovieCast.query.filter_by(movie_id=movie.id)
        ] == [(7001, "Hero", 0)]
        assert [
            (c.credit_id, c.department, c.job)
            for c in MovieCrew.query.filter_by(movie_id=movie.id)
        ] == [(7002, "Directing", "Director")]

        # A second full payload replaces every link. A payload with no
        # collection removes the collection link, on purpose: most films
        # belong to no collection.

        second = dict(
            MOVIE_PAYLOAD,
            genres=[{"id": 27, "name": "Horror"}],
            keywords={"keywords": [{"id": 1, "name": "ghost"}]},
            production_companies=[{"id": 42, "name": "Studio B"}],
            production_countries=[{"iso_3166_1": "GB", "name": "United Kingdom"}],
            spoken_languages=[{"iso_639_1": "fr", "name": "French"}],
            belongs_to_collection=None,
        )
        stored.tmdb_movie_apply(second)
        db.session.commit()
        stored = db.session.get(Movie, movie.id)
        assert _names(stored.genres) == ["Horror"]
        assert _names(stored.keywords) == ["ghost"]
        assert _names(stored.production_companies) == ["Studio B"]
        assert [c.id for c in stored.production_countries] == ["GB"]
        assert [s.id for s in stored.spoken_languages] == ["fr"]
        assert stored.collections.count() == 0

        # A glitched payload with empty lists keeps every stored link (#251).

        stored.tmdb_movie_apply(
            {
                "id": 991001,
                "genres": [],
                "keywords": {"keywords": []},
                "production_companies": [],
                "production_countries": [],
                "spoken_languages": [],
                "release_dates": {"results": []},
                "credits": {"cast": [], "crew": []},
            }
        )
        db.session.commit()
        stored = db.session.get(Movie, movie.id)
        assert _names(stored.genres) == ["Horror"]
        assert _names(stored.keywords) == ["ghost"]
        assert _names(stored.production_companies) == ["Studio B"]
        assert [c.id for c in stored.production_countries] == ["GB"]
        assert [s.id for s in stored.spoken_languages] == ["fr"]
        assert stored.certifications.count() == 1
        assert MovieCast.query.filter_by(movie_id=movie.id).count() == 1
        assert MovieCrew.query.filter_by(movie_id=movie.id).count() == 1

        # The clear removes every TMDB field and every link.

        stored.tmdb_movie_clear()
        db.session.commit()
        stored = db.session.get(Movie, movie.id)
        for column in Movie.__table__.columns:
            if column.key.startswith("tmdb_") and column.key != "tmdb_ignored":
                assert getattr(stored, column.key) is None, column.key
        assert stored.imdb_id is None
        assert stored.tmdb_ignored is True
        for rel in (
            "collections",
            "genres",
            "keywords",
            "production_companies",
            "production_countries",
            "spoken_languages",
            "certifications",
        ):
            assert getattr(stored, rel).count() == 0, rel
        assert MovieCast.query.filter_by(movie_id=movie.id).count() == 0
        assert MovieCrew.query.filter_by(movie_id=movie.id).count() == 0
        assert stored.title == "Pinned Film"


TV_PAYLOAD = {
    "id": 992001,
    "backdrop_path": "/tvb.jpg",
    "first_air_date": "2001-02-03",
    "last_air_date": "2004-05-06",
    "homepage": "https://example.com/tv",
    "in_production": False,
    "name": "Pinned Series",
    "number_of_episodes": 40,
    "number_of_seasons": 2,
    "original_language": "en",
    "original_name": "Pinned Series Original",
    "overview": "A series.",
    "popularity": 3.5,
    "poster_path": "/tvp.jpg",
    "status": "Ended",
    "type": "Scripted",
    "vote_average": 8.1,
    "vote_count": 99,
    "external_ids": {"imdb_id": "tt0992001", "tvdb_id": 4242},
    "genres": [{"id": 18, "name": "Drama"}],
    "keywords": {"results": [{"id": 310, "name": "time travel"}]},
    "networks": [
        {"id": 49, "name": "HBO", "logo_path": "/hbo.png", "origin_country": "US"}
    ],
    "production_companies": [{"id": 43, "name": "TV Studio"}],
    "content_ratings": {
        "results": [
            {"iso_3166_1": "GB", "rating": "15"},
            {"iso_3166_1": "US", "rating": "TV-MA"},
        ]
    },
    "seasons": [
        {
            "id": 78001,
            "season_number": 1,
            "name": "Season 1",
            "air_date": "2001-02-03",
            "episode_count": 20,
            "overview": "First.",
            "poster_path": "/s1.jpg",
        },
        {"id": 78002, "season_number": 2, "name": "Season 2", "episode_count": 20},
    ],
    "aggregate_credits": {
        "cast": [
            {
                "id": 7101,
                "name": "TV Actor",
                "order": 0,
                "roles": [{"character": "Lead", "episode_count": 40}],
            }
        ],
        "crew": [
            {
                "id": 7102,
                "name": "TV Writer",
                "department": "Writing",
                "jobs": [{"job": "Writer", "episode_count": 10}],
            }
        ],
    },
}


def test_a_full_tv_apply_stores_every_field_and_link(app):
    from app.models import TMDBSeason

    with app.app_context():
        series = make_tv_series("Pinned Series Local")
        db.session.commit()
        series.tmdb_tv_apply(TV_PAYLOAD)
        db.session.commit()

        stored = db.session.get(TVSeries, series.id)
        assert stored.imdb_id == "tt0992001"
        assert stored.tvdb_id == 4242
        assert stored.tmdb_id == 992001
        assert stored.tmdb_backdrop_path == "/tvb.jpg"
        assert stored.tmdb_first_air_date == datetime(2001, 2, 3)
        assert stored.tmdb_last_air_date == datetime(2004, 5, 6)
        assert stored.tmdb_homepage == "https://example.com/tv"
        assert stored.tmdb_in_production is False
        assert stored.tmdb_name == "Pinned Series"
        assert stored.tmdb_number_of_episodes == 40
        assert stored.tmdb_number_of_seasons == 2
        assert stored.tmdb_content_rating == "TV-MA"
        assert stored.tmdb_original_language == "en"
        assert stored.tmdb_original_name == "Pinned Series Original"
        assert stored.tmdb_overview == "A series."
        assert stored.tmdb_popularity == 3.5
        assert stored.tmdb_poster_path == "/tvp.jpg"
        assert stored.tmdb_status == "Ended"
        assert stored.tmdb_type == "Scripted"
        assert stored.tmdb_vote_average == 8.1
        assert stored.tmdb_vote_count == 99
        assert stored.tmdb_data_as_of is not None
        assert _names(stored.genres) == ["Drama"]
        assert _names(stored.keywords) == ["time travel"]
        (network,) = stored.networks
        assert (network.name, network.tmdb_logo_path, network.origin_country) == (
            "HBO",
            "/hbo.png",
            "US",
        )
        assert _names(stored.production_companies) == ["TV Studio"]
        seasons = sorted(stored.seasons, key=lambda s: s.season_number)
        assert [(s.id, s.episode_count, s.name) for s in seasons] == [
            (78001, 20, "Season 1"),
            (78002, 20, "Season 2"),
        ]
        assert seasons[0].air_date == datetime(2001, 2, 3)
        assert seasons[0].tmdb_poster_path == "/s1.jpg"
        assert seasons[1].air_date is None
        assert [
            (c.credit_id, c.character, c.episode_count)
            for c in TVCast.query.filter_by(tv_id=series.id)
        ] == [(7101, "Lead", 40)]
        assert [
            (c.credit_id, c.department, c.job)
            for c in TVCrew.query.filter_by(tv_id=series.id)
        ] == [(7102, "Writing", "Writer")]

        # The season fields refresh on every apply. An unlinked season goes
        # (#305). A running series keeps its stored episode counts.

        second = dict(
            TV_PAYLOAD,
            status="Returning Series",
            number_of_episodes=50,
            seasons=[{"id": 78001, "season_number": 1, "episode_count": 22}],
        )
        stored.tmdb_tv_apply(second)
        db.session.commit()
        stored = db.session.get(TVSeries, series.id)
        assert [(s.id, s.episode_count) for s in stored.seasons] == [(78001, 22)]
        assert db.session.get(TMDBSeason, 78002) is None
        assert stored.tmdb_number_of_episodes == 40

        # A glitched payload keeps every stored link (#251).

        stored.tmdb_tv_apply(
            {
                "id": 992001,
                "genres": [],
                "keywords": {"results": []},
                "networks": [],
                "production_companies": [],
                "seasons": [],
                "content_ratings": {"results": []},
            }
        )
        db.session.commit()
        stored = db.session.get(TVSeries, series.id)
        assert _names(stored.genres) == ["Drama"]
        assert _names(stored.keywords) == ["time travel"]
        assert _names(stored.networks) == ["HBO"]
        assert _names(stored.production_companies) == ["TV Studio"]
        assert stored.seasons.count() == 1
        assert stored.tmdb_content_rating == "TV-MA"

        stored.tmdb_tv_clear()
        db.session.commit()
        stored = db.session.get(TVSeries, series.id)
        for column in TVSeries.__table__.columns:
            if column.key.startswith("tmdb_") and column.key != "tmdb_ignored":
                assert getattr(stored, column.key) is None, column.key
        assert stored.imdb_id is None and stored.tvdb_id is None
        assert stored.tmdb_ignored is True
        for rel in (
            "genres",
            "keywords",
            "networks",
            "production_companies",
            "seasons",
        ):
            assert getattr(stored, rel).count() == 0, rel
        assert TVCast.query.filter_by(tv_id=series.id).count() == 0
        assert TVCrew.query.filter_by(tv_id=series.id).count() == 0


def test_an_empty_object_at_a_list_path_keeps_the_links(app):
    """A {} where TMDB sends a list is a glitch (#251). It must not clear."""

    from app.models import Movie, TMDBGenre

    with app.app_context():
        movie = make_movie(title="Object Glitch", year=2001, tmdb_id=991003)
        movie.tmdb_movie_apply({"id": 991003, "genres": [{"id": 27, "name": "Horror"}]})
        db.session.commit()
        nameless_before = TMDBGenre.query.filter_by(name=None).count()

        movie.tmdb_movie_apply(
            {
                "id": 991003,
                "genres": {},
                "keywords": {"keywords": {}},
                "belongs_to_collection": {},
            }
        )
        db.session.commit()
        stored = db.session.get(Movie, movie.id)
        assert _names(stored.genres) == ["Horror"]
        assert stored.collections.count() == 0
        assert TMDBGenre.query.filter_by(name=None).count() == nameless_before
