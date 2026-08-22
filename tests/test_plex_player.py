"""Remote playback on each user's own device (Plex Companion):
ratingKey resolution by TMDb guid with a verified title-search
fallback, the empty-play-queue guard, the exact hand-off the player
was validated with, per-user targeting, the play route, the popover
card's button, and the Profile page's probe-and-save device flow."""

import json
import re

from types import SimpleNamespace

import pytest

from tests.factories import make_movie

SERVER_CONFIG = {
    "PLEX_URL": "http://plex.test",
    "PLEX_TOKEN": "token",
    "PLEX_PLAYER_SERVER_URI": "https://plex-direct.test:32400",
}


def device_user(address="192.168.1.247:32500", machine_id="ATV-MACHINE-ID"):
    """A stand-in user carrying a playback device."""

    return SimpleNamespace(
        plex_player_address=address,
        plex_player_id=machine_id,
        plex_player_configured=bool(address and machine_id),
    )


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
def server_config(app, monkeypatch):
    for key, value in SERVER_CONFIG.items():
        monkeypatch.setitem(app.config, key, value)


@pytest.fixture
def member_device(app):
    """Give the member user a playback device for the duration of one
    test — the user table survives clean_state, so this must restore."""

    from app import db
    from app.models import User
    from tests.conftest import MEMBER_EMAIL

    with app.app_context():
        user = User.query.filter_by(email=MEMBER_EMAIL).one()
        user.plex_player_address = "192.168.1.63:32500"
        user.plex_player_id = "MEMBER-ATV-ID"
        db.session.commit()
    yield
    with app.app_context():
        user = User.query.filter_by(email=MEMBER_EMAIL).one()
        user.plex_player_address = None
        user.plex_player_id = None
        db.session.commit()


def _wire(monkeypatch, fake):
    import app.plex_player as plex_player

    monkeypatch.setattr(plex_player, "_plex_get", fake.get)
    monkeypatch.setattr(plex_player.requests, "post", fake.post)
    monkeypatch.setattr(plex_player.requests, "get", fake.player_get)
    return plex_player


def test_plays_via_guid_lookup(app, monkeypatch, server_config):
    fake = FakePlex(guid_hits=[{"ratingKey": "189344"}])
    plex_player = _wire(monkeypatch, fake)

    with app.app_context():
        movie = make_movie("Jaws", 1975, tmdb_id=578)
        ok, message = plex_player.play_movie(movie, device_user())

    assert ok is True

    # The queue names the SERVER machine id — an empty one still returns
    # a playQueueID, so this is the field that actually matters
    [queue] = fake.queue_posts
    assert queue["uri"] == (
        "server://SERVER-ID/com.plexapp.plugins.library/library/metadata/189344"
    )

    # The player hand-off: the USER'S device address and machine id,
    # plus the server coordinates the player can actually reach
    [command] = fake.player_gets
    assert command["url"] == ("http://192.168.1.247:32500/player/playback/playMedia")
    assert command["headers"]["X-Plex-Target-Client-Identifier"] == "ATV-MACHINE-ID"
    assert command["params"]["protocol"] == "https"
    assert command["params"]["address"] == "plex-direct.test"
    assert command["params"]["port"] == 32400
    assert command["params"]["containerKey"] == "/playQueues/6572?window=100&own=1"
    assert command["params"]["key"] == "/library/metadata/189344"
    assert command["params"]["token"] == "token"


def test_each_user_plays_on_their_own_device(app, monkeypatch, server_config):
    fake = FakePlex(guid_hits=[{"ratingKey": "189344"}])
    plex_player = _wire(monkeypatch, fake)

    with app.app_context():
        movie = make_movie("Jaws", 1975, tmdb_id=578)
        plex_player.play_movie(movie, device_user("10.0.0.5:32500", "ATV-A"))
        plex_player.play_movie(movie, device_user("100.101.0.9:32500", "ATV-B"))

    first, second = fake.player_gets
    assert first["url"].startswith("http://10.0.0.5:32500/")
    assert first["headers"]["X-Plex-Target-Client-Identifier"] == "ATV-A"
    assert second["url"].startswith("http://100.101.0.9:32500/")
    assert second["headers"]["X-Plex-Target-Client-Identifier"] == "ATV-B"


def test_falls_back_to_search_verified_by_tmdb_guid(app, monkeypatch, server_config):
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
        ok, _ = plex_player.play_movie(movie, device_user())

    assert ok is True
    assert "metadata/222" in fake.queue_posts[0]["uri"]


