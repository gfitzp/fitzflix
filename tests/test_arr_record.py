"""Test that an import gives the arr record of its file the release data back (#671).

Sonarr or Radarr imports a download and sends the webhook. Fitzflix
imports the file in place and can store it under a different name. Then
the app drops its record at the next disk scan, and the new record has
no scene name and no release group. The app then downloads the same
release again. The webhook keeps the release data for the path. The
import queues a task that writes it onto the new record.
"""

import os

from datetime import date

import pytest

from tests.test_webhooks import auth_header

SCENE = "Bobs.Burgers.S11E19.1080p.WEB.h264-GRP"


class FakeArr:
    """A fake _sonarr or _radarr for the record update.

    records is the list of file records of the owner. A command ends at
    once. movie is the Radarr movie of the task. down makes each call
    raise ConnectionError. calls records each call in order."""

    def __init__(self, record, records=(), movie=None, down=False):
        self.record = record
        self.records = [dict(r) for r in records]
        self.movie = dict(movie) if movie else None
        self.down = down
        self.calls = []
        self.puts = []

    def __call__(self, method, path, payload=None):
        if self.down:
            raise ConnectionError("the arr is down")
        self.calls.append((method, path.split("?")[0]))
        if method == "POST" and path == "/api/v3/command":
            self.calls[-1] = (method, path, payload)
            return {"id": 77}
        if method == "GET" and path == "/api/v3/command/77":
            return {"status": "completed"}
        if method == "GET" and path.startswith(f"/api/v3/{self.record}?"):
            return [dict(r) for r in self.records]
        if method == "PUT" and path.startswith(f"/api/v3/{self.record}/"):
            self.puts.append(dict(payload))
            return payload
        if method == "GET" and path.startswith("/api/v3/movie/") and self.movie:
            return dict(self.movie)
        if method == "PUT" and path.startswith("/api/v3/movie/") and self.movie:
            self.movie = dict(payload)
            return payload
        raise AssertionError(f"unexpected {method} {path}")


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    import time

    monkeypatch.setattr(time, "sleep", lambda seconds: None)


def _kept(app, path):
    from app.arr_client import DOWNLOAD_RECORD_KEY

    return app.redis.get(DOWNLOAD_RECORD_KEY.format(path=path))


def _restore_jobs(app):
    return [
        job
        for job in app.request_queue.jobs
        if job.func_name.endswith(".restore_download_record_task")
    ]


def _run_restore_jobs(app):
    results = []
    for job in _restore_jobs(app):
        results.append(job.func(*job.args, **job.kwargs))
        app.request_queue.remove(job)
    return results


def _sonarr_webhook(client, series, name):
    return client.post(
        "/api/sonarr/add",
        json={
            "eventType": "Download",
            "series": {"id": 4, "title": "Bob's Burgers", "path": str(series)},
            "episodes": [{"airDate": date(2020, 1, 1).isoformat()}],
            "episodeFile": {
                "id": 9,
                "relativePath": f"Season 11/{name}",
                "quality": "WEBDL-1080p",
                "releaseGroup": "GRP",
                "sceneName": SCENE,
            },
            "customFormatInfo": {"customFormatScore": 2000},
        },
        headers=auth_header(),
    )


def _tv_download(app, monkeypatch, tmp_path):
    """Post the webhook of a Sonarr download that keeps its name."""

    monkeypatch.setitem(app.config, "SONARR_ROOT_FOLDERS", [str(tmp_path)])
    monkeypatch.setattr("app.api.arr.send_arr_command", lambda *args: None)
    series = tmp_path / "Bob's Burgers (2011)"
    (series / "Season 11").mkdir(parents=True)
    name = "Bob's Burgers (2011) - S11E19 - [WEBDL-1080p].mkv"
    source = series / "Season 11" / name
    source.write_bytes(b"episode")
    return series, str(source)


def test_a_renamed_episode_gets_its_release_data_back(
    app, client, monkeypatch, tmp_path
):
    """Sonarr rescans, then the record at the Fitzflix path gets the data."""

    import app.sonarr_push as sonarr_push
    from app.importing import _restore_arr_record

    series, source = _tv_download(app, monkeypatch, tmp_path)
    with app.app_context():
        assert _sonarr_webhook(client, series, os.path.basename(source)).status_code
        assert _kept(app, source)

        final = tmp_path / "Bob's Burgers" / "Season 11"
        final.mkdir(parents=True)
        output = str(final / "Bob's Burgers - S11E19 - [WEBDL-1080p].mkv")
        open(output, "wb").close()
        _restore_arr_record(source, output)
        assert _kept(app, source) is None

        fake = FakeArr(
            "episodefile",
            records=[
                {"id": 500, "path": "/elsewhere/other.mkv", "seriesId": 4},
                {"id": 501, "path": output, "seriesId": 4, "quality": {"q": 1}},
            ],
        )
        monkeypatch.setattr(sonarr_push, "_sonarr", fake)
        assert _run_restore_jobs(app) == [True]

    assert fake.calls == [
        ("POST", "/api/v3/command", {"name": "RescanSeries", "seriesId": 4}),
        ("GET", "/api/v3/command/77"),
        ("GET", "/api/v3/episodefile"),
        ("PUT", "/api/v3/episodefile/501"),
    ]
    assert fake.puts == [
        {
            "id": 501,
            "path": output,
            "seriesId": 4,
            "quality": {"q": 1},
            "sceneName": SCENE,
            "releaseGroup": "GRP",
        }
    ]


