"""Page smoke tests: routes render, auth gates hold, and the admin filename
tester works end-to-end without writing log lines.
"""

import logging
import re


def csrf_token_from(page_html):
    match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page_html)
    assert match, "no csrf token found in page"
    return match.group(1)


def test_anonymous_user_is_redirected_to_login(client):
    response = client.get("/")
    assert response.status_code == 302
    assert "/auth/login" in response.headers["Location"]


def test_login_page_renders(client):
    assert client.get("/auth/login").status_code == 200


def test_index_renders_for_admin(admin_client):
    assert admin_client.get("/").status_code == 200


def test_about_renders(admin_client):
    assert admin_client.get("/about").status_code == 200


def test_movie_shopping_list_renders(admin_client):
    assert admin_client.get("/shopping-list/movie").status_code == 200


def test_admin_page_shows_health_card_and_tools(admin_client):
    response = admin_client.get("/admin")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "System health" in body
    assert "Filename tester" in body
    assert "Scheduled tasks" in body


def test_filename_tester_previews_without_logging(
    app, admin_client, fake_tmdb, log_capture
):
    page = admin_client.get("/admin").get_data(as_text=True)
    token = csrf_token_from(page)

    log_capture.clear()
    response = admin_client.post(
        "/admin",
        data={
            "csrf_token": token,
            "test_filename": "Jaws (1975) - [Bluray-1080p].mkv",
            "filename_test_submit": "Test",
        },
    )
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "would import as" in body
    assert "Movies/Jaws (1975)/Jaws (1975) - [Bluray-1080p].mkv" in body

    info_lines = [r for r in log_capture if r.levelno >= logging.INFO]
    assert not info_lines, [r.getMessage() for r in info_lines]


def test_filename_tester_shows_rejection(admin_client):
    page = admin_client.get("/admin").get_data(as_text=True)
    token = csrf_token_from(page)

    response = admin_client.post(
        "/admin",
        data={
            "csrf_token": token,
            "test_filename": "Jaws (1975) - [Betamax].mkv",
            "filename_test_submit": "Test",
        },
    )
    assert response.status_code == 200
    assert "would be rejected" in response.get_data(as_text=True)
