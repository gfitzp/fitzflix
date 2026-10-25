"""Test the shared Radarr and Sonarr client of app/arr_client.py."""


def test_the_rescan_command_of_each_app(app):
    from app.arr_client import RADARR, SONARR, rescan_command

    assert rescan_command(RADARR, "3") == {"name": "RefreshMovie", "movieIds": [3]}
    assert rescan_command(SONARR, 7) == {"name": "RescanSeries", "seriesId": 7}


def test_the_wrappers_keep_the_old_names(app):
    """The stored rq job names and the old import paths still resolve."""

    from app import arr_client, radarr_push, sonarr_push

    assert radarr_push.RadarrError is arr_client.RadarrError
    assert sonarr_push.SonarrError is arr_client.SonarrError
    assert radarr_push.fallback_rename is arr_client.fallback_rename
    assert arr_client.RADARR.adopt_task == "app.radarr_push.adopt_download_task"
    assert arr_client.SONARR.adopt_task == "app.sonarr_push.adopt_episode_task"
    assert callable(radarr_push.adopt_download_task)
    assert callable(sonarr_push.adopt_episode_task)


def test_a_series_rename_points_sonarr_at_the_new_folder_and_rescans(app, monkeypatch):
    """The rename finds the series by tvdbId, PUTs the path, then rescans."""

    from app import sonarr_push
    from app.series_rename import _update_sonarr_path

    calls = []

    def fake(method, path, payload=None):
        calls.append((method, path, payload))
        if method == "GET":
            return [{"id": 12, "tvdbId": 4242, "path": "/tv/Batman", "title": "Batman"}]
        return {}

    class Series:
        tvdb_id = 4242

    monkeypatch.setitem(app.config, "SONARR_URL", "http://sonarr.test")
    monkeypatch.setitem(app.config, "SONARR_API_KEY", "key")
    monkeypatch.setattr(sonarr_push, "_sonarr", fake)
    with app.app_context():
        assert _update_sonarr_path(Series(), "Batman", "Batman (1966)") is True

    assert calls[0] == ("GET", "/api/v3/series?tvdbId=4242", None)
    assert calls[1][0:2] == ("PUT", "/api/v3/series/12")
    assert calls[1][2]["path"] == "/tv/Batman (1966)"
    assert calls[2] == (
        "POST",
        "/api/v3/command",
        {"name": "RescanSeries", "seriesId": 12},
    )


def test_a_series_that_sonarr_does_not_manage_changes_nothing(app, monkeypatch):
    from app import sonarr_push
    from app.series_rename import _update_sonarr_path

    calls = []

    def fake(method, path, payload=None):
        calls.append(method)
        return []

    class Series:
        tvdb_id = 99

    monkeypatch.setitem(app.config, "SONARR_URL", "http://sonarr.test")
    monkeypatch.setitem(app.config, "SONARR_API_KEY", "key")
    monkeypatch.setattr(sonarr_push, "_sonarr", fake)
    with app.app_context():
        assert _update_sonarr_path(Series(), "Old", "New") is False
    assert calls == ["GET"]


def test_only_a_sonarr_adopt_job_carries_the_queue_position(
    app, client, tmp_path, monkeypatch
):
    """Sonarr puts a recent episode at the front. Radarr has no such argument."""

    from datetime import date

    from tests.test_webhooks import auth_header

    movie_dir = tmp_path / "Heat (1995)"
    movie_dir.mkdir()
    (movie_dir / "Heat (1995) - [Bluray-1080p].mkv").write_bytes(b"movie")
    series_dir = tmp_path / "Show" / "Season 01"
    series_dir.mkdir(parents=True)
    episode = "Show - S01E01 - Pilot [Bluray-1080p].mkv"
    (series_dir / episode).write_bytes(b"episode")

    config = {
        "RADARR_URL": "http://radarr.test",
        "RADARR_API_KEY": "key",
        "SONARR_URL": "http://sonarr.test",
        "SONARR_API_KEY": "key",
        "RADARR_ROOT_FOLDERS": [str(tmp_path)],
        "SONARR_ROOT_FOLDERS": [str(tmp_path)],
    }
    for key, value in config.items():
        monkeypatch.setitem(app.config, key, value)
    with app.app_context():
        assert (
            client.post(
                "/api/radarr/add",
                json={
                    "eventType": "Download",
                    "movie": {"id": 3, "folderPath": str(movie_dir)},
                    "movieFile": {
                        "id": 9,
                        "relativePath": "Heat (1995) - [Bluray-1080p].mkv",
                        "quality": "Bluray-1080p",
                    },
                },
                headers=auth_header(),
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/api/sonarr/add",
                json={
                    "eventType": "Download",
                    "series": {"id": 5, "path": str(tmp_path / "Show")},
                    "episodeFile": {
                        "id": 11,
                        "relativePath": f"Season 01/{episode}",
                        "quality": "Bluray-1080p",
                    },
                    "episodes": [{"airDate": date.today().isoformat()}],
                },
                headers=auth_header(),
            ).status_code
            == 200
        )

    jobs = {job.func_name: job for job in app.request_queue.jobs}
    try:
        radarr = jobs["app.radarr_push.adopt_download_task"]
        sonarr = jobs["app.sonarr_push.adopt_episode_task"]
        assert len(radarr.args) == 5 and radarr.args[4] == "WEBRip-1080p"
        assert len(sonarr.args) == 6 and sonarr.args[5] is True
    finally:
        for job in jobs.values():
            job.delete()
