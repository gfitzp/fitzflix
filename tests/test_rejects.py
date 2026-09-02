"""Test the rejects triage page.

These tests cover the list with reasons, the re-import back to the
import directory, the deletion, and the path-traversal guard.
"""

import os
import re
import shutil

import pytest


@pytest.fixture(autouse=True)
def clean_rejects(app):
    """Leave the shared rejects directory empty for the other test modules."""

    yield
    rejects = app.config["REJECTS_DIR"]
    if os.path.isdir(rejects):
        for entry in os.listdir(rejects):
            path = os.path.join(rejects, entry)
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)


def csrf_token_from(page_html):
    match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page_html)
    assert match, "no csrf token found in page"
    return match.group(1)


def reject_a_file(app, reason, basename, content=b"rejected bytes"):
    reason_dir = os.path.join(app.config["REJECTS_DIR"], reason)
    os.makedirs(reason_dir, exist_ok=True)
    path = os.path.join(reason_dir, basename)
    with open(path, "wb") as f:
        f.write(content)
    return path


def test_rejects_requires_login(app):
    assert app.test_client().get("/rejects").status_code == 302


def test_rejects_page_lists_files_with_reasons(app, admin_client):
    reject_a_file(app, "exception", "Broken (2021) - [DVD].mkv")
    reject_a_file(app, "incorrect filename", "not-a-movie.mkv")

    page = admin_client.get("/rejects").get_data(as_text=True)
    assert "Broken (2021) - [DVD].mkv" in page
    assert "exception" in page
    assert "not-a-movie.mkv" in page
    assert "incorrect filename" in page


def test_reimport_moves_file_to_import_directory(app, admin_client):
    source = reject_a_file(app, "exception", "Retry Me (2021) - [DVD].mkv")

    page = admin_client.get("/rejects").get_data(as_text=True)
    response = admin_client.post(
        "/rejects",
        data={
            "csrf_token": csrf_token_from(page),
            "file_path": os.path.join("exception", "Retry Me (2021) - [DVD].mkv"),
            "reimport_submit": "Re-import",
        },
        follow_redirects=True,
    )
    assert "Moved" in response.get_data(as_text=True)
    assert not os.path.exists(source)
    destination = os.path.join(app.config["IMPORT_DIR"], "Retry Me (2021) - [DVD].mkv")
    assert os.path.exists(destination)
    os.remove(destination)

    # Fitzflix removed the reason folder, because it is now empty.

    assert not os.path.isdir(os.path.join(app.config["REJECTS_DIR"], "exception"))


def test_reimport_refuses_to_overwrite_import_file(app, admin_client):
    source = reject_a_file(app, "exception", "Duplicate (2021) - [DVD].mkv")
    existing = os.path.join(app.config["IMPORT_DIR"], "Duplicate (2021) - [DVD].mkv")
    os.makedirs(app.config["IMPORT_DIR"], exist_ok=True)
    with open(existing, "wb") as f:
        f.write(b"already importing")

    try:
        page = admin_client.get("/rejects").get_data(as_text=True)
        response = admin_client.post(
            "/rejects",
            data={
                "csrf_token": csrf_token_from(page),
                "file_path": os.path.join("exception", "Duplicate (2021) - [DVD].mkv"),
                "reimport_submit": "Re-import",
            },
            follow_redirects=True,
        )
        assert "already exists" in response.get_data(as_text=True)
        assert os.path.exists(source)
        with open(existing, "rb") as f:
            assert f.read() == b"already importing"
    finally:
        os.remove(existing)
        os.remove(source)


def test_delete_removes_file(app, admin_client):
    source = reject_a_file(app, "upload error", "Gone (2021) - [DVD].mkv")

    page = admin_client.get("/rejects").get_data(as_text=True)
    response = admin_client.post(
        "/rejects",
        data={
            "csrf_token": csrf_token_from(page),
            "file_path": os.path.join("upload error", "Gone (2021) - [DVD].mkv"),
            "delete_submit": "Delete",
        },
        follow_redirects=True,
    )
    assert "Deleted" in response.get_data(as_text=True)
    assert not os.path.exists(source)


def test_path_traversal_is_rejected(app, admin_client):
    """Test that a crafted path cannot reach outside the rejects directory."""

    reject_a_file(app, "exception", "Decoy (2021) - [DVD].mkv")
    outside = os.path.join(os.path.dirname(app.config["REJECTS_DIR"]), "precious.txt")
    with open(outside, "wb") as f:
        f.write(b"do not touch")

    try:
        page = admin_client.get("/rejects").get_data(as_text=True)
        response = admin_client.post(
            "/rejects",
            data={
                "csrf_token": csrf_token_from(page),
                "file_path": os.path.join("..", "precious.txt"),
                "delete_submit": "Delete",
            },
            follow_redirects=True,
        )
        assert "no longer exists" in response.get_data(as_text=True)
        assert os.path.exists(outside)
    finally:
        os.remove(outside)


def test_maintenance_page_links_to_rejects(app, admin_client):
    reject_a_file(app, "exception", "Counted (2021) - [DVD].mkv")

    page = admin_client.get("/maintenance").get_data(as_text=True)
    assert "Triage rejected files" in page
    assert "/rejects" in page
