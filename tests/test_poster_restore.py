"""Test the restore of a lost library poster copy (#654).

Radarr deletes the poster copy of a folder with the movie file that it
linked the copy to. The Radarr webhook queues a restore of the folder.
A nightly task restores every folder that lost its copy.
"""

import os

from rq.job import Job
from rq.registry import ScheduledJobRegistry

from app import db
from tests.factories import make_movie, make_movie_file
from tests.test_posters import build_custom_tree
from tests.test_webhooks import auth_header


def _folder_without_a_copy(app, title, year):
    """Make a movie with a custom poster, its file, and its folder with no copy."""

    movie = make_movie(title, year, custom_poster="poster.jpg")
    file = make_movie_file(movie, "Bluray-1080p")
    db.session.commit()
    build_custom_tree(app, "movie", movie.id, "poster.jpg")
    library_dir = os.path.join(app.config["LIBRARY_DIR"], file.dirname)
    os.makedirs(library_dir, exist_ok=True)
    return file.dirname, library_dir


def _scheduled_jobs(app):
    registry = ScheduledJobRegistry(queue=app.request_queue)
    return [
        Job.fetch(job_id, connection=app.request_queue.connection)
        for job_id in registry.get_job_ids()
    ]


def test_the_nightly_task_restores_a_lost_copy_and_leaves_the_rest(app):
    from app.custom_artwork import reconcile_library_posters

    with app.app_context():
        lost_dirname, lost_dir = _folder_without_a_copy(app, "Lost Copy", 1999)
        kept_dirname, kept_dir = _folder_without_a_copy(app, "Kept Copy", 1987)
        with open(os.path.join(kept_dir, "poster.png"), "wb") as f:
            f.write(b"a copy that a person placed")
        absent = make_movie("Absent Folder", 1975, custom_poster="poster.jpg")
        make_movie_file(absent, "Bluray-1080p")
        plain = make_movie("No Poster", 1962)
        plain_file = make_movie_file(plain, "Bluray-1080p")
        db.session.commit()
        plain_dir = os.path.join(app.config["LIBRARY_DIR"], plain_file.dirname)
        os.makedirs(plain_dir, exist_ok=True)

        summary = reconcile_library_posters()

        assert summary == "Custom posters: 3 folder(s) checked, 1 copy(ies) restored"
        assert os.path.isfile(os.path.join(lost_dir, "poster.jpg"))
        with open(os.path.join(kept_dir, "poster.png"), "rb") as f:
            assert f.read() == b"a copy that a person placed"
        assert not os.path.exists(os.path.join(kept_dir, "poster.jpg"))
        assert os.listdir(plain_dir) == []


def test_a_radarr_movie_file_delete_queues_the_restore(app, client, caplog):
    from app.custom_artwork import POSTER_RESTORE_DELAY, restore_folder_poster

    with app.app_context():
        dirname, library_dir = _folder_without_a_copy(app, "Deleted File", 1956)
        folder = os.path.join(app.config["LIBRARY_DIR"], dirname)

    response = client.post(
        "/api/radarr/add",
        json={
            "eventType": "MovieFileDelete",
            "deleteReason": "MissingFromDisk",
            "movie": {"id": 9079, "title": "Deleted File", "folderPath": folder},
            "movieFile": {
                "id": 1,
                "relativePath": "Deleted File (1956) - [WEBDL-1080p].mkv",
            },
        },
        headers=auth_header(),
    )
    assert response.status_code == 202

    jobs = _scheduled_jobs(app)
    assert len(jobs) == 1
    assert jobs[0].func_name == "app.custom_artwork.restore_folder_poster"
    assert jobs[0].args == (dirname,)
    assert f"returns in {POSTER_RESTORE_DELAY} s" in caplog.text

    with app.app_context():
        assert restore_folder_poster(dirname) is True
        assert os.path.isfile(os.path.join(library_dir, "poster.jpg"))
        # A second run finds the copy and changes nothing.
        assert restore_folder_poster(dirname) is False


def test_a_folder_outside_the_roots_queues_nothing(app, client, caplog):
    response = client.post(
        "/api/radarr/add",
        json={
            "eventType": "MovieFileDelete",
            "deleteReason": "Manual",
            "movie": {"id": 1, "folderPath": "/elsewhere/Film (2000)"},
        },
        headers=auth_header(),
    )
    assert response.status_code == 202
    assert _scheduled_jobs(app) == []
    assert "outside RADARR_ROOT_FOLDERS" in caplog.text


def test_a_root_folder_outside_the_library_queues_nothing(app, monkeypatch, tmp_path):
    from app.custom_artwork import queue_folder_poster_restore

    monkeypatch.setitem(app.config, "RADARR_ROOT_FOLDERS", [str(tmp_path)])
    with app.app_context():
        assert queue_folder_poster_restore(str(tmp_path / "Film (2000)")) is None
        assert queue_folder_poster_restore(app.config["LIBRARY_DIR"]) is None
        assert queue_folder_poster_restore(None) is None
    assert _scheduled_jobs(app) == []


def test_the_nightly_task_has_a_cron_row_and_the_restore_a_queue(app):
    from app import cron_table
    from app.tasks import TASKS

    with app.app_context():
        rows = cron_table(app.config)
    name = "app.custom_artwork.reconcile_library_posters"
    assert [row["cron"] for row in rows if row["func"] == name] == ["55 5 * * *"]
    assert TASKS[name].queue == "maintenance"
    assert TASKS["app.custom_artwork.restore_folder_poster"].queue == "request"
