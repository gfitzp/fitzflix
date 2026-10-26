"""Test the Sonarr path check and its webhook trigger (#370).

Sonarr is where a series gets renamed. The nightly check compares the
folder of each Sonarr series with the Fitzflix folder of the series. It
renames the series when only the Sonarr folder exists. It repoints
Sonarr when only the Fitzflix folder exists. It changes nothing in the
other cases. The webhook queues the check of one series when a Rename,
Download or SeriesAdd event names a different folder. A Sonarr rename
of episode files in the same folder changes only the rows."""

import base64
import os

from datetime import date, datetime

import pytest

from app import safe_job_id
from tests.conftest import ADMIN_API_KEY, ADMIN_EMAIL
from tests.factories import make_tv_file, make_tv_series
from tests.fakes import Sonarr

TVDB = 77871


@pytest.fixture
def sonarr(app, monkeypatch):
    """Install a fake Sonarr. Return a function that sets its series list."""

    import app.sonarr_push as sonarr_push

    holder = {}

    def install(*series, down=False):
        holder["fake"] = Sonarr(series, down=down)
        monkeypatch.setattr(sonarr_push, "_sonarr", holder["fake"])
        return holder["fake"]

    install()
    return install


def sonarr_series(folder, series_id=5, tvdb_id=TVDB, root=None, **extra):
    """Return a Sonarr series dict at a folder of the Sonarr root.

    The root is the TV library unless the test gives one."""

    from app import get_app

    root = root or get_app().config["TV_LIBRARY"]
    return {
        "id": series_id,
        "tvdbId": tvdb_id,
        "title": folder,
        "path": os.path.join(root, folder),
        **extra,
    }


def place(app, relative):
    """Put a file at a path under the library directory. Return the path."""

    path = os.path.join(app.config["LIBRARY_DIR"], relative)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        handle.write("video")
    return path


def tv_folder(app, name):
    """Make an empty show folder in the TV library."""

    os.makedirs(os.path.join(app.config["TV_LIBRARY"], name), exist_ok=True)


def batman(app, on_disk=True, **kwargs):
    """Make the series Batman with one episode. Return the series id and file id."""

    from app import db

    series = make_tv_series("Batman", tvdb_id=TVDB, tmdb_id=2287, **kwargs)
    file = make_tv_file(series, 1, 1, "Bluray-1080p")
    file.untouched_basename = file.basename
    db.session.commit()
    if on_disk:
        place(app, file.file_path)
    return series.id, file.id


def rename_jobs(app):
    return [
        job
        for job in app.file_queue.jobs
        if job.func_name == "app.series_rename.rename_tv_series_task"
    ]


def check_jobs(app):
    return [
        job
        for job in app.maintenance_queue.jobs
        if job.func_name == "app.series_rename.reconcile_sonarr_series"
    ]


MOVED = "TV Shows/Batman (1966)/Season 01/Batman (1966) - S01E01 - [Bluray-1080p].mkv"


# The nightly check, branch by branch.


def test_equal_folders_change_nothing(app, sonarr):
    from app.series_rename import reconcile_sonarr_paths

    with app.app_context():
        batman(app)
    fake = sonarr(sonarr_series("Batman"))

    assert reconcile_sonarr_paths() == {
        "checked": 1,
        "renamed": 0,
        "repointed": 0,
        "conflicts": 0,
    }
    assert fake.puts == [] and fake.commands == []
    assert rename_jobs(app) == []


def test_a_folder_that_sonarr_moved_queues_the_rename_once(app, sonarr, caplog):
    """Sonarr moved the folder. The rename task carries the title over.

    A second run finds the rename in the queue and adds no second job."""

    from app.series_rename import reconcile_sonarr_paths

    with app.app_context():
        series_id, _file_id = batman(app, on_disk=False)
    place(app, MOVED)
    sonarr(sonarr_series("Batman (1966)"))

    assert reconcile_sonarr_paths()["renamed"] == 1
    (job,) = rename_jobs(app)
    assert job.args == (series_id, "Batman (1966)")
    assert job.id == safe_job_id(f"sonarr-rename:{series_id}")
    assert "Sonarr moved 'Batman' to 'Batman (1966)'. Renaming" in caplog.text

    assert reconcile_sonarr_paths()["renamed"] == 0
    assert len(rename_jobs(app)) == 1
    assert "A rename is already queued" in caplog.text


