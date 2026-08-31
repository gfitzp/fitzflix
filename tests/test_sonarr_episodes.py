"""Sonarr-sourced episode data (#162): the nightly sync's row
rebuilds, source flips, and wipe guards; the TMDB refresh's hands-off
rule for Sonarr-owned rows; and the render and search gates that
trust Sonarr titles on numbering-suspect series."""

import json

from app import db
from app.sonarr_episodes import (
    sync_sonarr_episodes,
    title_is_placeholder,
)
from app.tv_validation import VALIDATION_KEY

from tests.factories import make_tv_episode, make_tv_file, make_tv_series


def _suspect_verdict():
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


def _sonarr(monkeypatch, payloads):
    monkeypatch.setattr(
        "app.sonarr_episodes._sonarr_get", lambda path: payloads.get(path)
    )


def test_placeholder_titles_are_recognized():
    assert title_is_placeholder(None)
    assert title_is_placeholder("")
    assert title_is_placeholder("Episode 12")
    assert title_is_placeholder("Season 1, Episode 1")
    assert title_is_placeholder("TBA")
    assert not title_is_placeholder("The Kirkoff Case")
    assert not title_is_placeholder("Episode 12: The Reckoning")


def test_sync_rebuilds_rows_and_flips_the_source(app, monkeypatch):
    with app.app_context():
        series = make_tv_series("Carson", tvdb_id=70334)

        # A TMDB leftover in a slot Sonarr doesn't list must go — it
        # follows the wrong numbering for these files

        make_tv_episode(series, 11, 99, title="TMDB Leftover")
        db.session.commit()
        series_id = series.id

        _sonarr(
            monkeypatch,
            {
                "/api/v3/series": [{"id": 5, "tvdbId": 70334}],
                "/api/v3/episode?seriesId=5": [
                    {
                        "seasonNumber": 11,
                        "episodeNumber": 1,
                        "title": "10th Anniversary Show",
                        "overview": "Jack Benny, Joey Bishop.",
                        "airDate": "1972-10-01",
                        "runtime": 80,
                    },
                    {"seasonNumber": 11, "episodeNumber": 2, "title": "TBA"},
                    {"seasonNumber": 12, "episodeNumber": 1, "title": "Bob Hope"},
                ],
            },
        )

        assert sync_sonarr_episodes() is True

        # The task commits in its own app context's session

        db.session.expire_all()
        series = db.session.get(type(series), series_id)
        assert series.episode_source == "sonarr"
        assert series.episodes.count() == 2
        first = series.episodes.filter_by(season=11, episode=1).one()
        assert first.title == "10th Anniversary Show"
        assert first.air_date.year == 1972
        assert first.runtime == 80
        assert first.tmdb_episode_id is None
        assert series.episodes.filter_by(season=11, episode=2).count() == 0
        assert series.episodes.filter_by(season=11, episode=99).count() == 0


def test_sync_reverts_a_series_sonarr_no_longer_manages(app, monkeypatch):
    with app.app_context():
        series = make_tv_series("Dropped", tvdb_id=111, episode_source="sonarr")
        make_tv_episode(series, 1, 1, title="TVDB-Numbered Row")
        db.session.commit()
        series_id = series.id

        _sonarr(monkeypatch, {"/api/v3/series": []})

        assert sync_sonarr_episodes() is True

        db.session.expire_all()
        series = db.session.get(type(series), series_id)
        assert series.episode_source == "tmdb"
        assert (
            series.episodes.count() == 0
        ), "TVDB-numbered rows would mislabel under TMDB numbering"


def test_sync_keeps_tmdb_rows_when_sonarr_has_no_usable_titles(app, monkeypatch):
    with app.app_context():

        # The Hour's shape: TVDB itself only titles "Episode N"

        series = make_tv_series("The Hour (2011)", tvdb_id=248503)
        make_tv_episode(series, 1, 1, title="A Real TMDB Title")
        db.session.commit()
        series_id = series.id

        _sonarr(
            monkeypatch,
            {
                "/api/v3/series": [{"id": 7, "tvdbId": 248503}],
                "/api/v3/episode?seriesId=7": [
                    {"seasonNumber": 1, "episodeNumber": 1, "title": "Episode 1"},
                    {"seasonNumber": 1, "episodeNumber": 2, "title": "Episode 2"},
                ],
            },
        )

        assert sync_sonarr_episodes() is True

        db.session.expire_all()
        series = db.session.get(type(series), series_id)
        assert series.episode_source == "tmdb"
        assert series.episodes.one().title == "A Real TMDB Title"


