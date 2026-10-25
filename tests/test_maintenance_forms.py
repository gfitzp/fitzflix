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