def test_an_unchanged_name_queues_nothing(app, client, monkeypatch, tmp_path):
    """The library file has the path that Sonarr gave. Sonarr keeps its record."""

    from app.importing import _restore_arr_record

    series, source = _tv_download(app, monkeypatch, tmp_path)
    with app.app_context():
        _sonarr_webhook(client, series, os.path.basename(source))
        assert _kept(app, source)
        _restore_arr_record(source, source)
        assert _kept(app, source) is None
    assert _restore_jobs(app) == []


def test_no_record_at_the_new_path_logs_one_warning(app, monkeypatch, caplog):
    import app.sonarr_push as sonarr_push

    fake = FakeArr("episodefile", records=[{"id": 1, "path": "/tv/other.mkv"}])
    monkeypatch.setattr(sonarr_push, "_sonarr", fake)
    library = os.path.join(app.config["TV_LIBRARY"], "Show", "Show - S01E01.mkv")
    assert sonarr_push.restore_download_record_task(4, library, SCENE, "GRP") is False
    assert fake.puts == []
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "has no file record at this path" in warnings[0].getMessage()


def test_a_record_that_an_older_scan_missed_is_found_by_a_second_scan(app, monkeypatch):
    """Sonarr gives back a scan that already runs. That scan can be too old for the file."""

    import app.sonarr_push as sonarr_push

    library = os.path.join(app.config["TV_LIBRARY"], "Show", "Show - S01E01.mkv")

    class LateArr(FakeArr):
        def __call__(self, method, path, payload=None):
            answer = super().__call__(method, path, payload)
            scans = [c for c in self.calls if c[0] == "POST"]
            if len(scans) == 2 and not self.records:
                self.records = [{"id": 9, "path": library}]
            return answer

    fake = LateArr("episodefile")
    monkeypatch.setattr(sonarr_push, "_sonarr", fake)
    assert sonarr_push.restore_download_record_task(4, library, SCENE, "GRP") is True
    assert len([c for c in fake.calls if c[0] == "POST"]) == 2
    assert [put["releaseGroup"] for put in fake.puts] == ["GRP"]


def test_a_download_with_no_release_data_forgets_the_older_data_of_its_path(app):
    from app.arr_client import SONARR, remember_download, take_remembered_download

    with app.app_context():
        remember_download(SONARR, "/tv/a.mkv", 4, {"releaseGroup": "OLD"})
        remember_download(SONARR, "/tv/a.mkv", 4, {})
        assert take_remembered_download("/tv/a.mkv") is None


def test_a_held_download_keeps_its_release_data_for_the_held_path(
    app, monkeypatch, tmp_path
):
    from app.arr_client import (
        DOWNLOAD_RECORD_KEY,
        DOWNLOAD_RECORD_TTL,
        RADARR,
        remember_download,
        take_remembered_download,
    )

    held_dir = tmp_path / "held"
    monkeypatch.setitem(app.config, "HELD_DIR", str(held_dir))
    monkeypatch.setattr(
        "app.sdr_conversion.makedirs_on_mounted_volume",
        lambda path: os.makedirs(path, exist_ok=True),
    )
    source = tmp_path / "Film (2020) - [WEBDL-2160p].mkv"
    source.write_bytes(b"x")
    with app.app_context():
        from app import db
        from app.models import HeldImport
        from app.sdr_conversion import hold_import

        remember_download(RADARR, str(source), 7, {"releaseGroup": "GRP"})
        plan = {"max_height": 1080, "source_format": "HDR10"}
        held = hold_import(str(source), {"quality_title": "Bluray-1080p"}, plan, [])
        try:
            assert _kept(app, str(source)) is None
            key = DOWNLOAD_RECORD_KEY.format(path=held.held_path)
            assert app.redis.ttl(key) > DOWNLOAD_RECORD_TTL
            assert take_remembered_download(held.held_path)["release_group"] == "GRP"
        finally:
            db.session.delete(db.session.get(HeldImport, held.id))
            db.session.commit()


def test_an_arr_that_is_down_is_logged(app, monkeypatch, caplog):
    import app.sonarr_push as sonarr_push

    monkeypatch.setattr(sonarr_push, "_sonarr", FakeArr("episodefile", down=True))
    library = os.path.join(app.config["TV_LIBRARY"], "Show", "Show - S01E01.mkv")
    assert sonarr_push.restore_download_record_task(4, library, SCENE, "GRP") is False
    assert "The Sonarr record update failed" in caplog.text


