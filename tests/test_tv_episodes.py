"""The TMDB TV refresh's series-level apply: aggregate credits,
season-count healing, and the billed-cast surface. Episode metadata is
deliberately not stored from any source."""

from app import db
from app.models import TMDBSeason

from tests.factories import make_tv_series


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

        # A refresh replaces wholesale: a role gone upstream disappears.
        # An EMPTY crew list, though, keeps the stored rows (#252) — an
        # every-credit wipe is likelier a glitched payload than TMDB
        # truly dropping the whole department (the Aug 22 2026 shape)
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
        assert series.crew.count() == 1


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


def test_apply_skips_malformed_credit_entries(app, caplog):
    """The 2026-08-22 overnight refresh: TMDB briefly served a bare list
    where a cast member's role object belongs, and one such entry aborted
    the whole apply. A malformed role, job, or person now logs its
    fragment and is skipped; everything well-formed still lands."""

    with app.app_context():
        series = make_tv_series("Futurama", tmdb_id=615)
        with caplog.at_level("WARNING"):
            series.tmdb_tv_apply(
                {
                    "id": 615,
                    "aggregate_credits": {
                        "cast": [
                            {
                                "id": 1,
                                "name": "Billy West",
                                "order": 0,
                                "roles": [
                                    ["Fry", 140],
                                    {"character": "Fry (voice)", "episode_count": 140},
                                ],
                            },
                            ["not", "a", "person"],
                        ],
                        "crew": [
                            {
                                "id": 2,
                                "name": "Matt Groening",
                                "department": "Writing",
                                "jobs": [
                                    {"job": "Creator", "episode_count": 140},
                                    "Executive Producer",
                                ],
                            }
                        ],
                    },
                }
            )
            db.session.commit()

        assert [c.character for c in series.cast] == ["Fry (voice)"]
        assert [c.job for c in series.crew] == ["Creator"]
        warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
        assert any(
            "cast 1 role entry is not an object" in w and "['Fry', 140]" in w
            for w in warnings
        )
        assert any("cast entry is not an object" in w for w in warnings)
        assert any("crew 2 job entry is not an object" in w for w in warnings)
