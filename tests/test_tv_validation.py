"""Episode-title validation (#78 step 5): fuzzy agreement, per-series
verdicts against Plex's titles, and the maintenance report."""

import json

from app import db
from app.tv_validation import (
    VALIDATION_KEY,
    compute_validation,
    series_is_suspect,
    titles_agree,
)

from tests.factories import make_tv_episode, make_tv_file, make_tv_series


def test_titles_agree_through_formatting_noise():
    assert titles_agree("The Kirkoff Case", "Kirkoff Case")
    assert titles_agree("Murder by the Book", "Murder By The Book!")
    assert titles_agree("An Unearthly Child", "An Unearthly Child (1)")
    assert not titles_agree("Photo Gallery", "Graeme Harper Featurette")
    assert not titles_agree("", "Anything")


def test_compute_flags_shifted_series_and_trusts_agreeing_ones(app):
    with app.app_context():
        good = make_tv_series("Columbo", tmdb_id=1041)
        shifted = make_tv_series("Cursed Show", tmdb_id=999)
        plex_titles = {}

        for e in range(1, 7):
            make_tv_episode(good, 1, e, title=f"Case {e}")
            file = make_tv_file(good, 1, e, "DVD")
            plex_titles[file.basename] = f"Case {e}"

        # Every slot's Plex title is the NEXT episode's TMDb title —
        # the off-by-one shape of a numbering divergence. Distinct
        # wording per episode: numbered titles ("Part 1"/"Part 2")
        # fuzzy-match each other, a known detection limit
        titles = [
            "Winter Lake",
            "Crimson Tide",
            "Silent Hill",
            "Golden Gate",
            "Broken Arrow",
            "Velvet Morning",
            "Iron Summit",
        ]
        for e in range(1, 7):
            make_tv_episode(shifted, 1, e, title=titles[e - 1])
            file = make_tv_file(shifted, 1, e, "DVD")
            plex_titles[file.basename] = titles[e]
        db.session.commit()

        results = compute_validation(plex_titles)

        assert results[good.id]["suspect"] is False
        assert results[good.id]["rate"] == 1.0
        assert results[shifted.id]["suspect"] is True
        assert results[shifted.id]["examples"]
        example = results[shifted.id]["examples"][0]
        assert example["plex"] != example["tmdb"]


def test_too_few_comparisons_never_suspect(app):
    with app.app_context():
        series = make_tv_series("Sparse", tmdb_id=555)
        make_tv_episode(series, 1, 1, title="Alpha")
        file = make_tv_file(series, 1, 1, "DVD")
        db.session.commit()

        results = compute_validation({file.basename: "Omega"})
        assert results[series.id]["compared"] == 1
        assert results[series.id]["suspect"] is False


def test_edition_files_and_rowless_slots_are_skipped(app):
    with app.app_context():
        series = make_tv_series("Doctor Who (1963)", tmdb_id=121)
        edition_file = make_tv_file(series, 0, 90004, "DVD", edition="Planet of Giants")
        rowless_file = make_tv_file(series, 5, 1, "DVD")
        db.session.commit()

        results = compute_validation(
            {
                edition_file.basename: "Planet of Giants",
                rowless_file.basename: "Something",
            }
        )
        assert series.id not in results


def test_multi_episode_span_agrees_on_any_member(app):
    with app.app_context():
        series = make_tv_series("Two-Parter", tmdb_id=777)
        make_tv_episode(series, 1, 1, title="Part One")
        make_tv_episode(series, 1, 2, title="Part Two")
        file = make_tv_file(series, 1, 1, "DVD", last_episode=2)
        db.session.commit()

        results = compute_validation({file.basename: "Part Two"})
        assert results[series.id]["agreed"] == 1


def test_suspect_gate_and_report_read_stored_verdicts(app, admin_client):
    with app.app_context():
        series = make_tv_series("Cursed Show", tmdb_id=999)
        db.session.commit()
        series_id = series.id
        app.redis.hset(
            VALIDATION_KEY,
            str(series_id),
            json.dumps(
                {
                    "name": "Cursed Show",
                    "compared": 10,
                    "agreed": 1,
                    "rate": 0.1,
                    "suspect": True,
                    "examples": [
                        {"season": 1, "episode": 2, "plex": "Real", "tmdb": "Wrong"}
                    ],
                }
            ),
        )
        assert series_is_suspect(series_id) is True
        assert series_is_suspect(series_id + 1) is False

    response = admin_client.get("/maintenance/tv-titles")
    assert response.status_code == 200
    assert b"Cursed Show" in response.data
    assert b"Numbering suspect" in response.data
    assert b"Wrong" in response.data