def test_an_mp4_film_stored_as_mkv_gets_its_release_data_back(
    app, sample_mp4, monkeypatch
):
    """The conversion changes the extension. The import queues the update.

    The import itself runs in full. The kept data is gone after it."""

    import app.radarr_push as radarr_push
    from app.arr_client import RADARR, remember_download
    from tests.localization_helpers import run_full_chain

    folder = os.path.join(app.config["MOVIE_LIBRARY"], "Mp4 Pin (2021)")
    os.makedirs(folder, exist_ok=True)
    source = os.path.join(folder, "Mp4 Pin (2021) - [WEBDL-1080p].mp4")
    output = os.path.join(folder, "Mp4 Pin (2021) - [WEBDL-1080p].mkv")
    with open(sample_mp4, "rb") as f_in, open(source, "wb") as f_out:
        f_out.write(f_in.read())
    try:
        with app.app_context():
            remember_download(
                RADARR,
                source,
                3,
                {"id": 9, "sceneName": "Mp4.Pin.2021.1080p.WEB-GRP"},
            )
            run_full_chain(app, source)
            assert os.path.exists(output) and not os.path.exists(source)
            assert _kept(app, source) is None
            assert [job.args for job in _restore_jobs(app)] == [
                (3, output, "Mp4.Pin.2021.1080p.WEB-GRP", None)
            ]

            fake = FakeArr(
                "moviefile",
                records=[{"id": 70, "path": output, "movieId": 3, "edition": ""}],
                movie={"id": 3, "path": "/Volumes/Movies/Mp4 Pin (2021)"},
            )
            monkeypatch.setattr(radarr_push, "_radarr", fake)
            assert _run_restore_jobs(app) == [True]
    finally:
        import shutil

        shutil.rmtree(folder, ignore_errors=True)

    # The movie already has the folder. Thus, no movie PUT goes out.
    assert fake.calls == [
        ("GET", "/api/v3/movie/3"),
        ("POST", "/api/v3/command", {"name": "RefreshMovie", "movieIds": [3]}),
        ("GET", "/api/v3/command/77"),
        ("GET", "/api/v3/moviefile"),
        ("PUT", "/api/v3/moviefile/70"),
    ]
    assert fake.puts[0]["sceneName"] == "Mp4.Pin.2021.1080p.WEB-GRP"
    assert fake.puts[0]["releaseGroup"] is None
    assert fake.puts[0]["edition"] == ""


def test_a_film_in_a_new_folder_is_repointed_before_the_lookup(app, monkeypatch):
    """The movie points at the new folder first. Then the rescan finds the file."""

    import app.radarr_push as radarr_push

    library = os.path.join(
        app.config["MOVIE_LIBRARY"], "VictorVictoria (1982)", "VictorVictoria.mkv"
    )
    fake = FakeArr(
        "moviefile",
        records=[{"id": 71, "path": library}],
        movie={"id": 3, "path": "/Volumes/Movies/Victor+Victoria (1982)"},
    )
    monkeypatch.setattr(radarr_push, "_radarr", fake)
    assert radarr_push.restore_download_record_task(3, library, None, "GRP") is True
    assert [call[:2] for call in fake.calls] == [
        ("GET", "/api/v3/movie/3"),
        ("PUT", "/api/v3/movie/3"),
        ("POST", "/api/v3/command"),
        ("GET", "/api/v3/command/77"),
        ("GET", "/api/v3/moviefile"),
        ("PUT", "/api/v3/moviefile/71"),
    ]
    assert fake.movie["path"] == "/Volumes/Movies/VictorVictoria (1982)"


def test_a_film_in_an_edition_folder_gets_nothing(app, monkeypatch):
    """Radarr does not own an edition file (#670). The task calls nothing."""

    import app.radarr_push as radarr_push

    fake = FakeArr("moviefile")
    monkeypatch.setattr(radarr_push, "_radarr", fake)
    library = os.path.join(
        app.config["MOVIE_LIBRARY"], "Star Wars (1977) {edition-D+77}", "a.mkv"
    )
    assert radarr_push.restore_download_record_task(3, library, None, "GRP") is False
    assert fake.calls == []


def test_an_arr_rename_carries_the_kept_data_to_the_new_path(app):
    from app.arr_client import (
        SONARR,
        carry_remembered_download,
        remember_download,
        take_remembered_download,
    )

    with app.app_context():
        remember_download(SONARR, "/tv/a.mkv", 4, {"releaseGroup": "GRP"})
        carry_remembered_download("/tv/a.mkv", "/tv/b.mkv")
        assert take_remembered_download("/tv/a.mkv") is None
        assert take_remembered_download("/tv/b.mkv") == {
            "service": "Sonarr",
            "owner_id": 4,
            "scene_name": None,
            "release_group": "GRP",
        }
        # A path with no kept data moves nothing, and it does not fail.
        carry_remembered_download("/tv/c.mkv", "/tv/d.mkv")
        assert take_remembered_download("/tv/d.mkv") is None
