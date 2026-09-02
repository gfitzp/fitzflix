"""Test remote playback on the device of each user (Plex Companion).

These tests cover the ratingKey resolution by TMDB guid with a verified
title-search fallback, the empty-play-queue guard, the exact hand-off
that was validated with the player, the per-user targeting, the play
route, the button on the popover card, and the probe-and-save device
flow on the Profile page."""

import json
import re

from types import SimpleNamespace

import pytest

from tests.conftest import page_csrf_token
from tests.factories import make_movie

SERVER_CONFIG = {
    "PLEX_URL": "http://plex.test",
    "PLEX_TOKEN": "token",
    "PLEX_PLAYER_SERVER_URI": "https://plex-direct.test:32400",
}


def device_user(
    address="192.168.1.247:32500",
    machine_id="ATV-MACHINE-ID",
    admin=True,
    plex_username=None,
):
    """Provide a stand-in user with a playback device.

    The user is the admin by default. The play command of the admin
    carries the owner token."""

    return SimpleNamespace(
        id=1,
        admin=admin,
        plex_username=plex_username,
        plex_player_address=address,
        plex_player_id=machine_id,
        plex_player_configured=bool(address and machine_id),
    )


HOME_USERS = {
    "users": [
        {"uuid": "OWNER-UUID", "username": "owner", "title": "owner", "admin": True},
        {
            "uuid": "MEMBER-UUID",
            "username": None,
            "title": "Member",
            "protected": False,
        },
        {"uuid": "KID-UUID", "username": None, "title": "Kid", "protected": True},
    ]
}


class FakePlex:
    """Provide the GET endpoints of the server and record the queue and
    player requests."""

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
        if url.startswith("https://plex.tv/api/v2/home/users/"):
            # The Home switch: a token for that user, not the owner's
            uuid = url.rsplit("/", 2)[1]
            return FakeResponse({"authToken": f"HOME-TOKEN-{uuid}"})
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
        if url == "https://plex.tv/api/v2/home/users":
            return FakeResponse(HOME_USERS)
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
    """Give the member user a playback device for 1 test.

    The user table survives clean_state. Thus, this fixture must restore
    the old value."""

    from app import db
    from app.models import User
    from tests.conftest import MEMBER_EMAIL

    with app.app_context():
        user = User.query.filter_by(email=MEMBER_EMAIL).one()
        user.plex_player_address = "192.168.1.63:32500"
        user.plex_player_id = "MEMBER-ATV-ID"
        user.plex_username = "Member"
        db.session.commit()
    yield
    with app.app_context():
        user = User.query.filter_by(email=MEMBER_EMAIL).one()
        user.plex_player_address = None
        user.plex_player_id = None
        user.plex_username = None
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

    # The queue names the SERVER machine id. An empty queue still returns
    # a playQueueID. Thus, this is the field that is important
    [queue] = fake.queue_posts
    assert queue["uri"] == (
        "server://SERVER-ID/com.plexapp.plugins.library/library/metadata/189344"
    )

    # The player hand-off contains the device address and machine id of
    # the USER, plus the server coordinates that the player can reach
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
    """Test the title-search fallback when the guid filter finds nothing.

    The title search accepts only a candidate with metadata that carries
    the TMDB guid of the movie."""

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
    """Test the validation trap of an empty queue.

    Plex can create a queue with 0 items and still return a playQueueID.
    Fitzflix must never give such a queue to the player."""

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
        f"/movie/{movie_id}/play",
        data={"csrf_token": page_csrf_token(user_client)},
        headers={"X-Requested-With": "play"},
    )
    assert r.status_code == 200
    state = json.loads(r.data)
    assert state["ok"] is True
    [command] = fake.player_gets
    assert command["url"].startswith("http://192.168.1.63:32500/")
    assert command["headers"]["X-Plex-Target-Client-Identifier"] == "MEMBER-ATV-ID"
    # A member's device gets a token for THEIR Plex Home user — the
    # owner token never travels to an address a member chose — and
    # the play queue was built as that user so the token can fetch it
    assert command["params"]["token"] == "HOME-TOKEN-MEMBER-UUID"
    assert fake.queue_posts[0]["X-Plex-Token"] == "HOME-TOKEN-MEMBER-UUID"


def test_play_route_without_a_device_reports_kindly(
    app, monkeypatch, user_client, server_config
):
    movie_id = _committed_movie(app)

    r = user_client.post(
        f"/movie/{movie_id}/play",
        data={"csrf_token": page_csrf_token(user_client)},
        headers={"X-Requested-With": "play"},
    )
    assert r.status_code == 502
    state = json.loads(r.data)
    assert state["ok"] is False
    assert "Profile" in state["message"]


# --- The popover card's button ---


