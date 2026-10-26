"""Pin the form posts of the maintenance page (/maintenance).

Each form of the page has a handler. Each post goes back to the page,
except the filename test. That test renders its result on the page."""

import pytest

from app import db
from tests.conftest import csrf_token_from
from tests.factories import make_movie


@pytest.fixture
def queued(monkeypatch):
    """Record each task and import scan that the page queues."""

    import app.main.admin as admin

    calls = []
    monkeypatch.setattr(
        admin, "enqueue_task", lambda name, *args, **kwargs: calls.append(name)
    )
    monkeypatch.setattr(
        admin,
        "enqueue_import_scan",
        lambda queue, **kwargs: calls.append(("import scan", queue.name)),
    )
    return calls


def _post(admin_client, data, follow=True):
    page = admin_client.get("/maintenance").get_data(as_text=True)
    return admin_client.post(
        "/maintenance",
        data={"csrf_token": csrf_token_from(page), **data},
        follow_redirects=follow,
    )


def test_the_scan_form_queues_the_library_scan(app, admin_client, queued):
    response = _post(admin_client, {"scan_submit": "Rescan"})
    assert queued == ["app.videos.track_metadata_scan_library"]
    assert "Scanning track metadata for all files in the library" in (
        response.get_data(as_text=True)
    )


def test_the_import_form_queues_a_scan_of_the_import_directory(
    app, admin_client, queued
):
    response = _post(admin_client, {"submit": "Scan Import Directory"})
    assert queued == [("import scan", app.request_queue.name)]
    assert "Manually scanning import directory for files" in (
        response.get_data(as_text=True)
    )


def test_the_sync_form_checks_the_password(app, admin_client, queued):
    response = _post(admin_client, {"password": "wrong", "sync_submit": "Sync"})
    assert queued == []
    assert "Incorrect password provided!" in response.get_data(as_text=True)


def test_the_merge_form_needs_a_duplicate(app, admin_client, queued):
    with app.app_context():
        make_movie("Lonely Film", 2001, tmdb_id=880011)
        db.session.commit()
    response = _post(admin_client, {"merge_tmdb_id": "880011", "merge_submit": "Merge"})
    assert queued == []
    assert "No duplicates found for that TMDB id." in response.get_data(as_text=True)


def test_a_tampered_merge_id_flashes_instead_of_a_500(app, admin_client):
    """The hidden field is not a number. The handler must not raise (#390)."""

    for value in ("abc", "12x", "²"):
        response = _post(
            admin_client, {"merge_tmdb_id": value, "merge_submit": "Merge"}
        )
        assert response.status_code == 200
        assert "No duplicates found for that TMDB id." in response.get_data(
            as_text=True
        )


def test_each_post_goes_back_to_the_page_but_the_filename_test(app, admin_client):
    redirected = _post(admin_client, {"scan_submit": "Rescan"}, follow=False)
    assert redirected.status_code == 302
    assert redirected.headers["Location"].endswith("/maintenance")
    rendered = _post(
        admin_client,
        {
            "test_filename": "Jaws (1975) - [Bluray-1080p].mkv",
            "filename_test_submit": "Test",
        },
        follow=False,
    )
    assert rendered.status_code == 200


def _queued_names(app):
    return [
        job.func_name
        for queue in (
            app.maintenance_queue,
            app.sql_queue,
            app.request_queue,
            app.file_queue,
        )
        for job in queue.jobs
    ]


@pytest.mark.parametrize(
    "data, name",
    [
        ({"criterion_refresh": "1"}, "app.videos.refresh_criterion_collection_info"),
        ({"tmdb_refresh": "1"}, "app.videos.refresh_tmdb_info"),
        ({"sync_submit": "1"}, "app.videos.sync_aws_s3_storage_task"),
        ({"scan_submit": "1"}, "app.videos.track_metadata_scan_library"),
    ],
)
def test_a_second_bulk_post_queues_nothing(app, admin_client, data, name):
    """A double click or a resubmit must not queue the whole run two times (#439)."""

    from app.tasks import BULK_GATE_KEY
    from tests.conftest import ADMIN_PASSWORD

    with app.app_context():
        make_movie("Bulk Guard", 2003, tmdb_id=880021)
        db.session.commit()
    data = {**data, "password": ADMIN_PASSWORD}

    _post(admin_client, data)
    first = _queued_names(app).count(name)
    assert first >= 1

    # A second click at once meets the gate.
    body = _post(admin_client, data).get_data(as_text=True)
    assert "already queued or running" in body
    assert _queued_names(app).count(name) == first

    # After the gate, the queued run still blocks a new one.
    for key in app.redis.scan_iter(BULK_GATE_KEY.format("*")):
        app.redis.delete(key)
    _post(admin_client, data)
    assert _queued_names(app).count(name) == first


def test_the_library_scan_jobs_keep_a_new_scan_out(app, admin_client):
    """The per-file jobs of a scan count as the run (#439)."""

    from app.tasks import BULK_GATE_KEY, bulk_job_id, enqueue_task
    from app.tracks import TRACK_SCAN_FILE_RUN

    with app.app_context():
        enqueue_task(
            "app.videos.track_metadata_scan_task",
            1,
            job_id=bulk_job_id(TRACK_SCAN_FILE_RUN),
        )
    body = _post(admin_client, {"scan_submit": "1"}).get_data(as_text=True)
    assert "already queued or running" in body
    assert "app.videos.track_metadata_scan_library" not in _queued_names(app)
    assert not list(app.redis.scan_iter(BULK_GATE_KEY.format("*")))


def test_a_second_missing_scan_queues_nothing(app, admin_client):
    def scan():
        page = admin_client.get("/maintenance/missing").get_data(as_text=True)
        return admin_client.post(
            "/maintenance/missing",
            data={
                "csrf_token": csrf_token_from(page),
                "missing_scan_submit": "Scan now",
            },
            follow_redirects=True,
        ).get_data(as_text=True)

    scan()
    assert "already queued or running" in scan()
    assert _queued_names(app).count("app.missing_files.missing_best_files_task") == 1