def test_a_lost_push_repoints_sonarr(app, sonarr):
    """Only the Fitzflix folder exists. Sonarr goes back to it (#266 case)."""

    from app.series_rename import reconcile_sonarr_paths

    with app.app_context():
        batman(app)
    fake = sonarr(sonarr_series("Batman (Old)"))

    assert reconcile_sonarr_paths()["repointed"] == 1
    (put,) = fake.puts
    assert put["path"] == os.path.join(app.config["TV_LIBRARY"], "Batman")
    assert fake.commands == [{"name": "RescanSeries", "seriesId": 5}]
    assert rename_jobs(app) == []


@pytest.mark.parametrize("both", [True, False], ids=["both exist", "neither exists"])
def test_two_or_no_folders_change_nothing(app, sonarr, caplog, both):
    from app.series_rename import reconcile_sonarr_paths

    with app.app_context():
        batman(app, on_disk=both)
    if both:
        tv_folder(app, "Batman (1966)")
    fake = sonarr(sonarr_series("Batman (1966)"))

    summary = reconcile_sonarr_paths()
    assert (summary["conflicts"], summary["renamed"], summary["repointed"]) == (
        1,
        0,
        0,
    )
    assert fake.puts == [] and rename_jobs(app) == []
    assert f"Sonarr folder exists: {both}, Fitzflix folder exists: {both}" in (
        caplog.text
    )


def test_a_series_that_sonarr_does_not_manage_is_untouched(app, sonarr):
    from app.series_rename import reconcile_sonarr_paths

    with app.app_context():
        batman(app)
    fake = sonarr(sonarr_series("Other Show", tvdb_id=1))

    assert reconcile_sonarr_paths()["checked"] == 0
    assert fake.puts == [] and rename_jobs(app) == []


def test_a_series_without_files_is_not_checked(app, sonarr):
    from app import db
    from app.series_rename import reconcile_sonarr_paths

    with app.app_context():
        make_tv_series("Batman", tvdb_id=TVDB)
        db.session.commit()
    fake = sonarr(sonarr_series("Batman (1966)"))

    assert reconcile_sonarr_paths()["checked"] == 0
    assert fake.puts == [] and rename_jobs(app) == []


def test_the_tmdb_id_matches_a_series_without_a_tvdb_id(app, sonarr):
    from app import db
    from app.series_rename import reconcile_sonarr_paths

    with app.app_context():
        series = make_tv_series("Batman", tmdb_id=2287)
        make_tv_file(series, 1, 1, "Bluray-1080p")
        db.session.commit()
        series_id = series.id
    place(app, MOVED)
    sonarr(sonarr_series("Batman (1966)", tvdb_id=999, tmdbId=2287))

    assert reconcile_sonarr_paths()["renamed"] == 1
    assert rename_jobs(app)[0].args == (series_id, "Batman (1966)")


def test_a_sonarr_folder_that_the_rename_cannot_give_is_a_conflict(app, sonarr, caplog):
    """The folder goes through the checks of the rename task first.

    An accented folder is not safe for the task. A folder with no file at
    its new path would leave the rows at a folder with no files."""

    from app.series_rename import reconcile_sonarr_paths

    with app.app_context():
        batman(app, on_disk=False)
    tv_folder(app, "Batmán (1966)")
    tv_folder(app, "Batman (1967)")
    sonarr(
        sonarr_series("Batmán (1966)"),
        sonarr_series("Batman (1967)", series_id=6, tvdb_id=TVDB),
    )

    summary = reconcile_sonarr_paths()
    assert summary["conflicts"] == 2 and summary["renamed"] == 0
    assert rename_jobs(app) == []
    assert "is not safe as a folder name" in caplog.text
    assert "no file is at its new path in the Sonarr folder" in caplog.text


def test_the_check_changes_nothing_over_the_limit(app, sonarr, caplog, monkeypatch):
    import app.series_rename as series_rename

    monkeypatch.setattr(series_rename, "MAX_SONARR_FIXES", 0)
    with app.app_context():
        batman(app)
    fake = sonarr(sonarr_series("Batman (Old)"))

    summary = series_rename.reconcile_sonarr_paths()
    assert summary["over_limit"] == 1 and summary["repointed"] == 0
    assert fake.puts == []
    assert "more than 0. Nothing changed" in caplog.text


