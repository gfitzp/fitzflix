"""Test that an import gives the arr record of its file the release data back (#671).

Sonarr or Radarr imports a download and sends the webhook. Fitzflix
imports the file in place and can store it under a different name. Then
the app drops its record at the next disk scan, and the new record has
no scene name and no release group. The app then downloads the same
release again. The webhook keeps the release data for the path. The
import queues a task that writes it onto the new record.
"""

import os

from datetime import date, datetime, timedelta, timezone

import pytest

from tests.test_webhooks import auth_header

SCENE = "Bobs.Burgers.S11E19.1080p.WEB.h264-GRP"


class FakeArr:
    """A fake _sonarr or _radarr for the record update.

    records is the list of file records of the owner. A command ends at
    once with status. A status of "started" never ends. movie is the
    Radarr movie of the task. down makes each call raise ConnectionError.
    calls records each call in order."""

    def __init__(self, record, records=(), movie=None, down=False, status="completed"):
        self.record = record
        self.records = [dict(r) for r in records]
        self.movie = dict(movie) if movie else None
        self.down = down
        self.status = status
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
            return {"status": self.status}
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


def _library_episode(app):
    """Make a library episode file on disk and return its path."""

    folder = os.path.join(app.config["TV_LIBRARY"], "Show")
    os.makedirs(folder, exist_ok=True)
    library = os.path.join(folder, "Show - S01E01.mkv")
    open(library, "wb").close()
    return library


def _booked_tries(app):
    """Return (job, due time) of each booked record update of the request queue."""

    from rq.registry import ScheduledJobRegistry

    registry = ScheduledJobRegistry(queue=app.request_queue)
    booked = []
    for job_id in registry.get_job_ids():
        job = app.request_queue.fetch_job(job_id)
        if job is not None and job.func_name.endswith(".restore_download_record_task"):
            booked.append(
                (job, registry.get_scheduled_time(job).replace(tzinfo=timezone.utc))
            )
    return booked


def _assert_booked_once(app, library, tries):
    """The task is booked 1 time, 10 minutes ahead, with the same data."""

    from app.arr_client import RESTORE_RETRY_DELAY

    ((job, due),) = _booked_tries(app)
    assert job.func_name == "app.sonarr_push.restore_download_record_task"
    assert job.args == (4, library, SCENE, "GRP")
    assert job.kwargs == {"tries": tries}
    now = datetime.now(timezone.utc)
    assert now + RESTORE_RETRY_DELAY - timedelta(minutes=1) < due
    assert due <= now + RESTORE_RETRY_DELAY
    return job


def _run_booked(app, job):
    """Run a booked try now, as the scheduler would, and remove its booking."""

    from rq.registry import ScheduledJobRegistry

    ScheduledJobRegistry(queue=app.request_queue).remove(job)
    result = job.func(*job.args, **job.kwargs)
    job.delete()
    return result


def test_a_failed_rescan_books_the_task_again_and_the_next_try_writes(
    app, monkeypatch, caplog
):
    """Sonarr fails a RescanSeries while a batch imports. The data stays (#680)."""

    import logging

    import app.sonarr_push as sonarr_push

    library = _library_episode(app)
    fake = FakeArr("episodefile", status="failed")
    monkeypatch.setattr(sonarr_push, "_sonarr", fake)
    caplog.set_level(logging.INFO)
    assert sonarr_push.restore_download_record_task(4, library, SCENE, "GRP") is False
    assert fake.puts == []
    job = _assert_booked_once(app, library, 2)
    assert "Try 2 of 6" in caplog.text
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []

    fake.status = "completed"
    fake.records = [{"id": 9, "path": library}]
    assert _run_booked(app, job) is True
    assert [put["releaseGroup"] for put in fake.puts] == ["GRP"]
    assert _booked_tries(app) == []


def test_a_rescan_that_does_not_end_in_time_books_the_task_again(app, monkeypatch):
    from types import SimpleNamespace

    import app.arr_client as arr_client
    import app.sonarr_push as sonarr_push

    ticks = iter(range(0, 10000, 50))
    monkeypatch.setattr(
        arr_client,
        "time",
        SimpleNamespace(monotonic=lambda: next(ticks), sleep=lambda seconds: None),
    )
    library = _library_episode(app)
    fake = FakeArr("episodefile", status="started")
    monkeypatch.setattr(sonarr_push, "_sonarr", fake)
    assert sonarr_push.restore_download_record_task(4, library, SCENE, "GRP") is False
    assert fake.puts == []
    _assert_booked_once(app, library, 2)

    # The rescan of a large series runs longer than the wait. An earlier
    # rescan made the record. The try reads the records in each case.
    fake.records = [{"id": 9, "path": library}]
    assert sonarr_push.restore_download_record_task(4, library, SCENE, "GRP") is True
    assert [put["releaseGroup"] for put in fake.puts] == ["GRP"]