def _owned_movie(app):
    """Commit a library movie and return its id."""

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
    """POST the playback-device form with a csrf token from the page."""

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
        # Fitzflix added the Companion port to the bare IP
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
    assert "private-network ip:port" in r.get_data(as_text=True)
    assert probed == []


# --- Who gets which token, and where a player may live ---


def test_member_without_a_linked_home_user_never_sees_the_owner_token(
    app, monkeypatch, server_config
):
    fake = FakePlex(guid_hits=[{"ratingKey": "1"}])
    plex_player = _wire(monkeypatch, fake)
    movie = SimpleNamespace(tmdb_id=578, title="Jaws", year=1975, tmdb_title=None)
    with app.app_context():
        ok, message = plex_player.play_movie(
            movie, device_user(admin=False, plex_username=None)
        )
    assert ok is False and "Plex Home" in message
    assert fake.player_gets == [] and fake.queue_posts == []


def test_protected_home_user_is_refused(app, monkeypatch, server_config):
    fake = FakePlex(guid_hits=[{"ratingKey": "1"}])
    plex_player = _wire(monkeypatch, fake)
    movie = SimpleNamespace(tmdb_id=578, title="Jaws", year=1975, tmdb_title=None)
    with app.app_context():
        ok, _ = plex_player.play_movie(
            movie, device_user(admin=False, plex_username="kid")
        )
    assert ok is False
    assert fake.player_gets == []


def test_home_token_is_cached(app, monkeypatch, server_config):
    fake = FakePlex()
    plex_player = _wire(monkeypatch, fake)
    switches = []
    original_post = fake.post

    def counting_post(url, **kwargs):
        if "/switch" in url:
            switches.append(url)
        return original_post(url, **kwargs)

    monkeypatch.setattr(plex_player.requests, "post", counting_post)
    user = device_user(admin=False, plex_username="member")
    with app.app_context():
        first = plex_player.player_token(user)
        second = plex_player.player_token(user)
    assert first == second == "HOME-TOKEN-MEMBER-UUID"
    assert len(switches) == 1


def test_admin_keeps_the_owner_token(app, monkeypatch, server_config):
    fake = FakePlex()
    plex_player = _wire(monkeypatch, fake)
    with app.app_context():
        assert plex_player.player_token(device_user(admin=True)) == "token"


@pytest.mark.parametrize(
    "text, expected",
    [
        ("192.168.1.63", "192.168.1.63:32500"),
        ("10.0.0.5:32500", "10.0.0.5:32500"),
        ("100.101.0.9", "100.101.0.9:32500"),
        ("[fd12::1]:32500", "[fd12::1]:32500"),
        ("fe80::1", "[fe80::1]:32500"),
        ("appletv.local:32500", None),
        ("attacker.example.com", None),
        ("8.8.8.8:32500", None),
        ("192.168.1.63:0", None),
        ("192.168.1.63:99999", None),
        ("", None),
    ],
)
def test_player_address_accepts_only_private_literals(text, expected):
    from app.plex_player import player_address

    assert player_address(text) == expected


def test_play_refuses_a_stored_hostname(app, monkeypatch, server_config):
    # A row edited outside the Profile page still can't send the
    # token to a name
    fake = FakePlex(guid_hits=[{"ratingKey": "1"}])
    plex_player = _wire(monkeypatch, fake)
    movie = SimpleNamespace(tmdb_id=578, title="Jaws", year=1975, tmdb_title=None)
    with app.app_context():
        ok, message = plex_player.play_movie(
            movie, device_user(address="attacker.example.com:32500")
        )
    assert ok is False and "private-network" in message
    assert fake.player_gets == []


def test_profile_rejects_a_hostname(app, monkeypatch, user_client, server_config):
    import app.main.account as account

    probes = []
    monkeypatch.setattr(
        account, "probe_player", lambda address: probes.append(address) or None
    )
    r = _profile_post(user_client, "appletv.local")
    assert "private-network" in r.get_data(as_text=True)
    assert probes == []


def test_play_route_refuses_a_post_without_a_csrf_token(
    app, monkeypatch, user_client, server_config, member_device
):
    """A cross-site form post (a remembered user on a browser that
    doesn't default cookies to Lax) never reaches the player."""

    fake = FakePlex(guid_hits=[{"ratingKey": "189344"}])
    _wire(monkeypatch, fake)
    movie_id = _committed_movie(app)

    r = user_client.post(
        f"/movie/{movie_id}/play", headers={"X-Requested-With": "play"}
    )
    assert r.status_code == 400
    assert json.loads(r.data)["ok"] is False
    assert fake.player_gets == [] and fake.queue_posts == []

    # A plain form post is sent back to the movie page with a flash
    r = user_client.post(f"/movie/{movie_id}/play", data={"player": "plex"})
    assert r.status_code == 302 and f"/movie/{movie_id}" in r.headers["Location"]
    assert fake.player_gets == []
    # ...and the token itself never ages out of a long-open page
    assert app.config["WTF_CSRF_TIME_LIMIT"] is None


