"""Test the series-level apply of the TMDB TV refresh.

This covers the aggregate credits, the season-count repair, and the
billed-cast surface. By design, Fitzflix does not store episode
metadata from any source."""

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

        # A refresh replaces all the rows. A role that is gone upstream
        # disappears. But an EMPTY crew list keeps the stored rows (#252).
        # A deletion of every credit is more probably a bad payload than a
        # TMDB removal of the whole department (the 2026-08-22 shape)
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
    """Test that one row survives for each collation-equal role.

    Real payloads carry role variants that are different to Python but
    equal under utf8mb4_general_ci. Examples: 'Self - Bee farmer' and
    'Self - Bee Farmer', 'Curare' and 'Curaré'. These caused a 1062
    error in the first live backfill."""

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
    """Test that a malformed credit entry does not abort the apply.

    In the overnight refresh of 2026-08-22, TMDB served a bare list for a
    short time where the role object of a cast member belongs. One such
    entry aborted the whole apply. Now a malformed role, job, or person
    logs its fragment, and Fitzflix skips it. All the well-formed entries
    still go into the database."""

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
