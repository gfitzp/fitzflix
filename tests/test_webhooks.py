"""Test the Sonarr and Radarr webhook endpoints.

The tests cover the API-key auth, the event filter, and the download
flow (the quality-downgrade rename and the import enqueue). The Sonarr
and Radarr command callbacks point at a port that cannot route.
Fitzflix logs those callbacks and ignores their errors.
"""

import base64

from datetime import date, timedelta

import pytest

from app import safe_job_id
from tests.conftest import ADMIN_API_KEY, ADMIN_EMAIL, MEMBER_API_KEY, MEMBER_EMAIL


@pytest.fixture(autouse=True)
def arr_roots(app, monkeypatch, tmp_path):
    """Point the root folders of both apps at the tmp_path of the test.

    The webhooks only act on files under the configured root folders of
    the apps. The tmp_path plays both roots. It is deliberately NOT the
    library directory. This proves that the roots are a separate
    setting."""

    monkeypatch.setitem(app.config, "RADARR_ROOT_FOLDERS", [str(tmp_path)])
    monkeypatch.setitem(app.config, "SONARR_ROOT_FOLDERS", [str(tmp_path)])


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

    def test_rejects_a_member_key(self, client, endpoint):
        # A valid key is not sufficient. The handlers move files by the
        # paths in the payload. Thus, only the key of an admin opens them.
        response = client.post(
            endpoint,
            json={"eventType": "Test"},
            headers=auth_header(email=MEMBER_EMAIL, key=MEMBER_API_KEY),
        )
        assert response.status_code == 403

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


def test_incomplete_download_is_refused_and_marked_failed(
    app, client, tmp_path, monkeypatch
):
    """Make sure an incomplete download is refused and marked failed.

    A download that is truncated never reaches the pipeline. Fitzflix
    marks the grab as failed. That marks it on the blocklist and starts
    a replacement search. Fitzflix deletes the junk file and enqueues
    nothing."""

    from app.api import arr as arr_module
    from app.api import radarr as radarr_module

    movie_dir = tmp_path / "Heat (1995)"
    movie_dir.mkdir(parents=True)
    original = "Heat (1995) - [WEBDL-1080p].mkv"
    (movie_dir / original).write_bytes(b"partial")

    monkeypatch.setattr(radarr_module, "import_source_incomplete", lambda path: True)
    calls = []
    monkeypatch.setattr(
        arr_module,
        "mark_grab_failed",
        lambda service, url, key, dl: calls.append((service, dl)) or True,
    )

    response = client.post(
        "/api/radarr/add",
        json={
            "eventType": "Download",
            "movie": {"id": 3, "folderPath": str(movie_dir)},
            "movieFile": {"relativePath": original, "quality": "WEBDL-1080p"},
            "downloadId": "abc123",
        },
        headers=auth_header(),
    )
    assert response.status_code == 200
    assert calls == [("Radarr", "abc123")]
    assert not (movie_dir / original).exists()
    assert app.import_queue.jobs == []


def test_incomplete_download_kept_when_failed_mark_fails(
    app, client, tmp_path, monkeypatch
):
    """Make sure the junk file stays when the failed mark fails.

    If Fitzflix cannot tell the app that sent the file, the junk file
    stays in place for manual handling. But it never reaches the
    pipeline."""

    from app.api import arr as arr_module
    from app.api import sonarr as sonarr_module

    series_dir = tmp_path / "Doctor Who (2005)" / "Season 01"
    series_dir.mkdir(parents=True)
    original = "Doctor Who (2005) - S01E01 - Rose [WEBDL-1080p].mkv"
    (series_dir / original).write_bytes(b"partial")

    monkeypatch.setattr(sonarr_module, "import_source_incomplete", lambda path: True)
    monkeypatch.setattr(arr_module, "mark_grab_failed", lambda *args: False)

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
                "quality": "WEBDL-1080p",
            },
            "episodes": [{"airDate": "2005-03-26"}],
            "downloadId": "xyz789",
        },
        headers=auth_header(),
    )
    assert response.status_code == 200
    assert (series_dir / original).exists()
    assert app.import_queue.jobs == []


def test_mark_grab_failed_finds_grab_and_posts(app, monkeypatch):
    """Make sure mark_grab_failed finds the grab and posts the failure.

    The history lookup finds the grab for the downloadId. It posts to
    /history/failed/{id}. That call adds the grab to the blocklist and
    starts a new search."""

    import json as jsonlib

    from app.api import arr as arr_module

    requests_made = []

    class FakeResponse:
        def __init__(self, status, data=b"{}"):
            self.status = status
            self.data = data

    class FakePool:
        def request(self, method, url, **kwargs):
            requests_made.append((method, url))
            if method == "GET":
                return FakeResponse(
                    200,
                    jsonlib.dumps(
                        {
                            "records": [
                                {"id": 55, "eventType": "downloadFolderImported"},
                                {"id": 42, "eventType": "grabbed"},
                            ]
                        }
                    ).encode(),
                )
            return FakeResponse(200)

    monkeypatch.setattr(arr_module.urllib3, "PoolManager", lambda: FakePool())
    with app.app_context():
        assert arr_module.mark_grab_failed("Radarr", "http://r", "key", "dl1") is True
    assert requests_made[0][0] == "GET"
    assert requests_made[1] == ("POST", "http://r/api/v3/history/failed/42")


@pytest.mark.parametrize(
    "relative_path",
    ["/etc/hosts", "../../outside/secret.mkv", "Season 01/../../outside/secret.mkv"],
)
def test_download_outside_the_library_root_is_refused(
    app, client, tmp_path, relative_path
):
    """Make sure a download outside the root never reaches the pipeline.

    An absolute relativePath, a relativePath that climbs to a parent, or
    a folder outside the root never reaches the rename, the probe, or
    the queue."""

    outside = tmp_path.parent / "outside"
    outside.mkdir(exist_ok=True)
    secret = outside / "secret.mkv"
    secret.write_bytes(b"not yours")

    response = client.post(
        "/api/radarr/add",
        json={
            "eventType": "Download",
            "movie": {"id": 3, "folderPath": str(tmp_path / "Heat (1995)")},
            "movieFile": {"relativePath": relative_path, "quality": "Bluray-1080p"},
            "customFormatInfo": {"customFormatScore": 2000},
        },
        headers=auth_header(),
    )
    assert response.status_code == 400
    assert secret.exists()
    assert app.import_queue.jobs == []


def test_download_folder_outside_the_root_is_refused(app, client, tmp_path):
    outside = tmp_path.parent / "elsewhere" / "Heat (1995)"
    outside.mkdir(parents=True, exist_ok=True)
    original = "Heat (1995) - [Bluray-1080p].mkv"
    (outside / original).write_bytes(b"movie")

    response = client.post(
        "/api/sonarr/add",
        json={
            "eventType": "Download",
            "series": {"id": 7, "title": "Heat", "path": str(outside)},
            "episodeFile": {"relativePath": original, "quality": "Bluray-1080p"},
            "episodes": [{"airDate": "2005-03-26"}],
        },
        headers=auth_header(),
    )
    assert response.status_code == 400
    assert (outside / original).exists()
    assert app.import_queue.jobs == []