def test_the_check_survives_a_sonarr_outage(app, sonarr):
    from app.series_rename import reconcile_sonarr_paths

    sonarr(down=True)
    assert reconcile_sonarr_paths()["error"] == "Sonarr unreachable"


def test_the_check_changes_nothing_without_the_tv_library(
    app, sonarr, caplog, monkeypatch, tmp_path
):
    """A dropped mount hides both folders. One warning, no 'neither' per series."""

    from app.series_rename import reconcile_sonarr_paths, reconcile_sonarr_series

    with app.app_context():
        series_id, _file_id = batman(app, on_disk=False)
    monkeypatch.setitem(app.config, "TV_LIBRARY", str(tmp_path / "unmounted"))
    fake = sonarr(sonarr_series("Batman (1966)"))

    assert reconcile_sonarr_paths()["error"] == "TV library not mounted"
    assert reconcile_sonarr_series(series_id)["error"] == "TV library not mounted"
    assert fake.gets == []
    assert "Fitzflix folder exists" not in caplog.text


def test_the_rename_task_completes_a_series_that_sonarr_moved(app, sonarr):
    """The full chain: the files are at the new paths before the task runs.

    The task completes the rows, rewrites the untouched names, requests
    the archive moves, and puts the same path back to Sonarr."""

    from app import db
    from app.models import File, TVSeries
    from app.series_rename import reconcile_sonarr_paths

    with app.app_context():
        series = make_tv_series("Batman", tvdb_id=TVDB, tmdb_id=2287)
        files = [make_tv_file(series, 1, n, "Bluray-1080p") for n in (1, 2)]
        for file in files:
            file.untouched_basename = file.basename
            file.aws_key = f"untouched/{file.basename}"
            file.aws_date_uploaded = datetime(2026, 1, 1)
        db.session.commit()
        ids = [file.id for file in files]
        series_id = series.id
    for n in (1, 2):
        place(
            app,
            f"TV Shows/Batman (1966)/Season 01/"
            f"Batman (1966) - S01E0{n} - [Bluray-1080p].mkv",
        )
    fake = sonarr(sonarr_series("Batman (1966)"))

    assert reconcile_sonarr_paths()["renamed"] == 1
    (job,) = rename_jobs(app)
    assert job.func(*job.args) is True

    moves = [
        job.args[0]
        for job in app.file_queue.jobs
        if job.func_name == "app.aws_storage.move_archive_object"
    ]
    assert sorted(moves) == sorted(ids)
    with app.app_context():
        assert db.session.get(TVSeries, series_id).title == "Batman (1966)"
        for file_id in ids:
            row = db.session.get(File, file_id)
            assert row.dirname == "TV Shows/Batman (1966)/Season 01"
            assert row.basename.startswith("Batman (1966) - S01E0")
            assert row.untouched_basename == row.basename
            assert row.aws_key_target == f"untouched/{row.untouched_basename}"
            assert os.path.isfile(
                os.path.join(app.config["LIBRARY_DIR"], row.file_path)
            )
    assert [put["path"] for put in fake.puts] == [
        os.path.join(app.config["TV_LIBRARY"], "Batman (1966)")
    ]

    # The next check finds the folders equal.
    assert reconcile_sonarr_paths()["renamed"] == 0


# The episode-path adoption of a Sonarr rename.


def test_a_renamed_episode_file_in_the_same_folder_is_adopted(app, sonarr):
    from app import db
    from app.models import File
    from app.series_rename import reconcile_sonarr_series

    with app.app_context():
        series_id, file_id = batman(app)
        old_path = os.path.join(
            app.config["LIBRARY_DIR"], db.session.get(File, file_id).file_path
        )
    new = "TV Shows/Batman/Season 01/Batman - S01E01 - Pilot [Bluray-1080p].mkv"
    new_path = place(app, new)
    os.remove(old_path)
    sonarr(sonarr_series("Batman"))

    summary = reconcile_sonarr_series(
        series_id, [{"previousPath": old_path, "path": new_path}]
    )
    assert summary["adopted"] == 1
    with app.app_context():
        row = db.session.get(File, file_id)
        assert row.file_path == new
        assert row.dirname == "TV Shows/Batman/Season 01"
        assert row.basename == "Batman - S01E01 - Pilot [Bluray-1080p].mkv"
        assert row.plex_title == "Batman - S01E01 - Pilot"
        assert row.edition == "Pilot"
        assert row.untouched_basename == "Batman - S01E01 - [Bluray-1080p].mkv"


