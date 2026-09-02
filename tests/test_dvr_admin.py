"""DVR channel editor (#182): admin gating, channel CRUD, explicit
member picks resolved by title, and the build honoring the edited
definitions (explicit picks air; disabled channels don't)."""

import re

import pytest

from app.models import DVRChannel, db
from tests.conftest import dvr_rebuild_jobs
from tests.factories import make_movie, make_movie_file, make_tv_file, make_tv_series


@pytest.fixture(autouse=True)
def library_present(monkeypatch):
    """These tests seed rows, not files: every row reads as on disk
    and the shares as online."""

    from app import dvr

    monkeypatch.setattr(dvr, "_on_disk", lambda file: True)
    monkeypatch.setattr(dvr, "_library_online", lambda: True)


def csrf_token_from(page_html):
    match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page_html)
    return match.group(1)


def rebuild_jobs(app):
    return dvr_rebuild_jobs(app)


def test_editor_requires_admin(client, user_client):
    response = client.get("/dvr/channels")
    assert response.status_code == 302 and "/auth/login" in response.headers["Location"]
    response = user_client.get("/dvr/channels")
    assert response.status_code == 302 and "/auth/login" not in (
        response.headers["Location"]
    )


def test_create_edit_delete_channel(app, admin_client):
    page = admin_client.get("/dvr/channels").get_data(as_text=True)
    token = csrf_token_from(page)

    response = admin_client.post(
        "/dvr/channels",
        data={
            "csrf_token": token,
            "name": "Westerns Forever",
            "number": "300",
            "enabled": "y",
            "include_movies": "y",
            "save_submit": "Save channel",
        },
    )
    assert response.status_code == 302
    with app.app_context():
        channel = DVRChannel.query.filter_by(number=300).one()
        assert channel.slug == "westerns-forever"
        assert channel.include_movies and not channel.include_tv
        channel_id = channel.id
    assert rebuild_jobs(app)

    # A colliding number is refused with an explanation

    response = admin_client.post(
        "/dvr/channels",
        data={
            "csrf_token": token,
            "name": "Different Name",
            "number": "300",
            "save_submit": "Save channel",
        },
        follow_redirects=True,
    )
    assert "already uses" in response.get_data(as_text=True)
    with app.app_context():
        assert DVRChannel.query.count() == 1

    # Editing renames and re-rules the channel but never moves its slug

    response = admin_client.post(
        f"/dvr/channels/{channel_id}",
        data={
            "csrf_token": token,
            "name": "Cowboy Channel",
            "number": "301",
            "enabled": "y",
            "include_movies": "y",
            "genres": "Western",
            "save_submit": "Save channel",
        },
    )
    assert response.status_code == 302
    with app.app_context():
        channel = db.session.get(DVRChannel, channel_id)
        assert (channel.name, channel.number) == ("Cowboy Channel", 301)
        assert channel.genres == "Western"
        assert channel.slug == "westerns-forever"

    response = admin_client.post(
        "/dvr/channels",
        data={
            "csrf_token": token,
            "channel_id": str(channel_id),
            "delete_submit": "Delete",
        },
    )
    assert response.status_code == 302
    with app.app_context():
        assert DVRChannel.query.count() == 0


def test_member_add_by_title(app, admin_client):
    with app.app_context():
        make_movie("Jaws", 1975)
        make_movie("Alien", 1979)
        make_movie("Aliens", 1986)
        make_tv_series("Doctor Who (1963)")
        channel = DVRChannel(number=310, name="Picks", slug="picks")
        db.session.add(channel)
        db.session.commit()
        channel_id = channel.id

    page = admin_client.get(f"/dvr/channels/{channel_id}").get_data(as_text=True)
    token = csrf_token_from(page)

    response = admin_client.post(
        f"/dvr/channels/{channel_id}",
        data={
            "csrf_token": token,
            "member_title": "Jaws (1975)",
            "add_movie_submit": "Add movie",
        },
        follow_redirects=True,
    )
    assert "Added Jaws (1975)" in response.get_data(as_text=True)

    # An ambiguous fragment is refused with suggestions

    response = admin_client.post(
        f"/dvr/channels/{channel_id}",
        data={
            "csrf_token": token,
            "member_title": "Alien",
            "add_movie_submit": "Add movie",
        },
        follow_redirects=True,
    )
    assert "ambiguous" in response.get_data(as_text=True)

    response = admin_client.post(
        f"/dvr/channels/{channel_id}",
        data={
            "csrf_token": token,
            "member_title": "Doctor Who (1963)",
            "add_series_submit": "Add series",
        },
        follow_redirects=True,
    )
    assert "Added Doctor Who (1963)" in response.get_data(as_text=True)

    with app.app_context():
        channel = db.session.get(DVRChannel, channel_id)
        assert [movie.title for movie in channel.movies] == ["Jaws"]
        assert [show.title for show in channel.series] == ["Doctor Who (1963)"]
        movie_id = channel.movies.first().id

    # Removing the movie leaves the series pick in place

    response = admin_client.post(
        f"/dvr/channels/{channel_id}",
        data={
            "csrf_token": token,
            "member_kind": "movie",
            "member_id": str(movie_id),
            "remove_submit": "Remove",
        },
    )
    assert response.status_code == 302
    with app.app_context():
        channel = db.session.get(DVRChannel, channel_id)
        assert channel.movies.count() == 0
        assert channel.series.count() == 1