def test_remember_cookie_is_samesite_lax(app, client):
    """Flask-Login's remember cookie defaults to no SameSite; the
    session cookie is Lax, and the remember cookie must match or a
    cross-site POST re-authenticates a remembered user from it."""

    from tests.conftest import MEMBER_EMAIL, MEMBER_PASSWORD

    assert app.config["REMEMBER_COOKIE_SAMESITE"] == "Lax"
    page = client.get("/auth/login").get_data(as_text=True)
    token = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page).group(1)
    r = client.post(
        "/auth/login",
        data={
            "csrf_token": token,
            "email": MEMBER_EMAIL,
            "password": MEMBER_PASSWORD,
            "remember_me": "y",
        },
    )
    assert r.status_code == 302
    remember = [
        header
        for header in r.headers.getlist("Set-Cookie")
        if header.startswith("remember_token=")
    ]
    assert remember and "SameSite=Lax" in remember[0]


def _home_users_calls(monkeypatch, fake):
    """Count plex.tv Home-user list reads on top of the fake."""

    import app.plex_player as plex_player

    calls = []
    original = fake.player_get

    def counting_get(url, **kwargs):
        if url.endswith("/home/users"):
            calls.append(url)
        return original(url, **kwargs)

    monkeypatch.setattr(plex_player.requests, "get", counting_get)
    return calls


def test_owner_home_user_is_never_switched_to(app, monkeypatch, server_config):
    """A member who claims the owner's Plex name gets nothing: the Home
    user flagged admin is refused at the point tokens are minted."""

    fake = FakePlex(guid_hits=[{"ratingKey": "1"}])
    plex_player = _wire(monkeypatch, fake)
    movie = SimpleNamespace(tmdb_id=578, title="Jaws", year=1975, tmdb_title=None)
    with app.app_context():
        ok, message = plex_player.play_movie(
            movie, device_user(admin=False, plex_username="OWNER")
        )
    assert ok is False and "Plex Home" in message
    assert fake.player_gets == [] and fake.queue_posts == []


def test_blank_username_matches_no_managed_user(app, monkeypatch, server_config):
    fake = FakePlex()
    plex_player = _wire(monkeypatch, fake)
    calls = _home_users_calls(monkeypatch, fake)
    with app.app_context():
        assert (
            plex_player.player_token(device_user(admin=False, plex_username=" "))
            is None
        )
    assert calls == []


def test_home_user_miss_is_remembered_briefly(app, monkeypatch, server_config):
    fake = FakePlex()
    plex_player = _wire(monkeypatch, fake)
    calls = _home_users_calls(monkeypatch, fake)
    user = device_user(admin=False, plex_username="nobody")
    with app.app_context():
        assert plex_player.player_token(user) is None
        assert plex_player.player_token(user) is None
    assert len(calls) == 1
    assert 0 < app.redis.ttl(plex_player.HOME_TOKEN_KEY.format(user_id=user.id)) <= 300


def _plex_username_post(client, name):
    return client.post(
        "/profile",
        data={
            "csrf_token": page_csrf_token(client),
            "plex_username": name,
            "plex_submit": "Update Plex Mapping",
        },
        follow_redirects=True,
    )


def test_changing_the_plex_username_forgets_the_cached_home_token(app, user_client):
    from app.models import User
    from app.plex_player import HOME_TOKEN_KEY
    from tests.conftest import MEMBER_EMAIL

    with app.app_context():
        user_id = User.query.filter_by(email=MEMBER_EMAIL).one().id
    key = HOME_TOKEN_KEY.format(user_id=user_id)
    app.redis.set(key, "HOME-TOKEN-OLD")
    try:
        r = _plex_username_post(user_client, "member")
        assert "now count as yours" in r.get_data(as_text=True)
        assert app.redis.get(key) is None
    finally:
        _plex_username_post(user_client, "")


def test_plex_username_uniqueness_ignores_case(app, user_client):
    from app import db
    from app.models import User
    from tests.conftest import ADMIN_EMAIL, MEMBER_EMAIL

    with app.app_context():
        User.query.filter_by(email=ADMIN_EMAIL).one().plex_username = "Owner"
        db.session.commit()
    try:
        r = _plex_username_post(user_client, "owner")
        assert "already mapped" in r.get_data(as_text=True)
        with app.app_context():
            assert User.query.filter_by(email=MEMBER_EMAIL).one().plex_username is None
    finally:
        with app.app_context():
            User.query.filter_by(email=ADMIN_EMAIL).one().plex_username = None
            db.session.commit()