def test_missing_movie_reports_without_touching_the_player(
    app, monkeypatch, server_config
):
    fake = FakePlex()
    plex_player = _wire(monkeypatch, fake)

    with app.app_context():
        movie = make_movie("Nowhere Film", 1999, tmdb_id=42)
        ok, message = plex_player.play_movie(movie, device_user())

    assert ok is False
    assert "doesn't have" in message
    assert fake.queue_posts == []
    assert fake.player_gets == []


def test_empty_play_queue_is_refused(app, monkeypatch, server_config):
    """The validation trap: a queue can be created with zero items and
    still return a playQueueID — the player must never be handed one."""

    fake = FakePlex(guid_hits=[{"ratingKey": "189344"}], queue_count=0)
    plex_player = _wire(monkeypatch, fake)

    with app.app_context():
        movie = make_movie("Jaws", 1975, tmdb_id=578)
        ok, message = plex_player.play_movie(movie, device_user())

    assert ok is False
    assert "empty play queue" in message
    assert fake.player_gets == []


def test_unreachable_player_blames_the_closed_app(app, monkeypatch, server_config):
    import requests

    fake = FakePlex(guid_hits=[{"ratingKey": "189344"}])
    plex_player = _wire(monkeypatch, fake)

    def refuse(url, params=None, headers=None, timeout=None):
        raise requests.ConnectionError("no route")

    monkeypatch.setattr(plex_player.requests, "get", refuse)

    with app.app_context():
        movie = make_movie("Jaws", 1975, tmdb_id=578)
        ok, message = plex_player.play_movie(movie, device_user())

    assert ok is False
    assert "Plex app open" in message


def test_unconfigured_server_short_circuits(app, monkeypatch):
    import app.plex_player as plex_player

    with app.app_context():
        movie = make_movie("Jaws", 1975, tmdb_id=578)
        ok, message = plex_player.play_movie(movie, device_user())

    assert ok is False
    assert "configured" in message


def test_user_without_device_is_pointed_at_their_profile(
    app, monkeypatch, server_config
):
    fake = FakePlex(guid_hits=[{"ratingKey": "189344"}])
    plex_player = _wire(monkeypatch, fake)

    with app.app_context():
        movie = make_movie("Jaws", 1975, tmdb_id=578)
        ok, message = plex_player.play_movie(movie, device_user(address=None))

    assert ok is False
    assert "Profile" in message
    assert fake.queue_posts == []


def test_probe_player_reads_the_machine_id(app, monkeypatch):
    import app.plex_player as plex_player

    def resources(url, headers=None, timeout=None):
        assert url == "http://192.168.1.63:32500/resources"
        response = FakeResponse({})
        response.content = (
            b'<?xml version="1.0" encoding="utf-8"?><MediaContainer size="1">'
            b'<Player machineIdentifier="MEMBER-ATV-ID" product="Plex for Apple TV"'
            b' title="Den Apple TV" protocol="plex"/></MediaContainer>'
        )
        return response

    monkeypatch.setattr(plex_player.requests, "get", resources)
    with app.app_context():
        player = plex_player.probe_player("192.168.1.63:32500")

    assert player == {"machine_id": "MEMBER-ATV-ID", "name": "Den Apple TV"}


def test_probe_player_answers_none_when_nothing_listens(app, monkeypatch):
    import requests

    import app.plex_player as plex_player

    def refuse(url, headers=None, timeout=None):
        raise requests.ConnectionError("no route")

    monkeypatch.setattr(plex_player.requests, "get", refuse)
    with app.app_context():
        assert plex_player.probe_player("192.168.1.63:32500") is None


# --- The play route ---


def _committed_movie(app, **kwargs):
    from app import db

    with app.app_context():
        movie = make_movie("Jaws", 1975, tmdb_id=578, **kwargs)
        movie_id = movie.id
        db.session.commit()
    return movie_id


def test_play_route_uses_the_users_device(
    app, monkeypatch, user_client, server_config, member_device
):
    fake = FakePlex(guid_hits=[{"ratingKey": "189344"}])
    _wire(monkeypatch, fake)
    movie_id = _committed_movie(app)

    r = user_client.post(
        f"/movie/{movie_id}/play", headers={"X-Requested-With": "play"}
    )
    assert r.status_code == 200
    state = json.loads(r.data)
    assert state["ok"] is True
    [command] = fake.player_gets
    assert command["url"].startswith("http://192.168.1.63:32500/")
    assert command["headers"]["X-Plex-Target-Client-Identifier"] == "MEMBER-ATV-ID"


