"""Test the manual-import flow for the downloads that Sonarr and Radarr cannot import (#276).

The tests cover the webhook event that records a download and emails
the admin once. They cover the page that reads the live queues, and
the proposed filename. They cover the import task: the copy under a
hidden name, the rename, the deletion of the download, and the removal
of the queue items. They cover the download-folder fence. The queues of
the apps are fakes. No test reaches a real service.
"""

import json
import os
import shutil

import pytest

import app.manual_imports as manual_imports
from app import safe_job_id
from tests.test_webhooks import auth_header

DOWNLOAD_ID = "935687bc-d065-4b02-818c-e3482824d6e6"


def radarr_item(output_path, state="importBlocked", queue_id=42):
    return {
        "id": queue_id,
        "downloadId": DOWNLOAD_ID,
        "title": "M-Eine.Stadt.1931.1080p.Remux.AVC.DTS-HD.MA.2.0-playBD",
        "outputPath": output_path,
        "size": 26871427071,
        "status": "completed",
        "trackedDownloadStatus": "warning",
        "trackedDownloadState": state,
        "statusMessages": [
            {
                "title": "M-Eine.Stadt.1931",
                "messages": [
                    "Found matching movie via grab history. Manual Import required."
                ],
            }
        ],
        "quality": {"quality": {"name": "Remux-1080p"}},
        "customFormatScore": 2175,
        "movie": {"title": "M", "year": 1931, "tmdbId": 832},
    }


@pytest.fixture
def downloads(app):
    """Make an empty download folder inside the fence and clean it up."""

    folder = app.config["ARR_DOWNLOAD_FOLDERS"][0]
    os.makedirs(folder, exist_ok=True)
    yield folder
    shutil.rmtree(folder, ignore_errors=True)
    app.redis.delete(manual_imports.PENDING_KEY)
    for key in app.redis.keys("fitzflix:manual-imports:started:*"):
        app.redis.delete(key)
    for name in os.listdir(app.config["IMPORT_DIR"]):
        if name.startswith((".M (", "M (")):
            os.remove(os.path.join(app.config["IMPORT_DIR"], name))


@pytest.fixture
def fake_queues(monkeypatch):
    """Serve the Radarr queue from a list and record the removals."""

    state = {"Radarr": [], "Sonarr": [], "removed": []}

    def fetch_queue(service):
        return list(state[service])

    def arr_request(service, method, path, fields=None):
        assert method == "DELETE"
        state["removed"].append((service, path, fields))
        state[service] = [
            item for item in state[service] if f"queue/{item['id']}" != path
        ]
        return None

    monkeypatch.setattr(manual_imports, "fetch_queue", fetch_queue)
    monkeypatch.setattr("app.main.admin.fetch_queue", fetch_queue)
    monkeypatch.setattr(manual_imports, "arr_request", arr_request)
    return state


def make_download(folder, basename="M.Eine.Stadt.1931.mkv", content=b"m" * 4096):
    output_path = os.path.join(folder, "M-Eine.Stadt.1931-playBD")
    os.makedirs(output_path, exist_ok=True)
    with open(os.path.join(output_path, basename), "wb") as f:
        f.write(content)
    with open(os.path.join(output_path, "sample.nfo"), "w") as f:
        f.write("junk")
    return output_path


def test_manual_interaction_event_records_and_emails_once(app, client, monkeypatch):
    sent = []
    monkeypatch.setattr(
        "app.email.send_email", lambda *args, **kwargs: sent.append(kwargs)
    )
    monkeypatch.setitem(app.config, "MAIL_SERVER", "smtp.test")
    monkeypatch.setitem(app.config, "SERVER_EMAIL", "noreply@example.test")
    app.redis.delete(manual_imports.PENDING_KEY)

    payload = {
        "eventType": "ManualInteractionRequired",
        "downloadId": DOWNLOAD_ID,
        "downloadInfo": {"title": "M-Eine.Stadt.1931-playBD"},
        "downloadStatusMessages": [
            {"title": "x", "messages": ["Manual Import required."]}
        ],
    }
    for _ in range(2):
        response = client.post("/api/radarr/add", json=payload, headers=auth_header())
        assert response.status_code == 202

    assert app.redis.hlen(manual_imports.PENDING_KEY) == 1
    assert len(sent) == 1
    assert "Manual Import required" in sent[0]["text_body"]
    assert "/maintenance/manual-imports" in sent[0]["text_body"]
    app.redis.delete(manual_imports.PENDING_KEY)


