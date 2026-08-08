"""Page smoke tests: routes render, auth gates hold, and the audit page's
filename tester works end-to-end without writing log lines.
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


def test_system_page_shows_health_schedules_and_bulk_ops(admin_client):
    response = admin_client.get("/system")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "System health" in body
    assert "Scheduled tasks" in body
    assert "Bulk operations" in body


def test_audit_page_shows_file_tools(admin_client):
    response = admin_client.get("/audit")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Rejected files" in body
    assert "Duplicate movies" in body
    assert "Filename tester" in body


def test_profile_page_holds_only_the_profile_forms(admin_client):
    """The old all-in-one Admin page became /profile and only holds the
    profile forms; everything else moved to /audit and /system."""

    response = admin_client.get("/profile")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Profile" in body
    assert "API key" in body
    assert "System health" not in body
    assert "Filename tester" not in body


def test_old_admin_url_redirects_to_profile(admin_client):
    response = admin_client.get("/admin")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/profile")


def test_admin_nav_shows_the_admin_dropdown(admin_client):
    body = admin_client.get("/").get_data(as_text=True)
    assert 'href="/audit"' in body
    assert 'href="/system"' in body
    assert 'href="/profile"' in body


def test_nonadmin_nav_shows_profile_link_instead_of_admin_dropdown(user_client):
    body = user_client.get("/").get_data(as_text=True)
    assert 'href="/audit"' not in body
    assert 'href="/system"' not in body
    assert 'href="/profile">Profile</a>' in body


def test_audit_system_and_rejects_require_admin(user_client):
    """Non-admin users are flashed back to the home page; the profile at
    /profile stays open to them."""

    for path in ("/audit", "/system", "/rejects"):
        response = user_client.get(path)
        assert response.status_code == 302, path
        assert response.headers["Location"].endswith("/recently-added"), path

    response = user_client.get("/audit", follow_redirects=True)
    assert "Need to be an admin user to view this page!" in response.get_data(
        as_text=True
    )

    response = user_client.get("/profile")
    assert response.status_code == 200
    assert "API key" in response.get_data(as_text=True)


def test_filename_tester_previews_without_logging(
    app, admin_client, fake_tmdb, log_capture
):
    page = admin_client.get("/audit").get_data(as_text=True)
    token = csrf_token_from(page)

    log_capture.clear()
    response = admin_client.post(
        "/audit",
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
    page = admin_client.get("/audit").get_data(as_text=True)
    token = csrf_token_from(page)

    response = admin_client.post(
        "/audit",
        data={
            "csrf_token": token,
            "test_filename": "Jaws (1975) - [Betamax].mkv",
            "filename_test_submit": "Test",
        },
    )
    assert response.status_code == 200
    assert "would be rejected" in response.get_data(as_text=True)


def test_tv_shopping_list_renders(admin_client):
    assert admin_client.get("/shopping-list/tv").status_code == 200


def test_service_worker_is_served_from_root_scope(client):
    """The PWA's offline layer: /sw.js must be at the root (not /static/)
    so its scope covers the whole application."""

    response = client.get("/sw.js")
    assert response.status_code == 200
    assert "javascript" in response.headers["Content-Type"]
    assert b"fitzflix-v1" in response.data

    # The manifest is excluded from cache-first so start_url/icon/shortcut
    # edits actually reach installed apps

    assert b"site.webmanifest" in response.data


def test_pages_register_the_service_worker(admin_client):
    body = admin_client.get("/").get_data(as_text=True)
    assert 'serviceWorker.register("/sw.js")' in body
    assert "site.webmanifest" in body

    # The search type-ahead ships its keyboard navigation

    assert "ArrowDown" in body
    assert "ArrowUp" in body

    # The queue poller pauses while the tab is hidden

    assert "visibilitychange" in body


def test_manifest_declares_installable_app():
    import json

    with open("app/static/site.webmanifest") as f:
        manifest = json.load(f)
    assert manifest["start_url"] == "/recently-added"
    assert manifest["scope"] == "/"
    assert manifest["display"] == "standalone"
    assert {icon["sizes"] for icon in manifest["icons"]} == {"192x192", "512x512"}
    assert {shortcut["url"] for shortcut in manifest["shortcuts"]} == {
        "/recently-added",
        "/shopping-list/movie",
        "/shopping-list/tv",
        "/search",
    }


def test_recently_added_badges_quality_by_upgradability(app, admin_client):
    """Recently Added shows quality as badges colored by upgrade
    eligibility: movie rules match the library page, and physical-media
    TV seasons count as final."""

    from app import db
    from tests.factories import (
        make_movie,
        make_movie_file,
        make_tv_file,
        make_tv_series,
    )

    with app.app_context():
        upgradable = make_movie("Recent Upgradable Film", 2010)
        make_movie_file(upgradable, "DVD")
        final = make_movie("Recent Final Film", 2011)
        make_movie_file(final, "Bluray-1080p")
        dvd_show = make_tv_series("Recent DVD Show")
        make_tv_file(dvd_show, 1, 1, "DVD", last_episode=1)
        sd_show = make_tv_series("Recent SD Show")
        make_tv_file(sd_show, 1, 2, "SDTV", last_episode=2)
        db.session.commit()

    page = admin_client.get("/recently-added").get_data(as_text=True)
    # Movie rules: DVD is an upgrade candidate, Blu-ray is final
    assert 'badge-warning">DVD' in page
    assert 'badge-success">Bluray-1080p' in page
    # TV rules: a physical-media DVD season is final; SDTV is not
    assert 'badge-success">DVD' in page
    assert 'badge-warning">SDTV' in page
