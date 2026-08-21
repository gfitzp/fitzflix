"""#78 step 6 surfaces: season-page episode guide, episode search,
people-page TV credits, the filmography Television section, and the
tv-page meta line."""

import json

from app import db
from app.models import TMDBCredit, TVCast
from app.tv_validation import VALIDATION_KEY

from tests.factories import make_tv_episode, make_tv_file, make_tv_series


def _suspect_verdict(series_id):
    return json.dumps(
        {
            "name": "whatever",
            "compared": 10,
            "agreed": 0,
            "rate": 0.0,
            "suspect": True,
            "examples": [],
        }
    )


def test_season_page_shows_guide_and_title_column(app, admin_client):
    with app.app_context():
        series = make_tv_series("Doctor Who (1963)", tmdb_id=121)
        make_tv_episode(
            series,
            1,
            1,
            title="An Unearthly Child",
            overview="Two teachers follow a strange pupil home.",
        )
        make_tv_episode(series, 1, 2, title="The Cave of Skulls")
        make_tv_file(series, 1, 1, "DVD")
        db.session.commit()
        series_id = series.id

    response = admin_client.get(f"/tv/{series_id}/1")
    assert response.status_code == 200
    assert b"Episodes" in response.data
    assert b"An Unearthly Child" in response.data
    assert b"The Cave of Skulls" in response.data
    assert b"In library" in response.data


def test_season_page_suspect_series_stays_plain(app, admin_client):
    with app.app_context():
        series = make_tv_series("Cursed Show", tmdb_id=999)
        make_tv_episode(series, 1, 1, title="Wrong Title")
        make_tv_file(series, 1, 1, "DVD")
        db.session.commit()
        series_id = series.id
        app.redis.hset(VALIDATION_KEY, str(series_id), _suspect_verdict(series_id))

    response = admin_client.get(f"/tv/{series_id}/1")
    assert response.status_code == 200
    assert b"Wrong Title" not in response.data


def test_search_finds_episode_titles_but_not_suspect_ones(app, admin_client):
    with app.app_context():
        good = make_tv_series("Columbo", tmdb_id=1041)
        make_tv_episode(good, 1, 1, title="Murder by the Book")
        cursed = make_tv_series("Cursed Show", tmdb_id=999)
        make_tv_episode(cursed, 1, 1, title="Murder by the Wrong Book")
        db.session.commit()
        good_id = good.id
        app.redis.hset(VALIDATION_KEY, str(cursed.id), _suspect_verdict(cursed.id))

    response = admin_client.get("/search?q=Murder+by+the")
    assert response.status_code == 200
    assert b"Murder by the Book" in response.data
    assert f"/tv/{good_id}/1".encode() in response.data
    assert b"Murder by the Wrong Book" not in response.data


def test_people_page_counts_tv_credits(app, admin_client):
    with app.app_context():
        falk = TMDBCredit(id=4886, name="Peter Falk")
        db.session.add(falk)
        columbo = make_tv_series("Columbo", tmdb_id=1041)
        rockford = make_tv_series("The Rockford Files", tmdb_id=1042)
        db.session.add(
            TVCast(tv_id=columbo.id, credit_id=4886, character="Lt. Columbo")
        )
        db.session.add(TVCast(tv_id=rockford.id, credit_id=4886, character="A Cop"))
        db.session.commit()

    response = admin_client.get("/people?q=Falk")
    assert response.status_code == 200
    assert b"Peter Falk" in response.data
    assert b"2 titles" in response.data


def test_filmography_shows_television_section(app, admin_client, monkeypatch):
    with app.app_context():
        monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
        series = make_tv_series("Columbo", tmdb_id=1041)
        make_tv_file(series, 1, 1, "DVD")
        db.session.add(TMDBCredit(id=4886, name="Peter Falk"))
        db.session.commit()
        series_id = series.id

        # Day-cached payloads stand in for TMDb: the route reads the
        # cache before ever touching the network
        app.redis.set(
            "fitzflix:tmdb:person:4886:details", json.dumps({"name": "Peter Falk"})
        )
        app.redis.set(
            "fitzflix:tmdb:person:4886:credits",
            json.dumps({"cast": [], "crew": []}),
        )
        app.redis.set(
            "fitzflix:tmdb:person:4886:tv_credits",
            json.dumps(
                {
                    "cast": [
                        {
                            "id": 1041,
                            "name": "Columbo",
                            "first_air_date": "1971-09-15",
                            "character": "Lt. Columbo",
                            "episode_count": 69,
                            "poster_path": None,
                        }
                    ],
                    "crew": [],
                }
            ),
        )

    response = admin_client.get("/library/movie?credit=4886")
    assert response.status_code == 200
    assert b"Television" in response.data
    assert b"Columbo" in response.data
    assert f"/tv/{series_id}".encode() in response.data
    assert b"Lt. Columbo" in response.data


def test_self_appearances_drop_but_selfridge_survives(app, admin_client, monkeypatch):
    """The Television section's self-filter matches at word boundaries:
    talk-show and awards-night rows ("Self", "Self - Host", "Herself")
    are dropped, but a genuine character that merely contains the
    letters — Harry Selfridge — is a real acting credit and stays."""

    import app.main.library as library

    with app.app_context():
        monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
        db.session.add(TMDBCredit(id=287, name="Jeremy Piven"))
        db.session.commit()

        # Person details and movie credits read cache-first; only the
        # tv_credits fetch reaches the patched network call
        app.redis.set(
            "fitzflix:tmdb:person:287:details", json.dumps({"name": "Jeremy Piven"})
        )
        app.redis.set(
            "fitzflix:tmdb:person:287:credits",
            json.dumps({"cast": [], "crew": []}),
        )

        payload = {
            "cast": [
                {
                    "id": 33217,
                    "name": "Mr Selfridge",
                    "first_air_date": "2013-01-06",
                    "character": "Harry Selfridge",
                    "episode_count": 40,
                    "poster_path": None,
                },
                {
                    "id": 2,
                    "name": "Talk Show",
                    "first_air_date": "2010-01-01",
                    "character": "Self",
                    "episode_count": 3,
                    "poster_path": None,
                },
                {
                    "id": 3,
                    "name": "Award Night",
                    "first_air_date": "2011-01-01",
                    "character": "Self - Host",
                    "episode_count": 1,
                    "poster_path": None,
                },
                {
                    "id": 4,
                    "name": "Retrospective",
                    "first_air_date": "2012-01-01",
                    "character": "Himself (archive footage)",
                    "episode_count": 2,
                    "poster_path": None,
                },
            ],
            "crew": [],
        }

        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return payload

        monkeypatch.setattr(library, "tmdb_get", lambda *a, **kw: FakeResponse())

    response = admin_client.get("/library/movie?credit=287")
    assert response.status_code == 200
    assert b"Mr Selfridge" in response.data
    assert b"Harry Selfridge" in response.data
    assert b"Talk Show" not in response.data
    assert b"Award Night" not in response.data
    assert b"Retrospective" not in response.data


def test_tv_page_meta_line(app, admin_client):
    from datetime import datetime

    with app.app_context():
        series = make_tv_series(
            "Doctor Who (1963)",
            tmdb_id=121,
            tmdb_first_air_date=datetime(1963, 11, 23),
            tmdb_last_air_date=datetime(1989, 12, 6),
            tmdb_number_of_seasons=26,
            tmdb_number_of_episodes=694,
        )
        db.session.commit()
        series_id = series.id

    response = admin_client.get(f"/tv/{series_id}")
    assert response.status_code == 200
    assert "1963–1989".encode() in response.data
    assert b"26 seasons, 694 episodes" in response.data
