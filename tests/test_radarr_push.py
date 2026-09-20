"""Test the ad-hoc Radarr hand-off.

The tests cover the per-film request and withdrawal with the house
settings. They also cover the Find-menu entries on the watchlist and
the movie page, and the badge cache."""

import re

from app import db
from app.models import UserWatchlist
from tests.factories import make_movie, make_movie_file


def csrf_token_from(page_html):
    match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page_html)
    assert match, "no csrf token found in page"
    return match.group(1)


class FakeRadarr:
    """A stand-in for the Radarr v3 API that keeps state."""

    def __init__(self):
        self.movies = {}  # radarr id -> movie dict
        self.next_id = 100
        self.added = []
        self.deleted = []
        self.updated = []
        self.commands = []

    def call(self, method, path, payload=None):
        if method == "GET" and "/qualityprofile" in path:
            return [{"id": 2, "name": "SD"}, {"id": 7, "name": "Fitzflix"}]
        if method == "GET" and "/rootfolder" in path:
            return [{"path": "/Volumes/Movies"}]
        if method == "GET" and "/movie/lookup/tmdb" in path:
            tmdb_id = int(path.split("tmdbId=")[1])
            return {"title": f"Film {tmdb_id}", "tmdbId": tmdb_id, "year": 2000}
        if method == "GET" and "/movie?tmdbId=" in path:
            tmdb_id = int(path.split("tmdbId=")[1])
            return [m for m in self.movies.values() if m["tmdbId"] == tmdb_id]
        if method == "GET" and path.endswith("/movie"):
            return list(self.movies.values())
        if method == "POST" and path.endswith("/movie"):
            payload = dict(payload)
            payload["id"] = self.next_id
            self.movies[self.next_id] = payload
            self.added.append(payload)
            self.next_id += 1
            return payload
        if method == "PUT" and "/movie/" in path:
            radarr_id = int(path.split("/movie/")[1].split("?")[0])
            self.movies[radarr_id] = dict(payload)
            self.updated.append((radarr_id, path, dict(payload)))
            return payload
        if method == "POST" and path.endswith("/command"):
            self.commands.append(dict(payload))
            return payload
        if method == "DELETE" and "/movie/" in path:
            radarr_id = int(path.split("/movie/")[1].split("?")[0])
            self.deleted.append((radarr_id, path))
            self.movies.pop(radarr_id, None)
            return None
        raise AssertionError(f"unexpected {method} {path}")


def wire(app, monkeypatch):
    import app.radarr_push as radarr_push

    fake = FakeRadarr()
    monkeypatch.setattr(radarr_push, "_radarr", fake.call)
    return fake


