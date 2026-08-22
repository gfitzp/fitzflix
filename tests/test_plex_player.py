"""Remote playback on the Apple TV (Plex Companion): ratingKey
resolution by TMDb guid with a verified title-search fallback, the
empty-play-queue guard, the exact hand-off the player was validated
with, and the admin-only play route."""

import json

import pytest

from tests.factories import make_movie

PLAYER_CONFIG = {
    "PLEX_URL": "http://plex.test",
    "PLEX_TOKEN": "token",
    "PLEX_PLAYER_ADDRESS": "192.168.1.247:32500",
    "PLEX_PLAYER_ID": "ATV-MACHINE-ID",
    "PLEX_PLAYER_SERVER_URI": "https://plex-direct.test:32400",
}


class FakePlex:
    """The server's GET endpoints plus recorded queue/player requests."""

    def __init__(self, guid_hits=None, search_hits=None, queue_count=1):
        self.guid_hits = guid_hits or []
        self.search_hits = search_hits or []
        self.queue_count = queue_count
        self.metadata = {}  # ratingKey -> [guid ids]
        self.queue_posts = []
        self.player_gets = []

    def get(self, path, params=None):
        if path == "/":
            return {"MediaContainer": {"machineIdentifier": "SERVER-ID"}}
        if path == "/library/all":
            return {"MediaContainer": {"Metadata": self.guid_hits}}
        if path == "/search":
            return {"MediaContainer": {"Metadata": self.search_hits}}
        if path.startswith("/library/metadata/"):
            rating_key = path.rsplit("/", 1)[1]
            return {
                "MediaContainer": {
                    "Metadata": [
                        {"Guid": [{"id": g} for g in self.metadata.get(rating_key, [])]}
                    ]
                }
            }
        raise AssertionError(f"unexpected GET {path}")

    def post(self, url, params=None, headers=None, timeout=None):
        assert url == "http://plex.test/playQueues"
        self.queue_posts.append(params)
        return FakeResponse(
            {
                "MediaContainer": {
                    "playQueueID": 6572,
                    "playQueueTotalCount": self.queue_count,
                }
            }
        )

    def player_get(self, url, params=None, headers=None, timeout=None):
        self.player_gets.append({"url": url, "params": params, "headers": headers})
        return FakeResponse({})


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload

    def raise_for_status(self):
        assert self.status_code == 200


@pytest.fixture
def player_config(app, monkeypatch):
    for key, value in PLAYER_CONFIG.items():
        monkeypatch.setitem(app.config, key, value)


def _wire(monkeypatch, fake):
    import app.plex_player as plex_player

    monkeypatch.setattr(plex_player, "_plex_get", fake.get)
    monkeypatch.setattr(plex_player.requests, "post", fake.post)
    monkeypatch.setattr(plex_player.requests, "get", fake.player_get)
    return plex_player


def test_plays_via_guid_lookup(app, monkeypatch, player_config):
    fake = FakePlex(guid_hits=[{"ratingKey": "189344"}])
    plex_player = _wire(monkeypatch, fake)

    with app.app_context():
        movie = make_movie("Jaws", 1975, tmdb_id=578)
        ok, message = plex_player.play_movie(movie)

    assert ok is True

    # The queue names the SERVER machine id — an empty one still returns
    # a playQueueID, so this is the field that actually matters
    [queue] = fake.queue_posts
    assert queue["uri"] == (
        "server://SERVER-ID/com.plexapp.plugins.library/library/metadata/189344"
    )

    # The player hand-off: Companion address, target header, and the
    # plex.direct server coordinates the Apple TV can actually reach
    [command] = fake.player_gets
    assert command["url"] == ("http://192.168.1.247:32500/player/playback/playMedia")
    assert command["headers"]["X-Plex-Target-Client-Identifier"] == "ATV-MACHINE-ID"
    assert command["params"]["protocol"] == "https"
    assert command["params"]["address"] == "plex-direct.test"
    assert command["params"]["port"] == 32400
    assert command["params"]["containerKey"] == "/playQueues/6572?window=100&own=1"
    assert command["params"]["key"] == "/library/metadata/189344"
    assert command["params"]["token"] == "token"


def test_falls_back_to_search_verified_by_tmdb_guid(app, monkeypatch, player_config):
    """When the guid filter misses, the title search only accepts a
    candidate whose metadata carries the movie's own TMDb guid."""

    fake = FakePlex(
        search_hits=[
            {"type": "movie", "ratingKey": "111", "year": 1975},
            {"type": "movie", "ratingKey": "222", "year": 1975},
        ]
    )
    fake.metadata = {"111": ["tmdb://999"], "222": ["tmdb://578"]}
    plex_player = _wire(monkeypatch, fake)

    with app.app_context():
        movie = make_movie("Jaws", 1975, tmdb_id=578)
        ok, _ = plex_player.play_movie(movie)

    assert ok is True
    assert "metadata/222" in fake.queue_posts[0]["uri"]