def test_sync_keeps_sonarr_rows_when_the_fetch_fails(app, monkeypatch):
    with app.app_context():
        series = make_tv_series("Carson", tvdb_id=70334, episode_source="sonarr")
        make_tv_episode(series, 11, 1, title="10th Anniversary Show")
        db.session.commit()
        series_id = series.id

        _sonarr(monkeypatch, {"/api/v3/series": [{"id": 5, "tvdbId": 70334}]})

        assert sync_sonarr_episodes() is True

        db.session.expire_all()
        series = db.session.get(type(series), series_id)
        assert series.episode_source == "sonarr"
        assert series.episodes.one().title == "10th Anniversary Show"


def test_sync_keeps_everything_when_the_series_list_is_unavailable(app, monkeypatch):
    with app.app_context():
        series = make_tv_series("Carson", tvdb_id=70334, episode_source="sonarr")
        make_tv_episode(series, 11, 1, title="10th Anniversary Show")
        db.session.commit()
        series_id = series.id

        _sonarr(monkeypatch, {})

        assert sync_sonarr_episodes() is True

        db.session.expire_all()
        series = db.session.get(type(series), series_id)
        assert series.episode_source == "sonarr"
        assert series.episodes.count() == 1


def test_tmdb_apply_leaves_sonarr_owned_rows_alone(app):
    with app.app_context():
        series = make_tv_series(
            "Carson", tmdb_id=2261, tvdb_id=70334, episode_source="sonarr"
        )
        make_tv_episode(series, 11, 1, title="10th Anniversary Show")
        db.session.commit()

        series.tmdb_tv_apply(
            {
                "id": 2261,
                "season/11": {
                    "season_number": 11,
                    "episodes": [
                        {"id": 900, "episode_number": 1, "name": "Episode 1"},
                        {"id": 901, "episode_number": 2, "name": "Episode 2"},
                    ],
                },
            }
        )
        db.session.commit()

        assert series.episodes.count() == 1
        assert series.episodes.one().title == "10th Anniversary Show"


def test_tmdb_clear_resets_the_episode_source(app):
    with app.app_context():
        series = make_tv_series(
            "Carson", tmdb_id=2261, tvdb_id=70334, episode_source="sonarr"
        )
        make_tv_episode(series, 11, 1, title="10th Anniversary Show")
        db.session.commit()

        series.tmdb_tv_clear()
        db.session.commit()

        assert series.episode_source == "tmdb"
        assert series.episodes.count() == 0


def test_season_page_trusts_sonarr_titles_on_a_suspect_series(app, admin_client):
    with app.app_context():
        series = make_tv_series("Carson", tvdb_id=70334, episode_source="sonarr")
        make_tv_episode(
            series,
            11,
            1,
            title="10th Anniversary Show",
            overview="Jack Benny, Joey Bishop.",
        )
        make_tv_file(series, 11, 1, "DVD")
        db.session.commit()
        series_id = series.id

        app.redis.hset(VALIDATION_KEY, str(series_id), _suspect_verdict())

    response = admin_client.get(f"/tv/{series_id}/11")
    assert response.status_code == 200
    assert b"10th Anniversary Show" in response.data
    assert b"Jack Benny" in response.data


def test_search_finds_sonarr_titles_on_a_suspect_series(app, admin_client):
    with app.app_context():
        series = make_tv_series("Carson", tvdb_id=70334, episode_source="sonarr")
        make_tv_episode(series, 11, 30, title="Muhammad Ali, Harry Chapin")
        db.session.commit()
        app.redis.hset(VALIDATION_KEY, str(series.id), _suspect_verdict())

    response = admin_client.get("/search?q=Muhammad+Ali")
    assert response.status_code == 200
    assert b"Muhammad Ali, Harry Chapin" in response.data


def test_report_page_badges_sonarr_sourced_series(app, admin_client):
    with app.app_context():
        series = make_tv_series("Carson", tvdb_id=70334, episode_source="sonarr")
        db.session.commit()
        app.redis.hset(VALIDATION_KEY, str(series.id), _suspect_verdict())

    response = admin_client.get("/maintenance/tv-titles")
    assert response.status_code == 200
    assert b"Sonarr episodes" in response.data