def test_list_page_shows_plex_setup_urls(app, admin_client, monkeypatch):
    page = admin_client.get("/dvr/channels").get_data(as_text=True)
    assert "http://127.0.0.1:8000/dvr/dvr-test-token" in page
    assert "http://127.0.0.1:8000/dvr/dvr-test-token/guide.xml" in page

    # Feature off: the hint replaces the URLs

    monkeypatch.setitem(app.config, "DVR_TOKEN", None)
    page = admin_client.get("/dvr/channels").get_data(as_text=True)
    assert "dvr-test-token" not in page
    assert "DVR_TOKEN" in page


def test_title_search_returns_canonical_pick_strings(app, admin_client, user_client):
    with app.app_context():
        make_movie("Jaws", 1975)
        make_movie("Jaws 2", 1978)
        make_tv_series("Doctor Who (1963)")
        db.session.commit()

    response = admin_client.get("/dvr/title-search.json?kind=movie&q=jaws")
    assert response.get_json()["results"] == ["Jaws (1975)", "Jaws 2 (1978)"]

    response = admin_client.get("/dvr/title-search.json?kind=series&q=doctor")
    assert response.get_json()["results"] == ["Doctor Who (1963)"]

    # Under two characters returns nothing; non-admins are bounced

    assert admin_client.get("/dvr/title-search.json?q=j").get_json() == {"results": []}
    assert user_client.get("/dvr/title-search.json?q=jaws").status_code == 302


def test_build_honors_picks_and_disabled(app, monkeypatch):
    from app import dvr

    monkeypatch.setattr(dvr, "_probe_duration", lambda path: 3000.0)
    with app.app_context():
        movie = make_movie("Jaws", 1975)
        make_movie_file(movie, "Bluray-1080p")
        series = make_tv_series("Doctor Who (1963)")
        for episode in range(1, 4):
            make_tv_file(series, 1, episode, "Bluray-1080p")

        picks = DVRChannel(number=310, name="Picks", slug="picks")
        picks.movies.append(movie)
        picks.series.append(series)
        dark = DVRChannel(
            number=311, name="Dark", slug="dark", enabled=False, include_movies=True
        )
        db.session.add_all([picks, dark])
        db.session.commit()
        assert dvr.build_channel_lineups() is True

    channels = {c["slug"] for c in dvr.channel_index(app.redis)}
    assert channels == {"picks"}

    programs = dvr.channel_lineup(app.redis, "picks")["programs"]
    titles = {p["title"] for p in programs}
    assert titles == {"Jaws", "Doctor Who (1963)"}
    # The film spaces itself into the episode cycle rather than
    # bolting onto the end
    assert programs[-1]["title"] == "Jaws" or programs[-2]["title"] == "Jaws"
    assert len(programs) == 4


def test_manual_rebuild_reports_when_nothing_was_queued(app, admin_client):
    page = admin_client.get("/dvr/channels").get_data(as_text=True)
    token = csrf_token_from(page)
    first = admin_client.post(
        "/dvr/channels",
        data={"csrf_token": token, "rebuild_submit": "1"},
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "Rebuilding the channel lineups" in first
    second = admin_client.post(
        "/dvr/channels",
        data={"csrf_token": token, "rebuild_submit": "1"},
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "No rebuild queued" in second
    assert len(rebuild_jobs(app)) == 1