def test_proposed_names():
    movie = {
        "quality": "Remux-1080p",
        "score": 2175,
        "record": {"kind": "movie", "title": "M", "year": 1931},
    }
    assert manual_imports.proposed_name(movie, ".mkv") == "M (1931) - [WEBDL-1080p].mkv"

    accented = dict(
        movie, record={"kind": "movie", "title": "Déjà Vu: Part 2?", "year": 2006}
    )
    assert (
        manual_imports.proposed_name(accented, "mp4")
        == "Deja Vu- Part 2 (2006) - [WEBDL-1080p].mp4"
    )

    episodes = {
        "quality": "Bluray-1080p",
        "score": 100,
        "record": {"kind": "series", "title": "30 Rock", "year": 2006},
        "episodes": [(1, 2), (1, 1)],
    }
    assert (
        manual_imports.proposed_name(episodes, ".mkv")
        == "30 Rock - S01E01-E02 - [WEBRip-1080p].mkv"
    )

    unmatched = {"quality": None, "title": "Some.Release.2020", "record": None}
    assert (
        manual_imports.proposed_name(unmatched, None)
        == "Some.Release.2020 - [WEBDL-1080p].mkv"
    )


def test_page_lists_blocked_downloads_with_candidates(
    app, admin_client, downloads, fake_queues
):
    output_path = make_download(downloads)
    fake_queues["Radarr"] = [
        radarr_item(output_path),
        dict(
            radarr_item(output_path, state="downloading", queue_id=1),
            downloadId="still-downloading-id",
        ),
    ]

    page = admin_client.get("/maintenance/manual-imports").get_data(as_text=True)
    assert "M-Eine.Stadt.1931.1080p.Remux" in page
    assert "Manual Import required" in page
    assert 'value="M.Eine.Stadt.1931.mkv"' in page
    assert "sample.nfo" not in page
    assert 'value="M (1931) - [WEBDL-1080p].mkv"' in page
    assert "still-downloading-id" not in page

    # The page made the stored record. Thus, the badges count it.

    assert app.redis.hlen(manual_imports.PENDING_KEY) == 1
    assert manual_imports.pending_count(app.redis) == 1

    # The download left the queue. The page forgets it.

    fake_queues["Radarr"] = []
    page = admin_client.get("/maintenance/manual-imports").get_data(as_text=True)
    assert "Nothing waits" in page
    assert app.redis.hlen(manual_imports.PENDING_KEY) == 0


def test_page_outside_fence_offers_no_import(
    app, admin_client, downloads, fake_queues, tmp_path
):
    output_path = make_download(str(tmp_path))
    fake_queues["Radarr"] = [radarr_item(output_path)]

    page = admin_client.get("/maintenance/manual-imports").get_data(as_text=True)
    assert "Outside ARR_DOWNLOAD_FOLDERS" in page
    assert 'name="import_submit"' not in page


def test_import_enqueues_task_and_task_finishes_the_download(
    app, admin_client, downloads, fake_queues, monkeypatch
):
    output_path = make_download(downloads)
    fake_queues["Radarr"] = [
        radarr_item(output_path),
        radarr_item(output_path, queue_id=43),
    ]

    page = admin_client.get("/maintenance/manual-imports").get_data(as_text=True)
    from tests.test_rejects import csrf_token_from

    response = admin_client.post(
        "/maintenance/manual-imports",
        data={
            "csrf_token": csrf_token_from(page),
            "service": "Radarr",
            "download_id": DOWNLOAD_ID,
            "source": "M.Eine.Stadt.1931.mkv",
            "new_name": "M (1931) - [WEBDL-1080p].mkv",
            "import_submit": "Import",
        },
        follow_redirects=True,
    )
    body = response.get_data(as_text=True)
    assert "Copying" in body
    assert "Import queued" in body

    job = app.file_queue.fetch_job(safe_job_id(f"manual-import:{DOWNLOAD_ID}"))
    assert job is not None
    assert job.args == (
        "Radarr",
        DOWNLOAD_ID,
        output_path,
        "M.Eine.Stadt.1931.mkv",
        "M (1931) - [WEBDL-1080p].mkv",
    )

    # Run the task in this process. The queue is not drained by a worker
    # during the tests.

    manual_imports.manual_import_task(*job.args)

    final = os.path.join(app.config["IMPORT_DIR"], "M (1931) - [WEBDL-1080p].mkv")
    assert os.path.isfile(final)
    assert os.path.getsize(final) == 4096
    assert not os.path.exists(
        os.path.join(app.config["IMPORT_DIR"], ".M (1931) - [WEBDL-1080p].mkv")
    )
    assert not os.path.exists(output_path)
    assert os.path.isdir(downloads)
    assert [path for _service, path, _fields in fake_queues["removed"]] == [
        "queue/42",
        "queue/43",
    ]
    assert fake_queues["removed"][0][2]["removeFromClient"] == "true"
    assert fake_queues["removed"][0][2]["blocklist"] == "false"
    assert not manual_imports.started(app.redis, DOWNLOAD_ID)
    assert app.redis.hlen(manual_imports.PENDING_KEY) == 0


