"""TVEpisode rows (#78): slot identity, uniqueness, series cascade, and
the fetch/apply refresh legs that populate them."""

import pytest
from sqlalchemy.exc import IntegrityError

from app import db
from app.models import TMDBSeason, TVEpisode

from tests.factories import make_tv_episode, make_tv_series


class FakeTMDb:
    """A scripted TMDb: answers the base series call and season-batch
    calls, recording every append_to_response it was asked for."""

    def __init__(self, season_count):
        self.season_count = season_count
        self.appends = []

    def get(self, url, params=None, **kwargs):
        append = (params or {}).get("append_to_response", "")
        self.appends.append(append)

        class Response:
            url = "fake://tmdb"

            def __init__(self, payload):
                self._payload = payload

            def json(self):
                return self._payload

            def raise_for_status(self):
                pass

        if "season/" not in append:
            return Response(
                {
                    "id": 121,
                    "name": "Doctor Who",
                    "external_ids": {"imdb_id": "tt0056751", "tvdb_id": 76107},
                    "seasons": [
                        {
                            "id": 1000 + n,
                            "season_number": n,
                            "episode_count": 2,
                            "name": f"Season {n}",
                        }
                        for n in range(self.season_count)
                    ],
                }
            )

        payload = {}
        for part in append.split(","):
            n = int(part.split("/")[1])
            payload[part] = {
                "season_number": n,
                "episodes": [
                    {
                        "id": n * 100 + e,
                        "episode_number": e,
                        "name": f"S{n}E{e}",
                        "overview": "An episode.",
                        "air_date": "1963-11-23",
                        "runtime": 25,
                        "still_path": f"/still-{n}-{e}.jpg",
                    }
                    for e in (1, 2)
                ],
            }
        return Response(payload)


def test_fetch_batches_season_appends_in_twenties(app, monkeypatch):
    with app.app_context():
        series = make_tv_series("Doctor Who (1963)", tmdb_id=121)
        monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
        fake = FakeTMDb(season_count=25)
        import app.models

        monkeypatch.setattr(app.models.requests, "get", fake.get)

        info = series.tmdb_tv_fetch(121)

        # The base call asks for the series-wide credit aggregate (#78
        # step 4) alongside the original appended blocks
        assert fake.appends[0] == "aggregate_credits,external_ids,keywords"

        season_appends = [a for a in fake.appends if "season/" in a]
        assert len(season_appends) == 2
        assert len(season_appends[0].split(",")) == 20
        assert len(season_appends[1].split(",")) == 5
        assert info["season/0"]["episodes"][0]["name"] == "S0E1"
        assert info["season/24"]["season_number"] == 24


def test_apply_syncs_episode_rows_per_fetched_season(app):
    with app.app_context():
        series = make_tv_series("Columbo", tmdb_id=1041)
        untouched = make_tv_episode(series, 2, 1, title="Stays put")
        db.session.commit()

        series.tmdb_tv_apply(
            {
                "id": 1041,
                "season/1": {
                    "season_number": 1,
                    "episodes": [
                        {
                            "id": 101,
                            "episode_number": 1,
                            "name": "Murder by the Book",
                            "air_date": "1971-09-15",
                            "runtime": 76,
                            "still_path": "/book.jpg",
                        },
                        {"id": 102, "episode_number": 2, "name": "Death Lends a Hand"},
                    ],
                },
            }
        )
        db.session.commit()

        assert series.episodes.filter_by(season=1).count() == 2
        first = series.episodes.filter_by(season=1, episode=1).one()
        assert first.title == "Murder by the Book"
        assert first.runtime == 76
        assert first.air_date.year == 1971
        assert first.tmdb_data_as_of is not None

        # Second refresh: episode 2 renamed, episode 1 gone upstream;
        # season 2 wasn't fetched so its row must survive
        series.tmdb_tv_apply(
            {
                "id": 1041,
                "season/1": {
                    "season_number": 1,
                    "episodes": [
                        {"id": 102, "episode_number": 2, "name": "Renamed"},
                    ],
                },
            }
        )
        db.session.commit()

        assert series.episodes.filter_by(season=1).count() == 1
        assert series.episodes.filter_by(season=1, episode=2).one().title == "Renamed"
        assert series.episodes.filter_by(season=2, episode=1).one().id == untouched.id


