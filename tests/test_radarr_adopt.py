"""Test that Radarr keeps its file after the quality downgrade (#288).

Before #288 Fitzflix renamed a Radarr download itself. Radarr then saw
its file as missing and downloaded the film again. Now Radarr changes
the quality of its own record and renames the file, in a task. The task
queues the import after the rename. If Radarr cannot do it, Fitzflix
renames the file as before.
"""

import base64
import os

import pytest

from app import safe_job_id
from tests.conftest import ADMIN_API_KEY, ADMIN_EMAIL

ORIGINAL = "Paths of Glory (1957) - [Remux-1080p].mkv"
RENAMED = "Paths of Glory (1957) - [WEBDL-1080p].mkv"


@pytest.fixture(autouse=True)
def radarr_root(app, monkeypatch, tmp_path):
    monkeypatch.setitem(app.config, "RADARR_ROOT_FOLDERS", [str(tmp_path)])
    monkeypatch.setitem(app.config, "RADARR_URL", "http://radarr.test")
    monkeypatch.setitem(app.config, "RADARR_API_KEY", "key")


def _download(tmp_path):
    folder = tmp_path / "Paths of Glory (1957)"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / ORIGINAL).write_bytes(b"movie")
    return folder


class FakeRadarr:
    """Fake the Radarr API calls of the adopt task. RenameFiles renames."""

    def __init__(self, folder, rename_status="completed", renames=None):
        self.folder = folder
        self.rename_status = rename_status
        self.renames = rename_status == "completed" if renames is None else renames
        self.record = {"id": 7, "path": str(folder / ORIGINAL), "quality": {}}
        self.calls = []

    def __call__(self, method, path, payload=None):
        self.calls.append((method, path))
        if path == "/api/v3/qualitydefinition":
            return [{"quality": {"id": 3, "name": "WEBDL-1080p"}}]
        if path == "/api/v3/moviefile/7" and method == "GET":
            return dict(self.record)
        if path == "/api/v3/moviefile/7" and method == "PUT":
            self.record = payload
            return payload
        if path == "/api/v3/command":
            assert payload == {"name": "RenameFiles", "movieId": 3, "files": [7]}
            if self.renames:
                os.rename(self.folder / ORIGINAL, self.folder / RENAMED)
                self.record["path"] = str(self.folder / RENAMED)
            return {"id": 55}
        if path == "/api/v3/command/55":
            return {"status": self.rename_status}
        raise AssertionError(f"unexpected {method} {path}")


def test_webhook_leaves_the_rename_to_radarr(app, client, tmp_path):
    folder = _download(tmp_path)
    token = base64.b64encode(f"{ADMIN_EMAIL}:{ADMIN_API_KEY}".encode()).decode()
    response = client.post(
        "/api/radarr/add",
        json={
            "eventType": "Download",
            "movie": {"id": 3, "folderPath": str(folder)},
            "movieFile": {"id": 7, "relativePath": ORIGINAL, "quality": "Remux-1080p"},
            "customFormatInfo": {"customFormatScore": 2000},
        },
        headers={"Authorization": f"Basic {token}"},
    )
    assert response.status_code == 200

    # The file keeps its Radarr name until Radarr renames it. Nothing is
    # imported yet. The adopt task waits on the request queue.
    assert (folder / ORIGINAL).exists()
    assert app.import_queue.job_ids == []
    jobs = app.request_queue.jobs
    assert [job.func_name for job in jobs] == ["app.radarr_push.adopt_download_task"]
    assert jobs[0].args == (
        3,
        7,
        str(folder / ORIGINAL),
        str(folder / RENAMED),
        "WEBDL-1080p",
    )


def test_adopt_task_lets_radarr_rename_then_queues_the_import(
    app, monkeypatch, tmp_path
):
    import app.radarr_push as radarr_push

    folder = _download(tmp_path)
    fake = FakeRadarr(folder)
    monkeypatch.setattr(radarr_push, "_radarr", fake)
    monkeypatch.setattr(radarr_push.time, "sleep", lambda seconds: None)

    assert radarr_push.adopt_download_task(
        3, 7, str(folder / ORIGINAL), str(folder / RENAMED), "WEBDL-1080p"
    )

    assert fake.record["quality"]["quality"]["name"] == "WEBDL-1080p"
    assert fake.record["quality"]["revision"]["version"] == 1
    assert (folder / RENAMED).exists()
    job = app.import_queue.fetch_job(safe_job_id(RENAMED))
    assert job is not None and job.args == (str(folder / RENAMED),)


def test_adopt_task_falls_back_to_a_fitzflix_rename(app, monkeypatch, tmp_path):
    """A failed Radarr rename leaves the file. Fitzflix renames it."""

    import app.radarr_push as radarr_push

    folder = _download(tmp_path)
    fake = FakeRadarr(folder, rename_status="failed")
    refreshes = []
    monkeypatch.setattr(radarr_push, "_radarr", fake)
    monkeypatch.setattr(radarr_push.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        "app.api.arr.send_arr_command",
        lambda service, url, key, payload: refreshes.append(payload),
    )

    assert radarr_push.adopt_download_task(
        3, 7, str(folder / ORIGINAL), str(folder / RENAMED), "WEBDL-1080p"
    )

    assert (folder / RENAMED).exists() and not (folder / ORIGINAL).exists()
    assert refreshes == [{"name": "RefreshMovie", "movieIds": [3]}]
    assert app.import_queue.fetch_job(safe_job_id(RENAMED)) is not None