def test_import_refuses_bad_names_and_foreign_files(
    app, admin_client, downloads, fake_queues
):
    output_path = make_download(downloads)
    fake_queues["Radarr"] = [radarr_item(output_path)]
    page = admin_client.get("/maintenance/manual-imports").get_data(as_text=True)
    from tests.test_rejects import csrf_token_from

    token = csrf_token_from(page)
    base = {
        "csrf_token": token,
        "service": "Radarr",
        "download_id": DOWNLOAD_ID,
        "import_submit": "Import",
    }
    bad_name = admin_client.post(
        "/maintenance/manual-imports",
        data=dict(
            base,
            source="M.Eine.Stadt.1931.mkv",
            new_name="../M (1931) - [WEBDL-1080p].mkv",
        ),
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "must be in the form" in bad_name

    foreign = admin_client.post(
        "/maintenance/manual-imports",
        data=dict(
            base, source="../../etc/passwd", new_name="M (1931) - [WEBDL-1080p].mkv"
        ),
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "not a video file" in foreign
    assert app.file_queue.fetch_job(safe_job_id(f"manual-import:{DOWNLOAD_ID}")) is None


def test_task_failure_leaves_download_and_clears_marker(app, downloads, fake_queues):
    output_path = make_download(downloads)
    final = os.path.join(app.config["IMPORT_DIR"], "M (1931) - [WEBDL-1080p].mkv")
    with open(final, "wb") as f:
        f.write(b"already here")
    app.redis.set(manual_imports.STARTED_KEY.format(download_id=DOWNLOAD_ID), "1")

    with pytest.raises(RuntimeError):
        manual_imports.manual_import_task(
            "Radarr",
            DOWNLOAD_ID,
            output_path,
            "M.Eine.Stadt.1931.mkv",
            "M (1931) - [WEBDL-1080p].mkv",
        )

    assert os.path.isfile(os.path.join(output_path, "M.Eine.Stadt.1931.mkv"))
    assert not manual_imports.started(app.redis, DOWNLOAD_ID)
    assert fake_queues["removed"] == []


def test_discard_removes_queue_items_with_client_delete(
    app, admin_client, downloads, fake_queues
):
    output_path = make_download(downloads)
    fake_queues["Radarr"] = [radarr_item(output_path)]
    page = admin_client.get("/maintenance/manual-imports").get_data(as_text=True)
    from tests.test_rejects import csrf_token_from

    body = admin_client.post(
        "/maintenance/manual-imports",
        data={
            "csrf_token": csrf_token_from(page),
            "service": "Radarr",
            "download_id": DOWNLOAD_ID,
            "discard_submit": "Discard download",
        },
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "Discarded" in body
    assert fake_queues["removed"] == [
        (
            "Radarr",
            "queue/42",
            {
                "removeFromClient": "true",
                "blocklist": "false",
                "skipRedownload": "true",
            },
        )
    ]
    assert app.redis.hlen(manual_imports.PENDING_KEY) == 0


def test_fenced_path_refuses_root_and_escape(app, downloads):
    with app.app_context():
        assert manual_imports.fenced_path(downloads) is None
        assert manual_imports.fenced_path(os.path.join(downloads, "..", "x")) is None
        assert manual_imports.fenced_path(None) is None
        inside = os.path.join(downloads, "release")
        assert manual_imports.fenced_path(inside) == os.path.realpath(inside)


def test_system_health_counts_manual_imports(app, admin_client):
    app.redis.hset(
        manual_imports.PENDING_KEY,
        "abc",
        json.dumps({"service": "Radarr", "title": "x"}),
    )
    page = admin_client.get("/system").get_data(as_text=True)
    assert "1 manual import" in page
    app.redis.delete(manual_imports.PENDING_KEY)