def test_play_route_without_a_device_reports_kindly(
    app, monkeypatch, user_client, server_config
):
    movie_id = _committed_movie(app)

    r = user_client.post(
        f"/movie/{movie_id}/play", headers={"X-Requested-With": "play"}
    )
    assert r.status_code == 502
    state = json.loads(r.data)
    assert state["ok"] is False
    assert "Profile" in state["message"]


# --- The popover card's button ---


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


def test_popover_card_carries_the_play_button(
    app, user_client, server_config, member_device
):
    movie_id = _owned_movie(app)
    card = user_client.get(f"/movie_card?movie_id={movie_id}").get_data(as_text=True)
    assert f'action="/movie/{movie_id}/play"' in card
    assert "Play on Apple TV" in card


def test_popover_card_hides_the_button_without_a_device(
    app, user_client, server_config
):
    movie_id = _owned_movie(app)
    card = user_client.get(f"/movie_card?movie_id={movie_id}").get_data(as_text=True)
    assert "Play on Apple TV" not in card


def test_popover_card_hides_the_button_when_server_unconfigured(
    app, user_client, member_device
):
    movie_id = _owned_movie(app)
    card = user_client.get(f"/movie_card?movie_id={movie_id}").get_data(as_text=True)
    assert "Play on Apple TV" not in card


def test_popover_card_hides_the_button_on_unowned_films(
    app, user_client, server_config, member_device
):
    from app import db

    with app.app_context():
        movie = make_movie("Wish List Film", 2001, tmdb_id=901)
        movie_id = movie.id
        db.session.commit()

    card = user_client.get(f"/movie_card?movie_id={movie_id}").get_data(as_text=True)
    assert "Play on Apple TV" not in card


# --- The Profile page's device flow ---


def _profile_post(client, address):
    """POST the playback-device form with a scraped csrf token."""

    page = client.get("/profile").get_data(as_text=True)
    match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page)
    assert match, "no csrf token found on the profile page"
    return client.post(
        "/profile",
        data={
            "csrf_token": match.group(1),
            "plex_player_address": address,
            "plex_player_submit": "1",
        },
        follow_redirects=True,
    )


def test_profile_probe_saves_a_verified_device(
    app, monkeypatch, user_client, server_config
):
    import app.main.account as account

    monkeypatch.setattr(
        account,
        "probe_player",
        lambda address: {"machine_id": "PROBED-ID", "name": "Den Apple TV"},
    )

    r = _profile_post(user_client, "192.168.1.63")
    assert "Den Apple TV" in r.get_data(as_text=True)

    from app.models import User
    from tests.conftest import MEMBER_EMAIL

    with app.app_context():
        user = User.query.filter_by(email=MEMBER_EMAIL).one()
        # The bare IP was completed with Companion's port
        assert user.plex_player_address == "192.168.1.63:32500"
        assert user.plex_player_id == "PROBED-ID"
        user.plex_player_address = None
        user.plex_player_id = None
        from app import db

        db.session.commit()


def test_profile_probe_failure_saves_nothing(
    app, monkeypatch, user_client, server_config
):
    import app.main.account as account

    monkeypatch.setattr(account, "probe_player", lambda address: None)

    r = _profile_post(user_client, "192.168.1.63:32500")
    assert "No Plex player answered" in r.get_data(as_text=True)

    from app.models import User
    from tests.conftest import MEMBER_EMAIL

    with app.app_context():
        user = User.query.filter_by(email=MEMBER_EMAIL).one()
        assert user.plex_player_address is None
        assert user.plex_player_id is None


def test_profile_blank_clears_the_device(
    app, user_client, server_config, member_device
):
    _profile_post(user_client, "")

    from app.models import User
    from tests.conftest import MEMBER_EMAIL

    with app.app_context():
        user = User.query.filter_by(email=MEMBER_EMAIL).one()
        assert user.plex_player_address is None
        assert user.plex_player_id is None


def test_profile_rejects_a_malformed_address(
    app, monkeypatch, user_client, server_config
):
    import app.main.account as account

    probed = []
    monkeypatch.setattr(account, "probe_player", lambda address: probed.append(address))

    r = _profile_post(user_client, "192.168.1.63/evil?x=1")
    assert "ip:port or hostname:port" in r.get_data(as_text=True)
    assert probed == []
