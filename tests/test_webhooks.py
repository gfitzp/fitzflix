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

    @pytest.mark.parametrize(
        "email, key", [("", ADMIN_API_KEY), (ADMIN_EMAIL, "")], ids=["user", "key"]
    )
    def test_rejects_an_empty_username_or_key(self, client, endpoint, email, key):
        # authenticate_api_request is the one check of the caller (#629).
        response = client.post(
            endpoint, json={"eventType": "Test"}, headers=auth_header(email, key)
        )
        assert response.status_code == 401

    def test_rejects_wrong_key(self, client, endpoint):
        response = client.post(
            endpoint,
            json={"eventType": "Test"},
            headers=auth_header(key="0" * 32),
        )
        assert response.status_code == 401

    def test_rejects_a_non_ascii_key(self, client, endpoint):
        # compare_digest raises on a non-ASCII str. That was a 500 and an
        # error email for each request.
        response = client.post(
            endpoint,
            json={"eventType": "Test"},
            headers=auth_header(key="\u00e9"),
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

    def test_rejects_missing_auth_before_it_reads_the_body(
        self, client, endpoint, caplog
    ):
        # An unauthenticated caller gets 401, also with a bad body. The
        # body never goes to the log (#398).
        response = client.post(
            endpoint, data="{not json SECRET-BODY", content_type="application/json"
        )
        assert response.status_code == 401
        assert "SECRET-BODY" not in caplog.text
        response = client.post(endpoint, data="SECRET-BODY", content_type="text/plain")
        assert response.status_code == 401

    def test_rejects_a_bad_body_after_auth(self, client, endpoint):
        response = client.post(
            endpoint,
            data="{not json",
            content_type="application/json",
            headers=auth_header(),
        )
        assert response.status_code == 400

    def test_caps_the_logged_payload(self, client, endpoint, caplog):
        response = client.post(
            endpoint,
            json={"eventType": "Grab", "pad": "x" * 20000},
            headers=auth_header(),
        )
        assert response.status_code == 202
        assert "[cut]" in caplog.text
        assert "x" * 5000 not in caplog.text

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

    movie_dir = tmp_path / "Heat (1995)"
    movie_dir.mkdir(parents=True)
    original = "Heat (1995) - [WEBDL-1080p].mkv"
    (movie_dir / original).write_bytes(b"partial")

    monkeypatch.setattr(arr_module, "import_source_incomplete", lambda path: True)
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

    series_dir = tmp_path / "Doctor Who (2005)" / "Season 01"
    series_dir.mkdir(parents=True)
    original = "Doctor Who (2005) - S01E01 - Rose [WEBDL-1080p].mkv"
    (series_dir / original).write_bytes(b"partial")

    monkeypatch.setattr(arr_module, "import_source_incomplete", lambda path: True)
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


def test_root_folder_lists_strip_whitespace():
    from config import _path_list

    assert _path_list("/mnt/movies: /mnt/movies2 :", ["x"]) == [
        "/mnt/movies",
        "/mnt/movies2",
    ]
    assert _path_list("", ["/default"]) == ["/default"]
    assert _path_list(None, ["/default"]) == ["/default"]


def test_a_second_webhook_adds_no_second_job(app, client, tmp_path):
    """Radarr can send the import webhook of 1 file more than once (#287).

    The second call answers 200 and adds nothing to the queue."""

    movie_dir = tmp_path / "Heat (1995)"
    movie_dir.mkdir(parents=True)
    original = "Heat (1995) - [WEBDL-1080p].mkv"
    (movie_dir / original).write_bytes(b"movie")
    payload = {
        "eventType": "Download",
        "movie": {"id": 3, "folderPath": str(movie_dir)},
        "movieFile": {"relativePath": original, "quality": "WEBDL-1080p"},
        "customFormatInfo": {"customFormatScore": 2000},
    }

    for _ in range(3):
        response = client.post("/api/radarr/add", json=payload, headers=auth_header())
        assert response.status_code == 200
    assert app.import_queue.job_ids == [safe_job_id(original)]


def test_each_service_uses_its_own_minimum_score(app, client, tmp_path, monkeypatch):
    """RADARR_WEBDL_MIN_SCORE and SONARR_WEBDL_MIN_SCORE are separate (#290)."""

    monkeypatch.setitem(app.config, "RADARR_WEBDL_MIN_SCORE", 50)
    monkeypatch.setitem(app.config, "SONARR_WEBDL_MIN_SCORE", 5000)

    movie_dir = tmp_path / "Heat (1995)"
    movie_dir.mkdir(parents=True)
    (movie_dir / "Heat (1995) - [Bluray-1080p].mkv").write_bytes(b"movie")
    response = client.post(
        "/api/radarr/add",
        json={
            "eventType": "Download",
            "movie": {"id": 3, "folderPath": str(movie_dir)},
            "movieFile": {
                "relativePath": "Heat (1995) - [Bluray-1080p].mkv",
                "quality": "Bluray-1080p",
            },
            "customFormatInfo": {"customFormatScore": 100},
        },
        headers=auth_header(),
    )
    assert response.status_code == 200
    assert (movie_dir / "Heat (1995) - [WEBDL-1080p].mkv").exists()

    season = tmp_path / "Columbo (1971)" / "Season 01"
    season.mkdir(parents=True)
    (season / "Columbo (1971) - S01E01 - [Bluray-1080p].mkv").write_bytes(b"ep")
    response = client.post(
        "/api/sonarr/add",
        json={
            "eventType": "Download",
            "series": {"title": "Columbo", "path": str(tmp_path / "Columbo (1971)")},
            "episodes": [{"airDate": "1971-09-15"}],
            "episodeFile": {
                "relativePath": "Season 01/Columbo (1971) - S01E01 - [Bluray-1080p].mkv",
                "quality": "Bluray-1080p",
            },
            "customFormatInfo": {"customFormatScore": 100},
        },
        headers=auth_header(),
    )
    assert response.status_code == 200
    assert (season / "Columbo (1971) - S01E01 - [WEBRip-1080p].mkv").exists()


@pytest.mark.parametrize(
    "endpoint, body",
    [
        ("/api/radarr/add", {"movie": {"id": 1, "folderPath": "{root}"}}),
        (
            "/api/radarr/add",
            {
                "movie": {"id": 1, "folderPath": "{root}"},
                "movieFile": {"relativePath": "Heat (1995).mkv"},
            },
        ),
        (
            "/api/sonarr/add",
            {
                "series": {"id": 7, "path": "{root}"},
                "episodeFile": {"relativePath": "a.mkv", "quality": "HDTV-720p"},
            },
        ),
        (
            "/api/sonarr/add",
            {
                "series": {"id": 7, "path": "{root}"},
                "episodeFile": {"relativePath": "a.mkv", "quality": "HDTV-720p"},
                "episodes": [],
            },
        ),
    ],
)
def test_a_download_without_its_file_fields_gets_a_400(
    app, client, tmp_path, endpoint, body
):
    """A malformed Download body gets the 400 of the wrapper, not a 500 (#448).

    The folders are under the root folder. Thus, only the missing fields
    can refuse the body."""

    import json

    (tmp_path / "Heat (1995).mkv").write_bytes(b"film")
    (tmp_path / "a.mkv").write_bytes(b"episode")
    body = json.loads(json.dumps(body).replace("{root}", str(tmp_path)))
    response = client.post(
        endpoint, json={"eventType": "Download", **body}, headers=auth_header()
    )
    assert response.status_code == 400
    assert response.is_json
    assert app.import_queue.count == 0


@pytest.mark.parametrize(
    "endpoint, body",
    [
        (
            "/api/radarr/add",
            {
                "movie": "Heat",
                "movieFile": {
                    "relativePath": "Heat (1995).mkv",
                    "quality": "Bluray-1080p",
                },
            },
        ),
        (
            "/api/sonarr/add",
            {
                "series": ["{root}"],
                "episodeFile": {"relativePath": "a.mkv", "quality": "HDTV-720p"},
                "episodes": [{"airDate": "2020-01-01"}],
            },
        ),
        (
            "/api/radarr/add",
            {
                "movie": {"id": 1, "folderPath": "{root}"},
                "movieFile": {
                    "relativePath": "Heat (1995).mkv",
                    "quality": "Bluray-1080p",
                },
                "customFormatInfo": [2000],
            },
        ),
    ],
)
def test_a_shaped_owner_block_gets_a_400(app, client, tmp_path, endpoint, body):
    """A movie or series block that is not an object gets a 400 (#507)."""

    import json

    (tmp_path / "Heat (1995).mkv").write_bytes(b"film")
    (tmp_path / "a.mkv").write_bytes(b"episode")
    body = json.loads(json.dumps(body).replace("{root}", str(tmp_path)))
    response = client.post(
        endpoint, json={"eventType": "Download", **body}, headers=auth_header()
    )
    assert response.status_code == 400
    assert app.import_queue.count == 0


def test_a_null_custom_format_score_counts_as_0(app, client, tmp_path):
    """A null customFormatScore is a low score, not a TypeError (#507)."""

    movie_dir = tmp_path / "Heat (1995)"
    movie_dir.mkdir()
    original = "Heat (1995) [Bluray-1080p].mkv"
    (movie_dir / original).write_bytes(b"film")
    response = client.post(
        "/api/radarr/add",
        json={
            "eventType": "Download",
            "movie": {"id": 1, "folderPath": str(movie_dir)},
            "movieFile": {"relativePath": original, "quality": "Bluray-1080p"},
            "customFormatInfo": {"customFormatScore": None},
        },
        headers=auth_header(),
    )
    assert response.status_code == 200
    assert (movie_dir / "Heat (1995) [WEBRip-1080p].mkv").exists()


def test_a_malformed_air_date_does_not_stop_the_import(app, client, tmp_path):
    """A malformed airDate is not a recent episode and not a 500 (#507)."""

    series_dir = tmp_path / "Doctor Who (2005)" / "Season 01"
    series_dir.mkdir(parents=True)
    original = "Doctor Who (2005) - S01E01 - Rose [HDTV-720p].mkv"
    (series_dir / original).write_bytes(b"episode")
    response = client.post(
        "/api/sonarr/add",
        json={
            "eventType": "Download",
            "series": {"id": 7, "path": str(tmp_path / "Doctor Who (2005)")},
            "episodeFile": {
                "relativePath": f"Season 01/{original}",
                "quality": "HDTV-720p",
            },
            "customFormatInfo": {"customFormatScore": 2000},
            "episodes": [{"airDate": "2020-13-45"}],
        },
        headers=auth_header(),
    )
    assert response.status_code == 200
    assert app.import_queue.fetch_job(safe_job_id(original)) is not None


@pytest.mark.parametrize(
    "owner_id, file_id",
    [("abc", 7), ([1], 7), (1, "x"), (1, True)],
)
def test_a_non_integer_arr_id_gets_a_400(app, client, tmp_path, owner_id, file_id):
    """A text or a list id answered 500 in int() (#577)."""

    (tmp_path / "a.mkv").write_bytes(b"episode")
    response = client.post(
        "/api/sonarr/add",
        json={
            "eventType": "Download",
            "series": {"id": owner_id, "path": str(tmp_path), "title": "Show"},
            "episodeFile": {
                "id": file_id,
                "relativePath": "a.mkv",
                "quality": "HDTV-720p",
            },
            "episodes": [{"seasonNumber": 1, "episodeNumber": 1}],
        },
        headers=auth_header(),
    )
    assert response.status_code == 400
    assert app.import_queue.count == 0