def test_apply_replaces_cast_and_crew_from_aggregate_credits(app):
    with app.app_context():
        series = make_tv_series("Doctor Who (1963)", tmdb_id=121)
        series.tmdb_tv_apply(
            {
                "id": 121,
                "aggregate_credits": {
                    "cast": [
                        {
                            "id": 30696,
                            "name": "William Hartnell",
                            "gender": 2,
                            "profile_path": "/hartnell.jpg",
                            "order": 0,
                            "roles": [
                                {"character": "The Doctor", "episode_count": 134},
                                {
                                    "character": "The Abbot of Amboise",
                                    "episode_count": 4,
                                },
                            ],
                        }
                    ],
                    "crew": [
                        {
                            "id": 1219912,
                            "name": "Verity Lambert",
                            "gender": 1,
                            "profile_path": None,
                            "department": "Production",
                            "jobs": [{"job": "Producer", "episode_count": 86}],
                        }
                    ],
                },
            }
        )
        db.session.commit()

        assert series.cast.count() == 2
        doctor = series.cast.filter_by(character="The Doctor").one()
        assert doctor.starring.name == "William Hartnell"
        assert doctor.billing_order == 0
        assert doctor.episode_count == 134
        producer = series.crew.one()
        assert producer.job == "Producer"
        assert producer.episode_count == 86

        # A refresh replaces wholesale: a role gone upstream disappears
        series.tmdb_tv_apply(
            {
                "id": 121,
                "aggregate_credits": {
                    "cast": [
                        {
                            "id": 30696,
                            "name": "William Hartnell",
                            "order": 0,
                            "roles": [
                                {"character": "The Doctor", "episode_count": 134}
                            ],
                        }
                    ],
                    "crew": [],
                },
            }
        )
        db.session.commit()
        assert series.cast.count() == 1
        assert series.crew.count() == 0


def test_apply_dedupes_credits_the_way_mysql_collates(app):
    """Real payloads carry role variants distinct to Python but equal
    under utf8mb4_general_ci — 'Self - Bee farmer' vs 'Self - Bee
    Farmer', 'Curare' vs 'Curaré' — which 1062'd the first live
    backfill. One row per collation-equal role must survive."""

    with app.app_context():
        series = make_tv_series("Clarkson's Farm", tmdb_id=95396)
        series.tmdb_tv_apply(
            {
                "id": 95396,
                "aggregate_credits": {
                    "cast": [
                        {
                            "id": 4719914,
                            "name": "A Farmer",
                            "order": 522,
                            "roles": [
                                {"character": "Self - Bee farmer", "episode_count": 1},
                                {"character": "Self - Bee Farmer", "episode_count": 1},
                                {"character": "Curare (voice)", "episode_count": 2},
                                {"character": "Curaré (voice)", "episode_count": 2},
                            ],
                        }
                    ],
                    "crew": [
                        {
                            "id": 555,
                            "name": "Someone",
                            "department": "Art",
                            "jobs": [
                                {"job": "Storyboard Artist", "episode_count": 5},
                                {"job": "Storyboard artist", "episode_count": 5},
                            ],
                        }
                    ],
                },
            }
        )
        db.session.commit()

        assert series.cast.count() == 2
        assert series.crew.count() == 1


def test_tv_page_shows_billed_cast(app, admin_client):
    with app.app_context():
        series = make_tv_series("Columbo", tmdb_id=1041)
        series.tmdb_tv_apply(
            {
                "id": 1041,
                "aggregate_credits": {
                    "cast": [
                        {
                            "id": 4886,
                            "name": "Peter Falk",
                            "order": 0,
                            "roles": [
                                {"character": "Lt. Columbo", "episode_count": 69}
                            ],
                        }
                    ],
                    "crew": [],
                },
            }
        )
        db.session.commit()
        series_id = series.id

    response = admin_client.get(f"/tv/{series_id}")
    assert response.status_code == 200
    assert b"Peter Falk" in response.data
    assert b"Lt. Columbo" in response.data


def test_apply_heals_stale_season_counts(app):
    with app.app_context():
        series = make_tv_series("Bob's Burgers", tmdb_id=32726)
        stale = TMDBSeason(id=5555, season_number=13, episode_count=1)
        db.session.add(stale)
        series.seasons.append(stale)
        db.session.commit()

        series.tmdb_tv_apply(
            {
                "id": 32726,
                "seasons": [
                    {
                        "id": 5555,
                        "season_number": 13,
                        "episode_count": 22,
                        "name": "Season 13",
                    }
                ],
            }
        )
        db.session.commit()

        assert db.session.get(TMDBSeason, 5555).episode_count == 22


def test_episode_round_trips_through_its_series(app):
    with app.app_context():
        series = make_tv_series("Doctor Who (1963)")
        make_tv_episode(
            series,
            1,
            1,
            title="An Unearthly Child",
            overview="Two teachers follow a strange pupil home.",
            runtime=25,
        )
        db.session.commit()

        stored = series.episodes.filter_by(season=1, episode=1).one()
        assert stored.title == "An Unearthly Child"
        assert stored.series is series


def test_slot_is_unique_per_series(app):
    with app.app_context():
        series = make_tv_series("Columbo")
        other = make_tv_series("The Rockford Files")
        make_tv_episode(series, 1, 1, title="Murder by the Book")

        # The same slot on a different series is fine
        make_tv_episode(other, 1, 1, title="The Kirkoff Case")
        db.session.commit()

        with pytest.raises(IntegrityError):
            make_tv_episode(series, 1, 1, title="Duplicate")
        db.session.rollback()


def test_episodes_cascade_with_their_series(app):
    with app.app_context():
        series = make_tv_series("K-9 and Company")
        make_tv_episode(series, 1, 1, title="A Girl's Best Friend")
        db.session.commit()

        db.session.delete(series)
        db.session.commit()
        assert TVEpisode.query.count() == 0
