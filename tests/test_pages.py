"""Page smoke tests: routes render, auth gates hold, and the maintenance
page's filename tester works end-to-end without writing log lines.
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


def test_system_page_is_monitoring_only(admin_client):
    """The System page shows health and schedules; the bulk operations
    live on the Library Maintenance page."""

    response = admin_client.get("/system")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "System health" in body
    assert "Scheduled tasks" in body
    assert "Bulk operations" not in body


def test_relative_time_covers_past_and_future():
    from datetime import datetime, timedelta, timezone

    from app.main.admin import _relative_time

    now = datetime.now(timezone.utc)
    assert _relative_time(now - timedelta(seconds=30)) == "under a minute ago"
    assert _relative_time(now - timedelta(minutes=5)) == "5 minutes ago"
    assert _relative_time(now - timedelta(hours=14)) == "14 hours ago"
    assert _relative_time(now + timedelta(seconds=45)) == "in under a minute"
    assert _relative_time(now + timedelta(minutes=10, seconds=30)) == "in 10 minutes"
    assert _relative_time(now + timedelta(days=3)) == "in 3 days"
    # Naive datetimes (rq job timestamps) are treated as UTC
    assert _relative_time(datetime.utcnow() - timedelta(hours=2)) == "2 hours ago"


def test_next_run_never_renders_as_the_past():
    """A due job's stored next-run sits in the past until the scheduler's
    60s tick re-queues it; that window is "due now", not "...ago", and
    anything older means the scheduler has stalled."""

    from datetime import datetime, timedelta, timezone

    from app.main.admin import _next_run_text

    now = datetime.now(timezone.utc)
    assert _next_run_text(now + timedelta(minutes=8)) == "in 8 minutes"
    assert _next_run_text(now - timedelta(seconds=30)) == "due now"
    assert _next_run_text(now - timedelta(seconds=110)) == "due now"
    assert _next_run_text(now - timedelta(minutes=10)) == "overdue"
    # Naive datetimes (what rq-scheduler returns) are treated as UTC
    assert _next_run_text(datetime.utcnow() - timedelta(seconds=30)) == "due now"


def test_system_page_includes_health_poller(admin_client):
    body = admin_client.get("/system").get_data(as_text=True)
    assert 'id="system-health"' in body
    assert "/system/metrics" in body
    assert "visibilitychange" in body


def test_system_metrics_returns_uncached_fragment(admin_client):
    """/system/metrics serves just the health card for the poller to swap
    in: no page chrome, backlog column included, never cached."""

    response = admin_client.get("/system/metrics")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    body = response.get_data(as_text=True)
    assert "navbar" not in body
    assert "Queued" in body
    assert "Database" in body
    assert "Scheduled tasks" in body


def test_system_metrics_shows_queue_backlog(app, admin_client):
    with app.app_context():
        app.request_queue.enqueue(
            "app.videos.refresh_tmdb_info",
            args=("Movies", 1, 1),
            description="Backlogged job",
        )

    body = admin_client.get("/system/metrics").get_data(as_text=True)
    row = re.search(r"<td>user-request</td>.*?</tr>", body, re.S)
    assert row is not None
    assert ">1<" in row.group(0)


def test_system_metrics_requires_admin(user_client):
    response = user_client.get("/system/metrics")
    assert response.status_code == 302


def test_maintenance_page_shows_file_tools_and_bulk_ops(admin_client):
    response = admin_client.get("/maintenance")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Rejected files" in body
    assert "Duplicate movies" in body
    assert "Filename tester" in body
    assert "Bulk operations" in body
    assert "VERY SURE" in body  # the S3 sync form moved along too


def test_profile_page_holds_only_the_profile_forms(admin_client):
    """The old all-in-one Admin page became /profile and only holds the
    profile forms; everything else moved to /maintenance and /system."""

    response = admin_client.get("/profile")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Profile" in body
    assert "API key" in body
    assert "System health" not in body
    assert "Filename tester" not in body


def test_admin_nav_shows_the_admin_dropdown(admin_client):
    body = admin_client.get("/").get_data(as_text=True)
    assert 'href="/maintenance"' in body
    assert 'href="/system"' in body
    assert 'href="/profile"' in body


def test_nonadmin_nav_shows_profile_link_instead_of_admin_dropdown(user_client):
    body = user_client.get("/").get_data(as_text=True)
    assert 'href="/maintenance"' not in body
    assert 'href="/system"' not in body
    assert 'href="/profile">Profile</a>' in body


def test_maintenance_system_and_rejects_require_admin(user_client):
    """Non-admin users are flashed back to the home page; the profile at
    /profile stays open to them."""

    for path in ("/maintenance", "/system", "/rejects"):
        response = user_client.get(path)
        assert response.status_code == 302, path
        assert response.headers["Location"].endswith("/index"), path

    response = user_client.get("/maintenance", follow_redirects=True)
    assert "Need to be an admin user to view this page!" in response.get_data(
        as_text=True
    )

    response = user_client.get("/profile")
    assert response.status_code == 200
    assert "API key" in response.get_data(as_text=True)


def test_filename_tester_previews_without_logging(
    app, admin_client, fake_tmdb, log_capture
):
    page = admin_client.get("/maintenance").get_data(as_text=True)
    token = csrf_token_from(page)

    log_capture.clear()
    response = admin_client.post(
        "/maintenance",
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
    page = admin_client.get("/maintenance").get_data(as_text=True)
    token = csrf_token_from(page)

    response = admin_client.post(
        "/maintenance",
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
    assert b"fitzflix-v2" in response.data

    # The manifest is excluded from cache-first so start_url/icon/shortcut
    # edits actually reach installed apps

    assert b"site.webmanifest" in response.data


def test_service_worker_only_caches_successful_responses(client):
    """A failed response kept cache-first is permanent: a proxy 502 stored
    under a poster's URL was served in the image's place until that browser's
    cache was emptied (#206). So every cache.put is gated on the response
    being usable, and the static branch revalidates so a file replaced in
    place isn't served stale forever."""

    body = client.get("/sw.js").get_data(as_text=True)

    assert "function isCacheable(response)" in body
    assert 'response.ok || response.type === "opaque"' in body

    # Neither branch may write to the cache unconditionally

    assert body.count("cache.put(request, copy)") == body.count(
        "if (isCacheable(response))"
    )


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
    # The installed app opens at the landing page's recommendation
    # shelves (Glenn's call, Aug 2026)
    assert manifest["start_url"] == "/"
    assert manifest["scope"] == "/"
    assert manifest["display"] == "standalone"
    assert {icon["sizes"] for icon in manifest["icons"]} == {"192x192", "512x512"}
    assert {shortcut["url"] for shortcut in manifest["shortcuts"]} == {
        "/",
        "/shopping-list/movie",
        "/shopping-list/tv",
        "/search",
    }


def test_recently_added_badges_quality_by_upgradability(app, admin_client):
    """File Activity shows quality as badges colored by upgrade
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

    page = admin_client.get("/file-activity").get_data(as_text=True)
    # Movie rules: DVD is an upgrade candidate, Blu-ray is final
    assert 'text-bg-warning">DVD' in page
    assert 'text-bg-success">Bluray-1080p' in page
    # TV rules: a physical-media DVD season is final; SDTV is not
    assert 'text-bg-success">DVD' in page
    assert 'text-bg-warning">SDTV' in page


def test_file_activity_page_wires_the_live_dashboard(app, admin_client):
    """Page 1 carries the card-fetch URL the queue poll's dashboard mode
    keys on, and each landed card advertises the basenames trails can
    match; deeper pages are a plain list. The separate in-flight list
    is gone — those chips paint on the queue page's rows now."""

    from app import db
    from tests.factories import make_movie, make_movie_file

    with app.app_context():
        movie = make_movie("Dashboard Film", 2012)
        make_movie_file(
            movie, "Bluray-1080p", untouched_basename="Dashboard.Film.2012.mkv"
        )
        db.session.commit()

    page = admin_client.get("/file-activity").get_data(as_text=True)
    assert 'id="pipeline-files-section"' not in page
    assert "window.fileActivityCardUrl = " in page
    assert (
        'data-file-basenames="Dashboard Film (2012) - [Bluray-1080p].mkv|Dashboard.Film.2012.mkv"'
        in page
    )
    assert "data-trail" in page

    deeper = admin_client.get("/file-activity?page=2").get_data(as_text=True)
    assert "window.fileActivityCardUrl = " not in deeper


def test_file_activity_card_fragment_matches_trail_basenames(app, admin_client):
    """/file-activity/card serves one landed file's card for the trail
    basename the poll knows — the File row's own name, the original
    import filename, or either with a container-converted extension —
    and 404s while no matching row exists yet."""

    from app import db
    from tests.factories import make_movie, make_movie_file

    with app.app_context():
        movie = make_movie("Fragment Film", 2013)
        make_movie_file(
            movie, "Bluray-1080p", untouched_basename="Fragment.Film.2013.avi"
        )
        db.session.commit()

    for basename in (
        "Fragment Film (2013) - [Bluray-1080p].mkv",  # the File row's name
        "Fragment.Film.2013.avi",  # the original import filename
        "Fragment.Film.2013.mkv",  # converted to Matroska en route
    ):
        response = admin_client.get(
            "/file-activity/card", query_string={"basename": basename}
        )
        assert response.status_code == 200, basename
        body = response.get_data(as_text=True)
        assert "data-file-basenames" in body
        assert "Fragment Film" in body

    assert (
        admin_client.get(
            "/file-activity/card", query_string={"basename": "No.Such.File.mkv"}
        ).status_code
        == 404
    )
    assert admin_client.get("/file-activity/card").status_code == 404


def test_genre_links_filter_the_library(app, admin_client):
    """Genre names on the movie page link to the library filtered to
    that TMDb genre; the filter composes with the quality
    dropdown and an unknown genre 404s."""

    from app import db
    from app.models import RefQuality
    from tests.factories import make_movie, make_movie_file
    from tests.test_recommendations import genre

    with app.app_context():
        western = genre(37, "Western")
        drama = genre(18, "Drama")
        cowboy = make_movie("Genre Cowboy", 1950)
        cowboy.genres.append(western)
        # The overview gates the paragraph the genre links live in
        cowboy.tmdb_overview = "A cowboy rides."
        make_movie_file(cowboy, "Bluray-1080p")
        oater = make_movie("Genre Oater on DVD", 1951)
        oater.genres.append(western)
        make_movie_file(oater, "DVD")
        weeper = make_movie("Genre Weeper", 1952)
        weeper.genres.append(drama)
        make_movie_file(weeper, "Bluray-1080p")
        db.session.commit()
        cowboy_id = cowboy.id
        bluray_id = RefQuality.query.filter_by(quality_title="Bluray-1080p").one().id

    # The movie page's genre line links to the filtered library

    page = admin_client.get(f"/movie/{cowboy_id}").get_data(as_text=True)
    assert "/library/movie?genre=37" in page

    listing = admin_client.get("/library/movie?genre=37").get_data(as_text=True)
    assert "Western Movies" in listing
    assert "Genre Cowboy" in listing
    assert "Genre Oater on DVD" in listing
    assert "Genre Weeper" not in listing

    # Quality composes with the genre filter

    listing = admin_client.get(f"/library/movie?genre=37&quality={bluray_id}").get_data(
        as_text=True
    )
    assert "Genre Cowboy" in listing
    assert "Genre Oater on DVD" not in listing

    assert admin_client.get("/library/movie?genre=99999").status_code == 404


def test_local_time_text_renders_server_local():
    """Tooltip timestamps interpret naive rq/scheduler times as UTC and
    render them in the server's local zone, in moment.js's LLL shape."""

    from datetime import datetime, timezone

    from app.main.admin import _local_time_text

    when = datetime(2026, 8, 18, 21, 0, 21)
    expected = when.replace(tzinfo=timezone.utc).astimezone()
    assert _local_time_text(when) == expected.strftime("%B %-d, %Y %-I:%M %p")
    assert _local_time_text(None) == ""


def test_every_template_reference_resolves(app):
    """Every {% import/from/include/extends %} target in app/templates loads
    through the Jinja environment. Guards against a dependency drop orphaning
    a template served by a package (bootstrap/wtf.html, Aug 2026)."""

    from pathlib import Path

    reference = re.compile(
        r"""\{%-?\s*(?:import|from|include|extends)\s+["']([^"']+)["']"""
    )
    templates = Path(app.root_path) / "templates"
    referenced = {
        (path, name)
        for path in templates.rglob("*.html")
        for name in reference.findall(path.read_text())
    }
    assert referenced, "no template cross-references found — regex broken?"

    unresolved = []
    for path, name in sorted(referenced):
        try:
            app.jinja_env.loader.get_source(app.jinja_env, name)
        except Exception:
            unresolved.append(f"{path.relative_to(templates)} -> {name}")
    assert not unresolved, f"unresolvable template references: {unresolved}"


def test_every_render_template_target_exists(app):
    """Every template name passed to render_template in app/*.py exists."""

    from pathlib import Path

    call = re.compile(r"""render_template\(\s*["']([^"']+)["']""")
    root = Path(app.root_path)
    referenced = {
        (path, name)
        for path in root.rglob("*.py")
        for name in call.findall(path.read_text())
    }
    assert referenced, "no render_template calls found — regex broken?"

    unresolved = []
    for path, name in sorted(referenced):
        try:
            app.jinja_env.loader.get_source(app.jinja_env, name)
        except Exception:
            unresolved.append(f"{path.relative_to(root)} -> {name}")
    assert not unresolved, f"missing render_template targets: {unresolved}"