def test_a_local_rename_without_a_radarr_address_does_not_crash(
    app, monkeypatch, tmp_path
):
    """With no Radarr address, the rename must not try to send a rescan."""

    import app.api.arr as arr
    from app.radarr_push import rename_download_locally

    monkeypatch.setitem(app.config, "RADARR_URL", "")
    sent = []
    monkeypatch.setattr(arr, "send_arr_command", lambda *args: sent.append(args))
    folder = _download(tmp_path)

    with app.app_context():
        rename_download_locally(str(folder / ORIGINAL), str(folder / RENAMED), 3)

    assert (folder / RENAMED).exists()
    assert sent == []


def test_a_late_radarr_rename_imports_the_radarr_file(app, monkeypatch, tmp_path):
    """Radarr renames the file after the wait ends (#325).

    The download is not at its old path. The task imports the file at the
    path of the Radarr record and does not crash in shutil.move."""

    import app.radarr_push as radarr_push

    folder = _download(tmp_path)
    fake = FakeRadarr(folder, rename_status="started", renames=True)
    monkeypatch.setattr(radarr_push, "_radarr", fake)
    monkeypatch.setattr(radarr_push, "RADARR_RENAME_WAIT", -1)
    monkeypatch.setattr(radarr_push.time, "sleep", lambda seconds: None)

    assert radarr_push.adopt_download_task(
        3, 7, str(folder / ORIGINAL), str(folder / RENAMED), "WEBDL-1080p"
    )

    assert (folder / RENAMED).exists()
    job = app.import_queue.fetch_job(safe_job_id(RENAMED))
    assert job is not None and job.args == (str(folder / RENAMED),)


def test_a_lost_download_fails_the_task_and_queues_nothing(app, monkeypatch, tmp_path):
    import app.radarr_push as radarr_push

    folder = _download(tmp_path)
    (folder / ORIGINAL).unlink()
    fake = FakeRadarr(folder, rename_status="failed")
    monkeypatch.setattr(radarr_push, "_radarr", fake)
    monkeypatch.setattr(radarr_push.time, "sleep", lambda seconds: None)

    with pytest.raises(FileNotFoundError, match="is not at its path, at the arr path"):
        radarr_push.adopt_download_task(
            3, 7, str(folder / ORIGINAL), str(folder / RENAMED), "WEBDL-1080p"
        )
    assert app.import_queue.job_ids == []
    # The task asked Radarr for the path of its record before it gave up (#325).
    gets = [call for call in fake.calls if call == ("GET", "/api/v3/moviefile/7")]
    assert len(gets) >= 2


def test_a_radarr_path_outside_the_root_is_not_imported(
    app, monkeypatch, tmp_path, caplog
):
    """Test that the path of the Radarr record goes through the fence (#399).

    Radarr reports a file outside the root folders. Fitzflix renames the
    download itself and imports that file."""

    import app.radarr_push as radarr_push

    folder = _download(tmp_path)
    monkeypatch.setitem(app.config, "RADARR_ROOT_FOLDERS", [str(folder)])
    outside = tmp_path / "outside" / RENAMED
    outside.parent.mkdir()
    outside.write_bytes(b"other")
    fake = FakeRadarr(folder, renames=False)
    fake.record["path"] = str(outside)
    monkeypatch.setattr(radarr_push, "_radarr", fake)
    monkeypatch.setattr(radarr_push.time, "sleep", lambda seconds: None)
    monkeypatch.setattr("app.api.arr.send_arr_command", lambda *args: None)

    assert radarr_push.adopt_download_task(
        3, 7, str(folder / ORIGINAL), str(folder / RENAMED), "WEBDL-1080p"
    )

    assert app.import_queue.fetch_job(safe_job_id(RENAMED)).args == (
        str(folder / RENAMED),
    )
    assert "not under a root folder" in caplog.text


def test_a_late_rename_outside_the_root_is_refused(app, monkeypatch, tmp_path, caplog):
    from app.arr_client import fallback_rename

    folder = _download(tmp_path)
    (folder / ORIGINAL).unlink()
    monkeypatch.setitem(app.config, "RADARR_ROOT_FOLDERS", [str(folder)])
    outside = tmp_path / "outside" / RENAMED
    outside.parent.mkdir()
    outside.write_bytes(b"other")
    with app.app_context(), pytest.raises(FileNotFoundError):
        fallback_rename(
            str(folder / ORIGINAL),
            str(folder / RENAMED),
            lambda: str(outside),
            lambda: None,
            service="Radarr",
        )
    assert "not under a root folder" in caplog.text
