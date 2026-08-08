"""Duplicate-movie detection on the Library Maintenance page: grouping by shared TMDb
id, oldest-record-wins ordering, and the one-click merge enqueue.
"""

import re

from datetime import datetime

from app import db

from tests.factories import make_movie


def csrf_token_from(page_html):
    match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page_html)
    assert match, "no csrf token found in page"
    return match.group(1)


def build_duplicates(app):
    """Two records for the same film, plus an unrelated singleton."""

    older = make_movie("Jaws", 1975, tmdb_id=578)
    older.date_created = datetime(2020, 1, 1)

    newer = make_movie("Jaws!", 1975, tmdb_id=578)

    make_movie("Sharknado", 2013, tmdb_id=119283)
    db.session.commit()
    return older, newer


def test_maintenance_lists_duplicate_groups_oldest_first(app, admin_client):
    with app.app_context():
        build_duplicates(app)

    page = admin_client.get("/maintenance").get_data(as_text=True)
    assert "Duplicate movies" in page
    assert "Jaws (1975)" in page
    assert "Jaws! (1975)" in page

    # The older record is marked as the one that will be kept

    assert re.search(r"Jaws \(1975\)</a>[^<]*<span[^>]*>[^<]*kept", page)

    # The singleton isn't listed as a duplicate

    assert "Sharknado" not in page


def test_merge_enqueues_refresh_for_each_duplicate(app, admin_client):
    with app.app_context():
        older, newer = build_duplicates(app)
        older_id, newer_id = older.id, newer.id

    page = admin_client.get("/maintenance").get_data(as_text=True)
    response = admin_client.post(
        "/maintenance",
        data={
            "csrf_token": csrf_token_from(page),
            "merge_tmdb_id": "578",
            "merge_submit": "Merge",
        },
        follow_redirects=True,
    )
    assert "Merging 1 duplicate(s) into" in response.get_data(as_text=True)

    jobs = [
        job
        for job in app.request_queue.jobs
        if job.func_name == "app.videos.refresh_tmdb_info"
    ]
    assert len(jobs) == 1

    # The newer record is the one refreshed (and thereby merged away);
    # the older record survives

    assert jobs[0].args == ("Movies", newer_id, 578)
    assert newer_id != older_id


def test_maintenance_shows_empty_state_without_duplicates(app, admin_client):
    page = admin_client.get("/maintenance").get_data(as_text=True)
    assert "No two movies share a TMDb id." in page