@pytest.mark.parametrize(
    "new, exists, reason",
    [
        (
            "TV Shows/Batman/Season 01/Batman - S01E01 - Pilot [Bluray-1080p].mkv",
            False,
            "it does not exist",
        ),
        (
            "TV Shows/Batman/Season 01/Batman S01E01 Pilot.mkv",
            True,
            "its name is not in the Fitzflix form",
        ),
        (
            "TV Shows/Batman/Season 01/Batman - S01E02 - [Bluray-1080p].mkv",
            True,
            "its name changes the episode",
        ),
        (
            "TV Shows/Batman/Season 01/Batman (1966) - S01E01 - [Bluray-1080p].mkv",
            True,
            "its name does not give this path",
        ),
    ],
    ids=["missing", "not a Fitzflix name", "other episode", "year added"],
)
def test_a_renamed_episode_file_that_does_not_fit_is_skipped(
    app, sonarr, caplog, new, exists, reason
):
    from app import db
    from app.models import File
    from app.series_rename import reconcile_sonarr_series

    with app.app_context():
        series_id, file_id = batman(app)
        old = db.session.get(File, file_id).file_path
    old_path = os.path.join(app.config["LIBRARY_DIR"], old)
    new_path = os.path.join(app.config["LIBRARY_DIR"], new)
    if exists:
        place(app, new)
    sonarr(sonarr_series("Batman"))

    summary = reconcile_sonarr_series(
        series_id, [{"previousPath": old_path, "path": new_path}]
    )
    assert summary["adopted"] == 0
    assert f"because {reason}" in caplog.text
    with app.app_context():
        assert db.session.get(File, file_id).file_path == old


def test_a_held_title_lock_skips_the_adoption(app, sonarr, caplog, held_lock):
    from app import db
    from app.models import File
    from app.series_rename import reconcile_sonarr_series

    with app.app_context():
        series_id, file_id = batman(app)
        file = db.session.get(File, file_id)
        old, identifier = file.file_path, file.file_identifier()
    held_lock(identifier)
    new = "TV Shows/Batman/Season 01/Batman - S01E01 - Pilot [Bluray-1080p].mkv"
    new_path = place(app, new)
    sonarr(sonarr_series("Batman"))

    summary = reconcile_sonarr_series(
        series_id,
        [
            {
                "previousPath": os.path.join(app.config["LIBRARY_DIR"], old),
                "path": new_path,
            }
        ],
    )
    assert summary["adopted"] == 0
    assert "another task holds its title lock" in caplog.text


def test_a_folder_change_runs_before_the_adoption(app, sonarr, caplog):
    """A rename that also moved the folder adopts no episode path."""

    from app import db
    from app.models import File
    from app.series_rename import reconcile_sonarr_series

    with app.app_context():
        series_id, file_id = batman(app, on_disk=False)
        old = db.session.get(File, file_id).file_path
    new_path = place(app, MOVED)
    sonarr(sonarr_series("Batman (1966)"))

    summary = reconcile_sonarr_series(
        series_id,
        [
            {
                "previousPath": os.path.join(app.config["LIBRARY_DIR"], old),
                "path": new_path,
            }
        ],
    )
    assert summary["renamed"] == 1 and summary["adopted"] == 0
    assert "The episode paths are not adopted" in caplog.text
    with app.app_context():
        assert db.session.get(File, file_id).file_path == old


# The webhook trigger.


