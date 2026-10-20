"""Test that Sonarr keeps its file after the quality downgrade (#289).

This is the Sonarr twin of tests/test_radarr_adopt.py (#288). Sonarr
changes the quality of its own episode-file record and renames the file,
in a task. The task queues the import after the rename, and keeps the
priority of a recent episode. If Sonarr cannot do it, Fitzflix renames
the file as before.
"""

import base64
import os

from datetime import date

import pytest

from app import safe_job_id
from tests.conftest import ADMIN_API_KEY, ADMIN_EMAIL

ORIGINAL = "Columbo (1971) - S01E01 - [Bluray-1080p].mkv"
RENAMED = "Columbo (1971) - S01E01 - [WEBDL-1080p].mkv"


@pytest.fixture(autouse=True)
def sonarr_root(app, monkeypatch, tmp_path):
    monkeypatch.setitem(app.config, "SONARR_ROOT_FOLDERS", [str(tmp_path)])
    monkeypatch.setitem(app.config, "SONARR_URL", "http://sonarr.test")
    monkeypatch.setitem(app.config, "SONARR_API_KEY", "key")


def _download(tmp_path):
    series = tmp_path / "Columbo (1971)"
    season = series / "Season 01"
    season.mkdir(parents=True, exist_ok=True)
    (season / ORIGINAL).write_bytes(b"episode")
    return series, season


class FakeSonarr:
    """Fake the Sonarr API calls of the adopt task. RenameFiles renames."""

    def __init__(self, season, rename_status="completed"):
        self.season = season
        self.rename_status = rename_status
        self.record = {"id": 9, "path": str(season / ORIGINAL), "quality": {}}

    def __call__(self, method, path, payload=None):
        if path == "/api/v3/qualitydefinition":
            return [{"quality": {"id": 3, "name": "WEBDL-1080p"}}]
        if path == "/api/v3/episodefile/9" and method == "GET":
            return dict(self.record)
        if path == "/api/v3/episodefile/9" and method == "PUT":
            self.record = payload
            return payload
        if path == "/api/v3/command":
            assert payload == {"name": "RenameFiles", "seriesId": 4, "files": [9]}
            if self.rename_status == "completed":
                os.rename(self.season / ORIGINAL, self.season / RENAMED)
                self.record["path"] = str(self.season / RENAMED)
            return {"id": 66}
        if path == "/api/v3/command/66":
            return {"status": self.rename_status}
        raise AssertionError(f"unexpected {method} {path}")


def test_webhook_leaves_the_rename_to_sonarr(app, client, tmp_path):
    series, season = _download(tmp_path)
    token = base64.b64encode(f"{ADMIN_EMAIL}:{ADMIN_API_KEY}".encode()).decode()
    response = client.post(
        "/api/sonarr/add",
        json={
            "eventType": "Download",
            "series": {"id": 4, "title": "Columbo", "path": str(series)},
            "episodes": [{"airDate": date.today().isoformat()}],
            "episodeFile": {
                "id": 9,
                "relativePath": f"Season 01/{ORIGINAL}",
                "quality": "Bluray-1080p",
            },
            "customFormatInfo": {"customFormatScore": 2000},
        },
        headers={"Authorization": f"Basic {token}"},
    )
    assert response.status_code == 200
    assert (season / ORIGINAL).exists()
    assert app.import_queue.job_ids == []
    jobs = app.request_queue.jobs
    assert [job.func_name for job in jobs] == ["app.sonarr_push.adopt_episode_task"]

    # The episode aired today. It keeps its place at the front.
    assert jobs[0].args == (
        4,
        9,
        str(season / ORIGINAL),
        str(season / RENAMED),
        "WEBDL-1080p",
        True,
    )


def test_adopt_task_lets_sonarr_rename_then_queues_the_import(
    app, monkeypatch, tmp_path
):
    import app.sonarr_push as sonarr_push

    _, season = _download(tmp_path)
    fake = FakeSonarr(season)
    monkeypatch.setattr(sonarr_push, "_sonarr", fake)
    monkeypatch.setattr(sonarr_push.time, "sleep", lambda seconds: None)

    assert sonarr_push.adopt_episode_task(
        4, 9, str(season / ORIGINAL), str(season / RENAMED), "WEBDL-1080p", True
    )

    assert fake.record["quality"]["quality"]["name"] == "WEBDL-1080p"
    assert (season / RENAMED).exists()
    job = app.import_queue.fetch_job(safe_job_id(RENAMED))
    assert job is not None and job.args == (str(season / RENAMED),)


def test_adopt_task_falls_back_to_a_fitzflix_rename(app, monkeypatch, tmp_path):
    import app.sonarr_push as sonarr_push

    _, season = _download(tmp_path)
    refreshes = []
    monkeypatch.setattr(sonarr_push, "_sonarr", FakeSonarr(season, "failed"))
    monkeypatch.setattr(sonarr_push.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        "app.api.arr.send_arr_command",
        lambda service, url, key, payload: refreshes.append(payload),
    )

    assert sonarr_push.adopt_episode_task(
        4, 9, str(season / ORIGINAL), str(season / RENAMED), "WEBDL-1080p"
    )

    assert (season / RENAMED).exists() and not (season / ORIGINAL).exists()
    assert refreshes == [{"name": "RescanSeries", "seriesId": 4}]
    assert app.import_queue.fetch_job(safe_job_id(RENAMED)) is not None