def test_missing_movie_reports_without_touching_the_player(
    app, monkeypatch, player_config
):
    fake = FakePlex()
    plex_player = _wire(monkeypatch, fake)

    with app.app_context():
        movie = make_movie("Nowhere Film", 1999, tmdb_id=42)
        ok, message = plex_player.play_movie(movie)

    assert ok is False
    assert "doesn't have" in message
    assert fake.queue_posts == []
    assert fake.player_gets == []


def test_empty_play_queue_is_refused(app, monkeypatch, player_config):
    """The validation trap: a queue can be created with zero items and
    still return a playQueueID — the player must never be handed one."""

    fake = FakePlex(guid_hits=[{"ratingKey": "189344"}], queue_count=0)
    plex_player = _wire(monkeypatch, fake)

    with app.app_context():
        movie = make_movie("Jaws", 1975, tmdb_id=578)
        ok, message = plex_player.play_movie(movie)

    assert ok is False
    assert "empty play queue" in message
    assert fake.player_gets == []


def test_unreachable_player_blames_the_closed_app(app, monkeypatch, player_config):
    import requests

    fake = FakePlex(guid_hits=[{"ratingKey": "189344"}])
    plex_player = _wire(monkeypatch, fake)

    def refuse(url, params=None, headers=None, timeout=None):
        raise requests.ConnectionError("no route")

    monkeypatch.setattr(plex_player.requests, "get", refuse)

    with app.app_context():
        movie = make_movie("Jaws", 1975, tmdb_id=578)
        ok, message = plex_player.play_movie(movie)

    assert ok is False
    assert "Plex app open" in message


def test_unconfigured_player_short_circuits(app, monkeypatch):
    import app.plex_player as plex_player

    with app.app_context():
        movie = make_movie("Jaws", 1975, tmdb_id=578)
        ok, message = plex_player.play_movie(movie)

    assert ok is False
    assert "configured" in message


def test_play_route_is_admin_only(app, monkeypatch, user_client, player_config):
    with app.app_context():
        movie = make_movie("Jaws", 1975, tmdb_id=578)
        movie_id = movie.id
        from app import db

        db.session.commit()

    r = user_client.post(
        f"/movie/{movie_id}/play", headers={"X-Requested-With": "play"}
    )
    assert r.status_code == 403


def test_play_route_returns_json_state(app, monkeypatch, admin_client, player_config):
    fake = FakePlex(guid_hits=[{"ratingKey": "189344"}])
    _wire(monkeypatch, fake)

    with app.app_context():
        movie = make_movie("Jaws", 1975, tmdb_id=578)
        movie_id = movie.id
        from app import db

        db.session.commit()

    r = admin_client.post(
        f"/movie/{movie_id}/play", headers={"X-Requested-With": "play"}
    )
    assert r.status_code == 200
    state = json.loads(r.data)
    assert state["ok"] is True
    assert "Playing" in state["message"]


def _owned_movie(app):
    """A committed library movie, returning its id."""

    from app import db
    from tests.factories import make_movie_file

    with app.app_context():
        movie = make_movie("Jaws", 1975, tmdb_id=578)
        make_movie_file(movie, "Bluray-1080p")
        movie_id = movie.id
        db.session.commit()
    return movie_id


def test_popover_card_carries_the_play_button(app, admin_client, player_config):
    movie_id = _owned_movie(app)
    card = admin_client.get(f"/movie_card?movie_id={movie_id}").get_data(as_text=True)
    assert f'action="/movie/{movie_id}/play"' in card
    assert "Play on Apple TV" in card


def test_popover_card_hides_the_button_from_members(app, user_client, player_config):
    movie_id = _owned_movie(app)
    card = user_client.get(f"/movie_card?movie_id={movie_id}").get_data(as_text=True)
    assert "Play on Apple TV" not in card


def test_popover_card_hides_the_button_when_unconfigured(app, admin_client):
    movie_id = _owned_movie(app)
    card = admin_client.get(f"/movie_card?movie_id={movie_id}").get_data(as_text=True)
    assert "Play on Apple TV" not in card


def test_popover_card_hides_the_button_on_unowned_films(
    app, admin_client, player_config
):
    from app import db

    with app.app_context():
        movie = make_movie("Wish List Film", 2001, tmdb_id=901)
        movie_id = movie.id
        db.session.commit()

    card = admin_client.get(f"/movie_card?movie_id={movie_id}").get_data(as_text=True)
    assert "Play on Apple TV" not in card