def test_request_uses_the_house_settings(app, admin_client, monkeypatch):
    fake = wire(app, monkeypatch)
    with app.app_context():
        movie = make_movie("Major League", 1989, tmdb_id=9942)
        db.session.commit()
        movie_id = movie.id

    page = admin_client.get(f"/movie/{movie_id}").get_data(as_text=True)
    assert "Request via Radarr" in page

    response = admin_client.post(
        f"/radarr?origin=/movie/{movie_id}",
        data={
            "csrf_token": csrf_token_from(page),
            "movie_id": movie_id,
            "radarr_request_submit": "Request via Radarr",
        },
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith(f"/movie/{movie_id}")

    (added,) = fake.added
    assert added["tmdbId"] == 9942
    assert added["qualityProfileId"] == 7  # "Fitzflix", found by name
    assert added["rootFolderPath"] == "/Volumes/Movies"
    assert added["monitored"] is True
    assert added["minimumAvailability"] == "released"
    assert added["addOptions"] == {"monitor": "movieOnly", "searchForMovie": True}

    # The page now shows a badge for the request. The Find-menu entry
    # returns to the plain Radarr link. The withdrawal occurs in Radarr

    page = admin_client.get(f"/movie/{movie_id}").get_data(as_text=True)
    assert "Requested via Radarr" in page
    assert 'title="Radarr is monitoring this film">Radarr</a>' in page
    assert "radarr_request_submit" not in page
    assert "radarr_remove_submit" not in page


def test_withdraw_deletes_keeping_files(app, admin_client, monkeypatch):
    # This test is route-level only because the Un-request entry left
    # the UI (2026-08). It stays as the counterpart to the request branch
    fake = wire(app, monkeypatch)
    with app.app_context():
        movie = make_movie("Major League", 1989, tmdb_id=9942)
        db.session.commit()
        movie_id = movie.id

    page = admin_client.get(f"/movie/{movie_id}").get_data(as_text=True)
    token = csrf_token_from(page)
    admin_client.post(
        "/radarr",
        data={
            "csrf_token": token,
            "movie_id": movie_id,
            "radarr_request_submit": "Request via Radarr",
        },
    )
    response = admin_client.post(
        "/radarr",
        data={
            "csrf_token": token,
            "movie_id": movie_id,
            "radarr_remove_submit": "Remove from Radarr",
        },
        follow_redirects=True,
    )
    assert "Removed" in response.get_data(as_text=True)
    ((radarr_id, path),) = fake.deleted
    assert "deleteFiles=false" in path
    assert fake.movies == {}


def test_watchlist_tiles_offer_request_then_plain_link(app, admin_client, monkeypatch):
    wire(app, monkeypatch)
    with app.app_context():
        user_id = 1
        wanted = make_movie("Wanted Film", 2001, tmdb_id=201)
        owned = make_movie("Owned Film", 2002, tmdb_id=202)
        make_movie_file(owned, "Bluray-1080p")
        db.session.add(UserWatchlist(user_id=user_id, movie_id=wanted.id))
        db.session.add(UserWatchlist(user_id=user_id, movie_id=owned.id))
        db.session.commit()
        wanted_id = wanted.id

    page = admin_client.get("/watchlist").get_data(as_text=True)

    # The unowned tile has the Find menu with the Request entry. The
    # owned tile has no Find menu

    assert page.count("dropdown-toggle-split") == 1
    assert page.count("radarr_request_submit") == 1
    assert f'value="{wanted_id}"' in page

    admin_client.post(
        "/radarr?origin=/watchlist",
        data={
            "csrf_token": csrf_token_from(page),
            "movie_id": wanted_id,
            "radarr_request_submit": "Request via Radarr",
        },
    )
    # After the request, the menu entry returns to the plain Radarr
    # link. Its title says that Radarr monitors the film. There is no
    # Un-request entry
    page = admin_client.get("/watchlist").get_data(as_text=True)
    assert "Un-request" not in page
    assert 'title="Radarr is monitoring this film">Radarr</a>' in page
    assert "radarr_request_submit" not in page
    assert "radarr_remove_submit" not in page


def test_request_refuses_owned_films_and_non_admins(
    app, admin_client, user_client, monkeypatch
):
    fake = wire(app, monkeypatch)
    assert fake.added == []
    with app.app_context():
        owned = make_movie("Owned Film", 2002, tmdb_id=202)
        make_movie_file(owned, "Bluray-1080p")
        db.session.commit()
        owned_id = owned.id

    page = admin_client.get(f"/movie/{owned_id}").get_data(as_text=True)
    assert "Request via Radarr" not in page

    response = admin_client.post(
        "/radarr",
        data={
            "csrf_token": csrf_token_from(page),
            "movie_id": owned_id,
            "radarr_request_submit": "Request via Radarr",
        },
        follow_redirects=True,
    )
    assert "already in the library" in response.get_data(as_text=True)
    assert fake.added == []

    # The route refuses all non-admins
    assert user_client.post("/radarr", data={}).status_code == 302


def refresh_rename_fixture(app, monkeypatch, title, year, tmdb_id, new_year):
    """Make a movie whose TMDB refresh moves its file to a new folder."""

    import os
    from datetime import date

    from app import tmdb_refresh
    from tests.factories import make_movie, make_movie_file

    movie = make_movie(title, year, tmdb_id=tmdb_id)
    file = make_movie_file(movie, "HDTV-720p")
    file.untouched_basename = file.basename
    movie.tmdb_title = title
    movie.tmdb_release_date = date(new_year, 1, 1)
    db.session.commit()
    old_path = os.path.join(app.config["LIBRARY_DIR"], file.file_path)
    os.makedirs(os.path.dirname(old_path), exist_ok=True)
    with open(old_path, "wb") as handle:
        handle.write(b"payload")
    monkeypatch.setattr(tmdb_refresh, "rename_untouched_object", lambda *a, **k: False)
    return movie.id


def test_refresh_rename_points_radarr_at_the_new_folder(app, monkeypatch):
    """A folder rename must reach Radarr, or Radarr downloads the film again."""

    from app.videos import apply_tmdb_refresh

    fake = wire(app, monkeypatch)
    fake.movies[5] = {
        "id": 5,
        "tmdbId": 777,
        "path": "/Volumes/Movies/Radarr Tune (1943)",
    }
    with app.app_context():
        movie_id = refresh_rename_fixture(
            app, monkeypatch, "Radarr Tune", 1943, 777, 1944
        )
        assert apply_tmdb_refresh("Movies", movie_id) is True

    assert [(i, p, m["path"]) for i, p, m in fake.updated] == [
        (5, "/api/v3/movie/5?moveFiles=false", "/Volumes/Movies/Radarr Tune (1944)")
    ]
    assert fake.commands == [{"name": "RefreshMovie", "movieIds": [5]}]
    assert fake.deleted == []


def test_refresh_merge_withdraws_the_old_radarr_entry(app, monkeypatch):
    """A record that moves to an other TMDB id leaves Radarr under the old id.

    The file belongs to the other film now. Radarr keeps the files on
    the disk. The entry of the new id gets a rescan."""

    import os
    from datetime import date

    from app import tmdb_refresh
    from app.videos import apply_tmdb_refresh
    from tests.factories import make_movie, make_movie_file

    fake = wire(app, monkeypatch)
    monkeypatch.setattr(tmdb_refresh, "rename_untouched_object", lambda *a, **k: False)
    fake.movies[8] = {
        "id": 8,
        "tmdbId": 1111,
        "path": "/Volumes/Movies/Duplicate Entry (2001)",
    }
    fake.movies[9] = {
        "id": 9,
        "tmdbId": 4242,
        "path": "/Volumes/Movies/Canonical Entry (2001)",
    }
    with app.app_context():
        source = make_movie("Duplicate Entry", 2001, tmdb_id=1111)
        file = make_movie_file(source, "DVD")
        file.untouched_basename = file.basename
        target = make_movie("Canonical Entry", 2001, tmdb_id=4242)
        target.tmdb_title = "Canonical Entry"
        target.tmdb_release_date = date(2001, 6, 1)
        db.session.commit()
        source_id = source.id
        old_path = os.path.join(app.config["LIBRARY_DIR"], file.file_path)
        os.makedirs(os.path.dirname(old_path), exist_ok=True)
        with open(old_path, "wb") as handle:
            handle.write(b"payload")

        assert apply_tmdb_refresh("Movies", source_id, tmdb_id=4242) is True

    assert [radarr_id for radarr_id, _ in fake.deleted] == [8]
    assert fake.deleted[0][1].endswith("?deleteFiles=false&addImportExclusion=false")
    assert fake.updated == []
    assert fake.commands == [{"name": "RefreshMovie", "movieIds": [9]}]


def test_refresh_rename_skips_a_film_radarr_lacks(app, monkeypatch):
    from app.videos import apply_tmdb_refresh

    fake = wire(app, monkeypatch)
    with app.app_context():
        movie_id = refresh_rename_fixture(
            app, monkeypatch, "Quiet Film", 1950, 888, 1951
        )
        assert apply_tmdb_refresh("Movies", movie_id) is True

    assert fake.updated == []
    assert fake.commands == []
    assert fake.deleted == []


def test_refresh_rename_survives_a_radarr_outage(app, monkeypatch):
    """A Radarr failure must not fail the refresh. The rename already happened."""

    import app.radarr_push as radarr_push
    from app.videos import apply_tmdb_refresh

    def down(*args, **kwargs):
        raise ConnectionError("radarr is down")

    monkeypatch.setattr(radarr_push, "_radarr", down)
    with app.app_context():
        movie_id = refresh_rename_fixture(
            app, monkeypatch, "Storm Film", 1960, 999, 1961
        )
        assert apply_tmdb_refresh("Movies", movie_id) is True
