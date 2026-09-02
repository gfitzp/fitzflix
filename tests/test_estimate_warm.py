"""Test the nightly estimate pre-warming task.

The task warms the careers of the affinity people and the TMDB charts
into the enrichment cache under a fetch budget. The tests cover the
rolling cursors, the month-long TTL, and the pre-scored tmdb overlay.
The overlay lets the tiles paint estimates without a wait."""

import json

from tests.factories import make_movie

NIGHTLY_PROFILE = {
    "affinities": {
        "genre:35": {"class": "genre", "label": "Comedy", "count": 3, "score": 0.5},
        "actor:9001": {
            "class": "actor",
            "label": "Warm Actor",
            "count": 4,
            "score": 0.9,
        },
        "director:9002": {
            "class": "director",
            "label": "Warm Director",
            "count": 2,
            "score": 0.4,
        },
        # A person who acts AND directs counts 1 time, at the best score
        "director:9001": {
            "class": "director",
            "label": "Warm Actor",
            "count": 1,
            "score": 0.2,
        },
    },
    "movies": 5,
    "calibration": {
        "scores": [0.0, 0.1, 0.2, 0.3],
        "stars": [1.0, 2.0, 4.0, 4.5],
    },
}


class FakeTMDB:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


def install_warm_fakes(app, monkeypatch, fetched):
    """Install a fake tmdb_get for the warm task and the enrichment module.

    The career of person 9001 is the films 700001-700003. The career of
    person 9002 is the films 700003-700004 (an overlap). Each chart
    serves 1 page. The fake records each /movie/{id} enrichment in
    `fetched`."""

    import app.estimate_warm as estimate_warm
    import app.streaming_rail as streaming_rail

    def fake_tmdb_get(url, params=None, timeout=None):
        if "/person/9001/movie_credits" in url:
            return FakeTMDB(
                {
                    "cast": [{"id": 700001}, {"id": 700002}],
                    "crew": [{"id": 700003, "job": "Director"}],
                }
            )
        if "/person/9002/movie_credits" in url:
            return FakeTMDB(
                {
                    "cast": [],
                    "crew": [
                        {"id": 700003, "job": "Director"},
                        {"id": 700004, "job": "Director"},
                        {"id": 700005, "job": "Best Boy"},
                    ],
                }
            )
        if url.endswith("/movie/popular"):
            return FakeTMDB(
                {"results": [{"id": 700010}, {"id": 700011}], "total_pages": 1}
            )
        if url.endswith("/movie/top_rated"):
            return FakeTMDB({"results": [{"id": 700020}], "total_pages": 1})
        for tmdb_id in (700001, 700002, 700003, 700004, 700010, 700011, 700020):
            if url.endswith(f"/movie/{tmdb_id}"):
                fetched.append(tmdb_id)
                return FakeTMDB(
                    {
                        "id": tmdb_id,
                        "title": f"Warm Film {tmdb_id}",
                        "release_date": "1994-05-01",
                        "original_language": "en",
                        "genres": [{"id": 35, "name": "Comedy"}],
                        "keywords": {"keywords": []},
                        "credits": {"cast": [], "crew": []},
                    }
                )
        return FakeTMDB({"results": []})

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(estimate_warm, "tmdb_get", fake_tmdb_get)
    monkeypatch.setattr(streaming_rail, "tmdb_get", fake_tmdb_get)


def plant_profile(app, user_id):
    app.redis.set(f"fitzflix:recs:profile:{user_id}", json.dumps(NIGHTLY_PROFILE))


def admin_id(app):
    from app.models import User

    with app.app_context():
        return User.query.filter_by(admin=True).first().id


def test_affinity_people_rank_and_dedupe(app):
    from app.estimate_warm import _affinity_people

    people = _affinity_people(NIGHTLY_PROFILE)
    assert people == [9001, 9002]


def test_warm_task_caches_scores_and_rolls_cursors(app, monkeypatch):
    """Test that one night's run warms, pre-scores, and rolls the cursors.

    The run warms each candidate payload with the long TTL. It pre-scores
    the films without a record into the tmdb overlay. The films with a
    record are not included, because the movie lane owns them. The run
    rolls the cursors. Thus, the next night continues further on."""

    from app import db
    from app.estimate_warm import CURSORS_KEY, warm_estimates
    from app.recommendations import TMDB_PATCH_SCORES_KEY

    user_id = admin_id(app)
    plant_profile(app, user_id)
    with app.app_context():
        make_movie("Warm Recorded", 1994, tmdb_id=700010)
        db.session.commit()

    fetched = []
    install_warm_fakes(app, monkeypatch, fetched)

    assert warm_estimates() is True

    # The run caches each candidate payload 1 time. Film 700003 is in
    # both careers, but the run fetches it 1 time. Each payload has the
    # month TTL

    assert sorted(set(fetched)) == sorted(fetched)
    for tmdb_id in (700001, 700002, 700003, 700004, 700010, 700011, 700020):
        key = f"fitzflix:tmdb:movie:{tmdb_id}:enriched"
        assert app.redis.exists(key)
        assert app.redis.ttl(key) > 7 * 86400

    # The overlay has pre-scores only for the films without a record

    overlay = {
        field.decode(): float(value)
        for field, value in app.redis.hgetall(
            TMDB_PATCH_SCORES_KEY.format(user_id=user_id)
        ).items()
    }
    for tmdb_id in ("700001", "700002", "700003", "700004", "700011", "700020"):
        assert tmdb_id in overlay and overlay[tmdb_id] > 0
    assert "700010" not in overlay

    # The cursors rolled. The run warmed both people and wrapped the
    # cursor to the start. The run used both single-page charts fully
    # and wrapped their cursors to 0

    cursors = {
        field.decode(): int(value)
        for field, value in app.redis.hgetall(CURSORS_KEY).items()
    }
    assert cursors[f"people:{user_id}"] == 0
    assert cursors["chart:popular"] == 0
    assert cursors["chart:top_rated"] == 0

    # A tile batch the next morning reads finished numbers. It does no
    # fetch and no live scoring. The estimate is already on the shelf

    fetched.clear()
    with app.test_client() as client:
        with client.session_transaction() as session:
            session["_user_id"] = str(user_id)
            session["_fresh"] = True
        payload = client.get("/movie_states?tmdb_ids=700001,700020").get_json()
    assert payload["tmdb"]["700001"]["estimated"] is not None
    assert payload["tmdb"]["700020"]["estimated"] is not None
    assert fetched == []


def test_warm_task_respects_the_fetch_budget(app, monkeypatch):
    """Test that the nightly budget limits the new enrichment fetches.

    The candidates after the budget wait for the roll of the next night."""

    import app.estimate_warm as estimate_warm

    user_id = admin_id(app)
    plant_profile(app, user_id)

    fetched = []
    install_warm_fakes(app, monkeypatch, fetched)
    monkeypatch.setattr(estimate_warm, "WARM_FETCH_BUDGET", 2)

    assert estimate_warm.warm_estimates() is True
    assert len(fetched) == 2