def test_no_record_at_the_new_path_books_the_task_again(app, monkeypatch):
    """The rescan can be an older one that runs already. The next try scans again."""

    import app.sonarr_push as sonarr_push

    library = _library_episode(app)
    fake = FakeArr("episodefile", records=[{"id": 1, "path": "/tv/other.mkv"}])
    monkeypatch.setattr(sonarr_push, "_sonarr", fake)
    assert sonarr_push.restore_download_record_task(4, library, SCENE, "GRP") is False
    job = _assert_booked_once(app, library, 2)
    assert len([c for c in fake.calls if c[0] == "POST"]) == 1

    # A second booking of the same path replaces the first.
    assert sonarr_push.restore_download_record_task(4, library, SCENE, "GRP") is False
    assert [booked.id for booked, _ in _booked_tries(app)] == [job.id]

    fake.records.append({"id": 9, "path": library})
    assert _run_booked(app, job) is True
    assert [put["id"] for put in fake.puts] == [9]


def test_the_last_try_logs_one_warning_and_books_nothing(app, monkeypatch, caplog):
    import app.sonarr_push as sonarr_push
    from app.arr_client import RESTORE_MAX_TRIES

    library = _library_episode(app)
    fake = FakeArr("episodefile", records=[{"id": 1, "path": "/tv/other.mkv"}])
    monkeypatch.setattr(sonarr_push, "_sonarr", fake)
    assert RESTORE_MAX_TRIES == 6
    assert (
        sonarr_push.restore_download_record_task(4, library, SCENE, "GRP", tries=6)
        is False
    )
    assert _booked_tries(app) == []
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert library in warnings[0].getMessage()
    assert "has no file record at this path" in warnings[0].getMessage()


def test_an_arr_that_is_down_books_the_task_again(app, monkeypatch, caplog):
    import app.sonarr_push as sonarr_push

    library = _library_episode(app)
    monkeypatch.setattr(sonarr_push, "_sonarr", FakeArr("episodefile", down=True))
    assert sonarr_push.restore_download_record_task(4, library, SCENE, "GRP") is False
    _assert_booked_once(app, library, 2)
    assert "The Sonarr record update failed" not in caplog.text


def test_a_library_file_that_is_gone_stops_the_tries(app, monkeypatch):
    import app.sonarr_push as sonarr_push

    fake = FakeArr("episodefile")
    monkeypatch.setattr(sonarr_push, "_sonarr", fake)
    library = os.path.join(app.config["TV_LIBRARY"], "Show", "Gone - S01E02.mkv")
    assert sonarr_push.restore_download_record_task(4, library, SCENE, "GRP") is False
    assert fake.calls == []
    assert _booked_tries(app) == []


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
    os.makedirs(os.path.dirname(library), exist_ok=True)
    open(library, "wb").close()
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


def test_each_try_of_a_film_points_the_movie_at_its_folder(app, monkeypatch):
    """A failed rescan books the Radarr task again. The next try repoints first."""

    import app.radarr_push as radarr_push

    library = os.path.join(
        app.config["MOVIE_LIBRARY"], "Retry Film (1990)", "Retry Film.mkv"
    )
    os.makedirs(os.path.dirname(library), exist_ok=True)
    open(library, "wb").close()
    fake = FakeArr(
        "moviefile",
        movie={"id": 3, "path": "/Volumes/Movies/Old Retry (1990)"},
        status="failed",
    )
    monkeypatch.setattr(radarr_push, "_radarr", fake)
    assert radarr_push.restore_download_record_task(3, library, None, "GRP") is False
    ((job, _),) = _booked_tries(app)
    assert job.func_name == "app.radarr_push.restore_download_record_task"
    assert (job.args, job.kwargs) == ((3, library, None, "GRP"), {"tries": 2})

    fake.calls.clear()
    fake.status = "completed"
    fake.records = [{"id": 72, "path": library}]
    assert _run_booked(app, job) is True
    assert [call[:2] for call in fake.calls][0] == ("GET", "/api/v3/movie/3")
    assert fake.movie["path"] == "/Volumes/Movies/Retry Film (1990)"


def test_a_library_volume_that_does_not_answer_books_the_task_again(app, monkeypatch):
    """A dead mount shows no file. The file is not gone, and the data stays."""

    import app.sonarr_push as sonarr_push
    import app.videos as videos

    library = _library_episode(app)
    os.remove(library)
    fake = FakeArr("episodefile", records=[{"id": 9, "path": library}])
    monkeypatch.setattr(sonarr_push, "_sonarr", fake)
    monkeypatch.setattr(videos, "_dead_volumes", lambda paths: ["/Volumes/TV Shows"])
    assert sonarr_push.restore_download_record_task(4, library, SCENE, "GRP") is False
    assert fake.calls == []
    _assert_booked_once(app, library, 2)


def test_a_later_try_leaves_the_data_of_a_newer_download(app, monkeypatch):
    """A second download of the path wrote its data. The older chain must not replace it."""

    import app.sonarr_push as sonarr_push

    library = _library_episode(app)
    record = {"id": 9, "path": library, "sceneName": "Newer.S01E01.1080p.WEB.h264-NEW"}
    fake = FakeArr("episodefile", records=[record])
    monkeypatch.setattr(sonarr_push, "_sonarr", fake)
    assert sonarr_push.restore_download_record_task(4, library, SCENE, "GRP", 2) is True
    assert fake.puts == []
    assert _booked_tries(app) == []