def auth_header():
    token = base64.b64encode(f"{ADMIN_EMAIL}:{ADMIN_API_KEY}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def event(app, event_type, folder, **extra):
    """Return a Sonarr event payload for the series Batman at a folder."""

    return {
        "eventType": event_type,
        "series": {
            "id": 5,
            "tvdbId": TVDB,
            "title": "Batman",
            "path": os.path.join(app.config["TV_LIBRARY"], folder),
        },
        **extra,
    }


def download_fields(app, folder):
    """Return the file fields of a Download event, with the file on disk."""

    name = f"{folder} - S01E01 - [WEBDL-1080p].mkv"
    place(app, f"TV Shows/{folder}/Season 01/{name}")
    return {
        "episodes": [{"airDate": date(2000, 1, 1).isoformat()}],
        "episodeFile": {
            "id": 9,
            "relativePath": f"Season 01/{name}",
            "quality": "WEBDL-1080p",
        },
    }


@pytest.mark.parametrize("event_type", ["Rename", "Download", "SeriesAdd"])
def test_an_event_with_a_different_folder_queues_the_check(app, client, event_type):
    with app.app_context():
        series_id, _file_id = batman(app)
    extra = download_fields(app, "Batman (1966)") if event_type == "Download" else {}

    response = client.post(
        "/api/sonarr/add",
        json=event(app, event_type, "Batman (1966)", **extra),
        headers=auth_header(),
    )
    # The import handler answers a Download. The wrapper answers the rest.
    assert response.status_code == (200 if event_type == "Download" else 202)
    (job,) = check_jobs(app)
    assert job.args == (series_id, [])
    assert job.id == safe_job_id(f"sonarr-reconcile:{series_id}")

    # A repeat of the event adds no second job.
    client.post(
        "/api/sonarr/add",
        json=event(app, event_type, "Batman (1966)", **extra),
        headers=auth_header(),
    )
    assert len(check_jobs(app)) == 1


@pytest.mark.parametrize("event_type", ["Rename", "SeriesAdd", "Grab"])
def test_an_event_with_the_same_folder_queues_nothing(app, client, event_type):
    with app.app_context():
        batman(app)

    response = client.post(
        "/api/sonarr/add",
        json=event(app, event_type, "Batman"),
        headers=auth_header(),
    )
    assert response.status_code == 202
    assert check_jobs(app) == []


def test_a_grab_with_a_different_folder_stays_ignored(app, client):
    with app.app_context():
        batman(app)

    response = client.post(
        "/api/sonarr/add",
        json=event(app, "Grab", "Batman (1966)"),
        headers=auth_header(),
    )
    assert response.status_code == 202
    assert check_jobs(app) == []


def test_a_series_path_outside_the_roots_is_ignored(app, client, caplog):
    with app.app_context():
        batman(app)
    payload = event(app, "Rename", "Batman (1966)")
    payload["series"]["path"] = "/elsewhere/Batman (1966)"

    response = client.post("/api/sonarr/add", json=payload, headers=auth_header())
    assert response.status_code == 202
    assert check_jobs(app) == []
    assert "outside SONARR_ROOT_FOLDERS. Ignored" in caplog.text


def test_a_rename_of_episode_files_queues_the_check_with_the_files(app, client):
    from app import db
    from app.models import File

    with app.app_context():
        series_id, file_id = batman(app)
        old = os.path.join(
            app.config["LIBRARY_DIR"], db.session.get(File, file_id).file_path
        )
    renamed = [
        {"previousPath": old, "path": old.replace(" - [", " - Pilot [")},
        {"previousPath": "/Volumes/TV Shows/Batman/unknown.mkv", "path": "/x.mkv"},
    ]

    response = client.post(
        "/api/sonarr/add",
        json=event(app, "Rename", "Batman", renamedEpisodeFiles=renamed),
        headers=auth_header(),
    )
    assert response.status_code == 202
    (job,) = check_jobs(app)
    assert job.args == (series_id, renamed[:1])


# The follow-up of the review (#370).


def localizing(app, basename):
    """Queue a localization of an episode name. No worker runs it."""

    return app.import_queue.enqueue(
        "app.videos.localization_task", args=(f"/import/{basename}",)
    )


def scheduled_rename_retry(app, series_id):
    from rq.registry import ScheduledJobRegistry

    from app.tasks import requeue_job_id

    retry = requeue_job_id("app.series_rename.rename_tv_series_task", series_id)
    return retry in ScheduledJobRegistry(queue=app.file_queue).get_job_ids()


@pytest.mark.parametrize("name", ["Batman", "Batman (1966)"])
def test_a_rename_waits_for_an_import_of_the_series(app, name):
    """An import attaches by title at its end. The rename must wait for it."""

    from app import db
    from app.models import File, TVSeries
    from app.series_rename import rename_tv_series_task

    with app.app_context():
        series_id, file_id = batman(app)
        old = db.session.get(File, file_id).file_path
    localizing(app, f"{name} - S01E03 - [WEBDL-1080p].mkv")

    assert rename_tv_series_task(series_id, "Batman (1966)") is False
    assert scheduled_rename_retry(app, series_id)
    with app.app_context():
        assert db.session.get(TVSeries, series_id).title == "Batman"
        assert db.session.get(File, file_id).file_path == old


def test_a_rename_waits_for_a_held_episode_lock(app, held_lock):
    from app import db
    from app.models import File, TVSeries
    from app.series_rename import rename_tv_series_task

    with app.app_context():
        series_id, file_id = batman(app)
        identifier = db.session.get(File, file_id).file_identifier()
    held_lock(identifier)

    assert rename_tv_series_task(series_id, "Batman (1966)") is False
    assert scheduled_rename_retry(app, series_id)
    with app.app_context():
        assert db.session.get(TVSeries, series_id).title == "Batman"


def test_an_import_after_a_rename_attaches_to_the_renamed_series(app):
    """The parse saw "Batman". The series is "Batman (1966)" at the attach."""

    from app import db
    from app.importing import _attach_tv_series
    from app.models import File, TVSeries

    with app.app_context():
        renamed = make_tv_series("Batman (1966)", tvdb_id=TVDB, tmdb_id=2287)
        tagged = make_tv_series("The Office (2005)", tmdb_id=2316)
        db.session.commit()
        with db.session.no_autoflush:
            for details, wanted in (
                ({"title": "Batman"}, renamed),
                ({"title": "The Office", "tmdb_id": 2316}, tagged),
            ):
                file = File()
                _attach_tv_series(file, details)
                assert file.tv_series is wanted
            assert TVSeries.query.count() == 2
        db.session.rollback()


def test_a_series_on_another_sonarr_root_is_a_conflict(
    app, sonarr, caplog, monkeypatch, tmp_path
):
    """A repoint keeps the Sonarr parent. On another root, it names a missing folder."""

    from app.series_rename import reconcile_sonarr_paths, reconcile_sonarr_series

    other = tmp_path / "Other TV"
    (other / "Batman (1966)").mkdir(parents=True)
    monkeypatch.setitem(
        app.config, "SONARR_ROOT_FOLDERS", [app.config["TV_LIBRARY"], str(other)]
    )
    with app.app_context():
        series_id, _file_id = batman(app)
    fake = sonarr(sonarr_series("Batman (1966)", root=str(other)))

    assert reconcile_sonarr_paths()["conflicts"] == 1
    assert reconcile_sonarr_series(series_id)["conflicts"] == 1
    assert fake.puts == [] and rename_jobs(app) == []
    assert "not under the TV library. Not changed" in caplog.text


def test_a_second_enqueue_gets_a_job_that_does_not_expire(app):
    """A finished job of the id is deleted, not only taken off its registry."""

    from rq.registry import FinishedJobRegistry

    from app.series_rename import _enqueue_once

    name = "app.series_rename.reconcile_sonarr_series"
    job_id = safe_job_id("sonarr-reconcile:1")
    with app.app_context():
        first = _enqueue_once(name, job_id, 1, [], description="first")
        app.maintenance_queue.remove(first)
        FinishedJobRegistry(queue=app.maintenance_queue).add(first, 500)
        app.redis.expire(first.key, 500)

        second = _enqueue_once(name, job_id, 1, [], description="second")
    assert second.id == job_id
    assert app.redis.ttl(second.key) == -1


def test_a_failed_job_stays_for_the_sweep(app):
    """The failed job keeps its id. The new job gets the id with a time tag."""

    from rq.registry import FailedJobRegistry

    from app.series_rename import _enqueue_once

    name = "app.series_rename.reconcile_sonarr_series"
    job_id = safe_job_id("sonarr-reconcile:2")
    failed = FailedJobRegistry(queue=app.maintenance_queue)
    with app.app_context():
        first = _enqueue_once(name, job_id, 2, [], description="first")
        app.maintenance_queue.remove(first)
        first.set_status("failed")
        failed.add(first, 500)

        second = _enqueue_once(name, job_id, 2, [], description="second")
        assert second.id.startswith(job_id + "_")
        assert job_id in failed.get_job_ids()
        # The tagged job counts as the job of the id.
        assert _enqueue_once(name, job_id, 2, [], description="third") is None


def test_a_series_matched_by_tmdb_takes_the_tvdb_id_and_is_repointed(app, sonarr):
    from app import db
    from app.models import TVSeries
    from app.series_rename import reconcile_sonarr_paths

    with app.app_context():
        series = make_tv_series("Batman", tmdb_id=2287)
        file = make_tv_file(series, 1, 1, "Bluray-1080p")
        db.session.commit()
        place(app, file.file_path)
        series_id = series.id
    fake = sonarr(sonarr_series("Batman (Old)", tvdb_id=999, tmdbId=2287))

    assert reconcile_sonarr_paths()["repointed"] == 1
    assert [put["path"] for put in fake.puts] == [
        os.path.join(app.config["TV_LIBRARY"], "Batman")
    ]
    with app.app_context():
        assert db.session.get(TVSeries, series_id).tvdb_id == 999

    # The next night finds the folders equal.
    assert reconcile_sonarr_paths()["repointed"] == 0


def test_a_conflict_stops_the_download_checks_for_a_day(app, client, sonarr):
    """A series in conflict queues no check for each Download. A Rename still does."""

    from app.series_rename import SONARR_CONFLICT_KEY, reconcile_sonarr_series

    with app.app_context():
        series_id, _file_id = batman(app)
    tv_folder(app, "Batman (1966)")
    sonarr(sonarr_series("Batman (1966)"))

    assert reconcile_sonarr_series(series_id)["conflicts"] == 1
    marker = SONARR_CONFLICT_KEY.format(series_id=series_id)
    assert 0 < app.redis.ttl(marker) <= 86400

    extra = download_fields(app, "Batman (1966)")
    client.post(
        "/api/sonarr/add",
        json=event(app, "Download", "Batman (1966)", **extra),
        headers=auth_header(),
    )
    assert check_jobs(app) == []
    client.post(
        "/api/sonarr/add",
        json=event(app, "Rename", "Batman (1966)"),
        headers=auth_header(),
    )
    assert len(check_jobs(app)) == 1


def test_a_converted_episode_keeps_the_quality_of_its_library_name(app, sonarr):
    """The library name of a conversion has its new quality (#292).

    Only the untouched name keeps converted_from_quality. Thus, a
    version-text rename of a converted episode is adopted."""

    from app import db
    from app.models import File
    from app.series_rename import reconcile_sonarr_series

    with app.app_context():
        series = make_tv_series("Batman", tvdb_id=TVDB)
        file = make_tv_file(
            series,
            1,
            1,
            "Bluray-1080p",
            converted_from_quality="Bluray-2160p",
            untouched_basename="Batman - S01E01 - [Bluray-2160p].mkv",
        )
        db.session.commit()
        old_path = place(app, file.file_path)
        series_id, file_id = series.id, file.id
    new_path = place(
        app, "TV Shows/Batman/Season 01/Batman - S01E01 - Pilot [Bluray-1080p].mkv"
    )
    os.remove(old_path)
    sonarr(sonarr_series("Batman"))

    summary = reconcile_sonarr_series(
        series_id, [{"previousPath": old_path, "path": new_path}]
    )
    assert summary["adopted"] == 1
    with app.app_context():
        assert db.session.get(File, file_id).edition == "Pilot"


# The series split of a Sonarr year (#372) and the adopt stage (#373).

NEW_FOLDER = "TV Shows/Batman (1966)/Season 01"


@pytest.mark.parametrize(
    "first_air", [date(1965, 9, 1), None], ids=["other year", "no first-air date"]
)
def test_an_import_in_the_sonarr_folder_attaches_to_its_series(app, sonarr, first_air):
    """The TMDB year cannot fold "Batman (1966)". The Sonarr folder can."""

    from app import db
    from app.importing import _attach_tv_series
    from app.models import File, TVSeries

    with app.app_context():
        series_id, _file_id = batman(app, tmdb_first_air_date=first_air)
        sonarr(sonarr_series("Batman (1966)"))
        with db.session.no_autoflush:
            file = File()
            _attach_tv_series(file, {"title": "Batman (1966)", "dirname": NEW_FOLDER})
            assert file.tv_series.id == series_id
            assert TVSeries.query.count() == 1
        db.session.rollback()


def test_an_import_attaches_to_the_series_of_a_waiting_rename(app, sonarr):
    """Sonarr is down. The rename that waits for the import names the series."""

    from app import db
    from app.importing import _attach_tv_series
    from app.models import File, TVSeries
    from app.tasks import enqueue_task

    sonarr(down=True)
    with app.app_context():
        series_id, _file_id = batman(app)
        enqueue_task(
            "app.series_rename.rename_tv_series_task", series_id, "Batman (1966)"
        )
        with db.session.no_autoflush:
            file = File()
            _attach_tv_series(file, {"title": "Batman (1966)", "dirname": NEW_FOLDER})
            assert file.tv_series.id == series_id
            assert TVSeries.query.count() == 1

            # Another title is still a new series.
            other = File()
            _attach_tv_series(other, {"title": "Robin", "dirname": "TV Shows/Robin"})
            assert other.tv_series.id != series_id
        db.session.rollback()


def test_an_episode_attached_before_the_rename_joins_the_renamed_series(app, sonarr):
    """The split branch: the old folder is gone, 1 row is in the Sonarr folder.

    The nightly check queues the rename, and the rename counts that row
    as done (#372, #374)."""

    from app import db
    from app.models import TVSeries
    from app.series_rename import fitzflix_folders, reconcile_sonarr_paths
    from tests.factories import make_file

    with app.app_context():
        series_id, file_id = batman(app, on_disk=False)
        series = db.session.get(TVSeries, series_id)
        make_file(
            "Batman (1966) - S01E02 - [Bluray-1080p].mkv",
            NEW_FOLDER,
            "Batman (1966) - S01E02",
            "TV Shows",
            "Bluray-1080p",
            series_id=series_id,
            season=1,
            episode=2,
            last_episode=2,
        )
        db.session.commit()
    place(app, MOVED)
    place(app, f"{NEW_FOLDER}/Batman (1966) - S01E02 - [Bluray-1080p].mkv")
    sonarr(sonarr_series("Batman (1966)"))

    assert reconcile_sonarr_paths()["renamed"] == 1
    (job,) = rename_jobs(app)
    assert job.func(*job.args) is True
    with app.app_context():
        series = db.session.get(TVSeries, series_id)
        assert series.title == "Batman (1966)"
        assert fitzflix_folders(series) == {"Batman (1966)"}
    assert reconcile_sonarr_paths()["conflicts"] == 0


def test_a_rename_waits_for_a_sonarr_adopt(app):
    """The adopt task queues the localization after the Sonarr rename (#373)."""

    from app import db
    from app.models import TVSeries
    from app.series_rename import rename_tv_series_task

    with app.app_context():
        series_id, _file_id = batman(app)
    download = "/Volumes/TV Shows/Batman/Season 01/Batman - S01E03 - [Bluray-1080p].mkv"
    app.request_queue.enqueue(
        "app.sonarr_push.adopt_episode_task",
        args=(5, 9, download, download.replace("Bluray", "WEBDL"), "WEBDL-1080p"),
    )

    assert rename_tv_series_task(series_id, "Batman (1966)") is False
    assert scheduled_rename_retry(app, series_id)
    with app.app_context():
        assert db.session.get(TVSeries, series_id).title == "Batman"


def test_an_import_that_starts_in_the_loop_holds_the_title(app, monkeypatch):
    """The rename checks again before the title changes (#373)."""

    import app.videos as videos

    from app import db
    from app.models import TVSeries
    from app.series_rename import rename_tv_series_task

    real_rename = videos._rename_with_retries
    queued = []

    def import_arrives(src, dst, *args, **kwargs):
        if not queued:
            queued.append(localizing(app, "Batman - S01E04 - [WEBDL-1080p].mkv"))
        return real_rename(src, dst, *args, **kwargs)

    monkeypatch.setattr(videos, "_rename_with_retries", import_arrives)
    with app.app_context():
        series_id, _file_id = batman(app)

    assert rename_tv_series_task(series_id, "Batman (1966)") is False
    assert scheduled_rename_retry(app, series_id)
    with app.app_context():
        assert db.session.get(TVSeries, series_id).title == "Batman"

    app.import_queue.remove(queued[0])
    assert rename_tv_series_task(series_id, "Batman (1966)") is True
    with app.app_context():
        assert db.session.get(TVSeries, series_id).title == "Batman (1966)"
