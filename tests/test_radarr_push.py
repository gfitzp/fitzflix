"""The ad-hoc Radarr hand-off (#66): per-film request and withdrawal
with the house settings, the watchlist and movie page buttons, and the
badge cache."""

import re

from app import db
from app.models import UserWatchlist
from tests.factories import make_movie, make_movie_file


def csrf_token_from(page_html):
    match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page_html)
    assert match, "no csrf token found in page"
    return match.group(1)


class FakeRadarr:
    """A stateful stand-in for the Radarr v3 API."""

    def __init__(self):
        self.movies = {}  # radarr id -> movie dict
        self.next_id = 100
        self.added = []
        self.deleted = []

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
    assert added["qualityProfileId"] == 7  # "Fitzflix", resolved by name
    assert added["rootFolderPath"] == "/Volumes/Movies"
    assert added["monitored"] is True
    assert added["minimumAvailability"] == "released"
    assert added["addOptions"] == {"monitor": "movieOnly", "searchForMovie": True}

    # The page now badges the request and offers withdrawal instead

    page = admin_client.get(f"/movie/{movie_id}").get_data(as_text=True)
    assert "Requested via Radarr" in page
    assert "Remove from Radarr" in page
    assert "radarr_request_submit" not in page


def test_withdraw_deletes_keeping_files(app, admin_client, monkeypatch):
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


def test_watchlist_rows_offer_request_and_unrequest(app, admin_client, monkeypatch):
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

    # The unowned row offers Request; the owned row offers nothing

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
    page = admin_client.get("/watchlist").get_data(as_text=True)
    assert "Requested via Radarr" in page
    assert "Un-request" in page
    assert "radarr_request_submit" not in page


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

    # Non-admins are turned away entirely
    assert user_client.post("/radarr", data={}).status_code == 302
