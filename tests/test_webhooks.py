"""Sonarr/Radarr webhook endpoints: API-key auth, event filtering, and the
download flow (quality-downgrade rename + import enqueue). The Sonarr/Radarr
command callbacks point at an unroutable port and are logged-and-swallowed.
"""

import base64

from datetime import date, timedelta

import pytest

from app import safe_job_id
from tests.conftest import ADMIN_API_KEY, ADMIN_EMAIL


def auth_header(email=ADMIN_EMAIL, key=ADMIN_API_KEY):
    token = base64.b64encode(f"{email}:{key}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.mark.parametrize("endpoint", ["/api/sonarr/add", "/api/radarr/add"])
class TestWebhookAuth:
    def test_rejects_missing_auth(self, client, endpoint):
        assert client.post(endpoint, json={"eventType": "Test"}).status_code == 401

    def test_rejects_wrong_key(self, client, endpoint):
        response = client.post(
            endpoint,
            json={"eventType": "Test"},
            headers=auth_header(key="0" * 32),
        )
        assert response.status_code == 401

    def test_rejects_unknown_user(self, client, endpoint):
        response = client.post(
            endpoint,
            json={"eventType": "Test"},
            headers=auth_header(email="nobody@example.test"),
        )
        assert response.status_code == 401

    def test_accepts_test_event(self, client, endpoint):
        response = client.post(
            endpoint, json={"eventType": "Test"}, headers=auth_header()
        )
        assert response.status_code == 202

    def test_ignores_non_download_events(self, client, endpoint):
        response = client.post(
            endpoint, json={"eventType": "Grab"}, headers=auth_header()
        )
        assert response.status_code == 202


def test_sonarr_download_renames_and_enqueues(app, client, tmp_path):
    series_dir = tmp_path / "Doctor Who (2005)" / "Season 01"
    series_dir.mkdir(parents=True)
    original = "Doctor Who (2005) - S01E01 - Rose [Bluray-1080p].mkv"
    (series_dir / original).write_bytes(b"episode")

    recent_airdate = (date.today() - timedelta(days=3)).isoformat()
    response = client.post(
        "/api/sonarr/add",
        json={
            "eventType": "Download",
            "series": {
                "id": 7,
                "title": "Doctor Who (2005)",
                "path": str(tmp_path / "Doctor Who (2005)"),
            },
            "episodeFile": {
                "relativePath": f"Season 01/{original}",
                "quality": "Bluray-1080p",
            },
            "customFormatInfo": {"customFormatScore": 2000},
            "episodes": [{"airDate": recent_airdate}],
        },
        headers=auth_header(),
    )
    assert response.status_code == 200

    renamed = original.replace("[Bluray-1080p]", "[WEBDL-1080p]")
    assert (series_dir / renamed).exists()
    assert not (series_dir / original).exists()

    job = app.import_queue.fetch_job(safe_job_id(renamed))
    assert job is not None
    assert job.args == (str(series_dir / renamed),)


def test_radarr_download_low_score_becomes_webrip(app, client, tmp_path):
    movie_dir = tmp_path / "Heat (1995)"
    movie_dir.mkdir(parents=True)
    original = "Heat (1995) - [Bluray-1080p].mkv"
    (movie_dir / original).write_bytes(b"movie")

    response = client.post(
        "/api/radarr/add",
        json={
            "eventType": "Download",
            "movie": {"id": 3, "folderPath": str(movie_dir)},
            "movieFile": {
                "relativePath": original,
                "quality": "Bluray-1080p",
            },
            "customFormatInfo": {"customFormatScore": 100},
        },
        headers=auth_header(),
    )
    assert response.status_code == 200

    renamed = original.replace("[Bluray-1080p]", "[WEBRip-1080p]")
    assert (movie_dir / renamed).exists()
    assert not (movie_dir / original).exists()

    job = app.import_queue.fetch_job(safe_job_id(renamed))
    assert job is not None
    assert job.args == (str(movie_dir / renamed),)


def test_download_with_already_web_quality_skips_rename(app, client, tmp_path):
    movie_dir = tmp_path / "Heat (1995)"
    movie_dir.mkdir(parents=True)
    original = "Heat (1995) - [WEBDL-1080p].mkv"
    (movie_dir / original).write_bytes(b"movie")

    response = client.post(
        "/api/radarr/add",
        json={
            "eventType": "Download",
            "movie": {"id": 3, "folderPath": str(movie_dir)},
            "movieFile": {
                "relativePath": original,
                "quality": "WEBDL-1080p",
            },
            "customFormatInfo": {"customFormatScore": 2000},
        },
        headers=auth_header(),
    )
    assert response.status_code == 200
    assert (movie_dir / original).exists()
    assert app.import_queue.fetch_job(safe_job_id(original)) is not None
