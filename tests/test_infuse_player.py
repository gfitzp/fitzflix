"""Infuse playback on each user's Apple TV (#192): the Companion deep
link, the Infuse-only-format recommendation, the play route's app
dispatch (explicit choice, per-user default, fallbacks, the ride-along
recommendation note), the movie page's dual buttons, and the Profile
page's queue-backed PIN pairing with its Redis hand-off."""

import asyncio
import json
import re

from types import SimpleNamespace

import pytest

from tests.factories import make_movie, make_movie_file

SERVER_CONFIG = {
    "PLEX_URL": "http://plex.test",
    "PLEX_TOKEN": "token",
    "PLEX_PLAYER_SERVER_URI": "https://plex-direct.test:32400",
}

CREDENTIALS = "aa:bb:cc:dd"


def infuse_user(address="192.168.1.247:49153", credentials=CREDENTIALS):
    return SimpleNamespace(
        infuse_player_address=address,
        infuse_player_credentials=credentials,
        infuse_player_configured=bool(address and credentials),
    )


class FakeAppleTV:
    def __init__(self, log):
        self.log = log
        self.apps = SimpleNamespace(launch_app=self._launch)

    async def _launch(self, url):
        self.log.append(url)

    def close(self):
        self.log.append("closed")


def _wire_connect(monkeypatch, log, error=None):
    import app.infuse_player as infuse_player

    async def connect(device, loop):
        if error is not None:
            raise error
        from pyatv.const import Protocol

        service = device.get_service(Protocol.Companion)
        log.append(("connect", str(device.address), service.port, service.credentials))
        return FakeAppleTV(log)

    monkeypatch.setattr(infuse_player.pyatv, "connect", connect)
    return infuse_player


# --- The deep-link launch ---


def test_play_launches_the_tmdb_deep_link(app, monkeypatch):
    log = []
    infuse_player = _wire_connect(monkeypatch, log)

    with app.app_context():
        movie = make_movie("Jaws", 1975, tmdb_id=578)
        ok, message = infuse_player.play_movie(movie, infuse_user())

    assert ok is True
    connect, launch, closed = log
    # The stored credentials ride the manual Companion service, and the
    # link is the TMDB-keyed form — never the raw-URL play that would
    # bypass Plex and lose the diary entry
    assert connect == ("connect", "192.168.1.247", 49153, CREDENTIALS)
    assert launch == "infuse://movie/578?play"
    assert closed == "closed"


def test_play_without_pairing_points_at_the_profile(app):
    import app.infuse_player as infuse_player

    with app.app_context():
        movie = make_movie("Jaws", 1975, tmdb_id=578)
        ok, message = infuse_player.play_movie(movie, infuse_user(credentials=None))

    assert ok is False
    assert "Profile" in message


def test_play_without_a_tmdb_id_is_refused(app, monkeypatch):
    log = []
    infuse_player = _wire_connect(monkeypatch, log)

    with app.app_context():
        movie = make_movie("Home Movie", 2001, tmdb_id=None)
        ok, message = infuse_player.play_movie(movie, infuse_user())

    assert ok is False
    assert "TMDB" in message
    assert log == []


def test_rejected_credentials_ask_for_a_re_pair(app, monkeypatch):
    from pyatv import exceptions

    infuse_player = _wire_connect(
        monkeypatch, [], error=exceptions.AuthenticationError("bad")
    )

    with app.app_context():
        movie = make_movie("Jaws", 1975, tmdb_id=578)
        ok, message = infuse_player.play_movie(movie, infuse_user())

    assert ok is False
    assert "re-pair" in message


def test_unreachable_apple_tv_reports_kindly(app, monkeypatch):
    infuse_player = _wire_connect(monkeypatch, [], error=OSError("no route"))

    with app.app_context():
        movie = make_movie("Jaws", 1975, tmdb_id=578)
        ok, message = infuse_player.play_movie(movie, infuse_user())

    assert ok is False
    assert "Apple TV" in message


# --- The Infuse-only-format recommendation ---


def _atmos_track(file):
    from app import db
    from app.models import FileAudioTrack

    db.session.add(
        FileAudioTrack(
            file_id=file.id,
            track=1,
            language="eng",
            language_name="English",
            format="E-AC-3 JOC",
            codec="Dolby Digital Plus with Dolby Atmos",
            channels="16",
            default=True,
            streamorder=1,
        )
    )
    db.session.flush()


def test_infuse_only_formats_flags_dv8_and_ddp_atmos(app):
    from app.infuse_player import infuse_only_formats

    with app.app_context():
        movie = make_movie("Jaws", 1975, tmdb_id=578)
        file = make_movie_file(movie, "Bluray-2160p Remux", dolby_vision_profile="8.1")
        _atmos_track(file)

        assert infuse_only_formats([file]) == [
            "Dolby Vision Profile 8",
            "E-AC-3 Atmos",
        ]


def test_other_formats_recommend_nothing(app):
    from app.infuse_player import infuse_only_formats

    with app.app_context():
        movie = make_movie("Jaws", 1975, tmdb_id=578)
        # Profile 5 isn't profile 8, and TrueHD Atmos isn't the DD+ twin
        file = make_movie_file(movie, "Bluray-2160p Remux", dolby_vision_profile="5")
        from app import db
        from app.models import FileAudioTrack

        db.session.add(
            FileAudioTrack(
                file_id=file.id,
                track=1,
                language="eng",
                language_name="English",
                format="MLP FBA 16-ch",
                codec="Dolby TrueHD with Dolby Atmos",
                channels="8",
                default=True,
                streamorder=1,
            )
        )
        db.session.flush()

        assert infuse_only_formats([file]) == []


# --- The play route's dispatch ---


@pytest.fixture
def server_config(app, monkeypatch):
    for key, value in SERVER_CONFIG.items():
        monkeypatch.setitem(app.config, key, value)


@pytest.fixture
def member_players(app):
    """Give the member user both apps for one test; the user table
    survives clean_state, so this must restore."""

    from app import db
    from app.models import User
    from tests.conftest import MEMBER_EMAIL

    with app.app_context():
        user = User.query.filter_by(email=MEMBER_EMAIL).one()
        user.plex_player_address = "192.168.1.63:32500"
        user.plex_player_id = "MEMBER-ATV-ID"
        user.infuse_player_address = "192.168.1.63:49153"
        user.infuse_player_credentials = CREDENTIALS
        user.default_player = None
        db.session.commit()
    yield
    with app.app_context():
        user = User.query.filter_by(email=MEMBER_EMAIL).one()
        user.plex_player_address = None
        user.plex_player_id = None
        user.infuse_player_address = None
        user.infuse_player_credentials = None
        user.default_player = None
        db.session.commit()


def _set_default(app, choice):
    from app import db
    from app.models import User
    from tests.conftest import MEMBER_EMAIL

    with app.app_context():
        user = User.query.filter_by(email=MEMBER_EMAIL).one()
        user.default_player = choice
        db.session.commit()


def _wire_route(monkeypatch, plex_result=(True, "Playing on your device.")):
    """Fake both play paths at the route's own namespace, recording
    which app the dispatch picked."""

    import app.main.library as library

    calls = []

    def fake_plex(movie, user):
        calls.append("plex")
        return plex_result

    def fake_infuse(movie, user):
        calls.append("infuse")
        return True, "Opened in Infuse."

    monkeypatch.setattr(library, "play_movie", fake_plex)
    monkeypatch.setattr(library, "infuse_play_movie", fake_infuse)
    return calls


def _committed_movie(app, **kwargs):
    from app import db

    kwargs.setdefault("tmdb_id", 578)
    with app.app_context():
        movie = make_movie("Jaws", 1975, **kwargs)
        movie_id = movie.id
        db.session.commit()
    return movie_id


def _play(client, movie_id, data=None):
    r = client.post(
        f"/movie/{movie_id}/play", data=data, headers={"X-Requested-With": "play"}
    )
    return r.status_code, json.loads(r.data)


def test_plain_post_uses_the_plex_default(
    app, monkeypatch, user_client, server_config, member_players
):
    calls = _wire_route(monkeypatch)
    movie_id = _committed_movie(app)

    status, state = _play(user_client, movie_id)
    assert status == 200 and state["ok"] is True
    assert calls == ["plex"]


def test_plain_post_follows_an_infuse_default(
    app, monkeypatch, user_client, server_config, member_players
):
    calls = _wire_route(monkeypatch)
    _set_default(app, "infuse")
    movie_id = _committed_movie(app)

    status, state = _play(user_client, movie_id)
    assert status == 200
    assert calls == ["infuse"]


def test_explicit_player_field_wins_over_the_default(
    app, monkeypatch, user_client, server_config, member_players
):
    calls = _wire_route(monkeypatch)
    movie_id = _committed_movie(app)

    _play(user_client, movie_id, data={"player": "infuse"})
    assert calls == ["infuse"]


def test_infuse_default_falls_back_to_plex_without_a_tmdb_id(
    app, monkeypatch, user_client, server_config, member_players
):
    calls = _wire_route(monkeypatch)
    _set_default(app, "infuse")
    movie_id = _committed_movie(app, tmdb_id=None)

    _play(user_client, movie_id)
    assert calls == ["plex"]


def test_plex_default_falls_back_to_infuse_when_server_unconfigured(
    app, monkeypatch, user_client, member_players
):
    calls = _wire_route(monkeypatch)
    movie_id = _committed_movie(app)

    _play(user_client, movie_id)
    assert calls == ["infuse"]


def test_plex_play_of_an_infuse_only_film_carries_the_recommendation(
    app, monkeypatch, user_client, server_config, member_players
):
    calls = _wire_route(monkeypatch)
    from app import db

    with app.app_context():
        movie = make_movie("Jaws", 1975, tmdb_id=578)
        file = make_movie_file(movie, "Bluray-2160p Remux", dolby_vision_profile="8.1")
        _atmos_track(file)
        movie_id = movie.id
        db.session.commit()

    status, state = _play(user_client, movie_id)
    assert calls == ["plex"]
    assert state["ok"] is True
    assert "only" in state["message"] and "Infuse" in state["message"]
    assert "Dolby Vision Profile 8" in state["message"]


def test_no_recommendation_note_without_infuse_only_formats(
    app, monkeypatch, user_client, server_config, member_players
):
    _wire_route(monkeypatch)
    from app import db

    with app.app_context():
        movie = make_movie("Jaws", 1975, tmdb_id=578)
        make_movie_file(movie, "Bluray-1080p")
        movie_id = movie.id
        db.session.commit()

    status, state = _play(user_client, movie_id)
    assert state["message"] == "Playing on your device."


# --- The movie page's buttons ---


def test_movie_page_offers_both_apps_and_the_recommendation(
    app, user_client, server_config, member_players
):
    from app import db

    with app.app_context():
        movie = make_movie("Jaws", 1975, tmdb_id=578)
        file = make_movie_file(movie, "Bluray-2160p Remux", dolby_vision_profile="8.1")
        _atmos_track(file)
        movie_id = movie.id
        db.session.commit()

    page = user_client.get(f"/movie/{movie_id}").get_data(as_text=True)
    assert "Play (Plex)" in page
    assert "Play (Infuse)" in page
    assert 'name="player" value="infuse"' in page
    assert "Recommended" in page
    # Default-first ordering: Plex leads while no default says otherwise
    assert page.index("Play (Plex)") < page.index("Play (Infuse)")


def test_movie_page_with_only_infuse_shows_a_single_button(
    app, user_client, member_players
):
    """No Plex server config: only the Infuse button renders, under its
    single-app label."""

    from app import db

    with app.app_context():
        movie = make_movie("Jaws", 1975, tmdb_id=578)
        make_movie_file(movie, "Bluray-1080p")
        movie_id = movie.id
        db.session.commit()

    page = user_client.get(f"/movie/{movie_id}").get_data(as_text=True)
    assert "Play in Infuse" in page
    assert "Play (Plex)" not in page


def test_popover_card_appears_with_only_infuse_configured(
    app, user_client, member_players
):
    from app import db
    from tests.factories import make_movie_file as make_file_

    with app.app_context():
        movie = make_movie("Jaws", 1975, tmdb_id=578)
        make_file_(movie, "Bluray-1080p")
        movie_id = movie.id
        db.session.commit()

    card = user_client.get(f"/movie_card?movie_id={movie_id}").get_data(as_text=True)
    assert f'action="/movie/{movie_id}/play"' in card


# --- Pairing: the queue task's Redis hand-off ---


class FakePairing:
    def __init__(self, log, accept=True):
        self.log = log
        self.accept = accept
        self.has_paired = False
        self.service = SimpleNamespace(credentials=None)

    async def begin(self):
        self.log.append("begin")

    def pin(self, pin):
        self.log.append(("pin", pin))

    async def finish(self):
        if self.accept:
            self.has_paired = True
            self.service.credentials = CREDENTIALS

    async def close(self):
        self.log.append("close")


def _run_pair(app, monkeypatch, user_id, accept=True):
    import app.infuse_player as infuse_player

    log = []

    async def fake_pair(device, protocol, loop):
        from pyatv.const import Protocol

        log.append(
            ("pair", str(device.address), device.get_service(Protocol.Companion).port)
        )
        return FakePairing(log, accept=accept)

    monkeypatch.setattr(infuse_player.pyatv, "pair", fake_pair)
    with app.app_context():
        asyncio.run(infuse_player._pair(app, user_id, "192.168.1.63:49153"))
    return log


def _member_id(app):
    from app.models import User
    from tests.conftest import MEMBER_EMAIL

    with app.app_context():
        return User.query.filter_by(email=MEMBER_EMAIL).one().id


def test_pair_task_trades_the_pin_for_stored_credentials(app, monkeypatch):
    user_id = _member_id(app)
    # The web form's PIN is already waiting in Redis when the task polls
    app.redis.set(f"fitzflix:infuse-pair:{user_id}:pin", "1234", ex=300)

    log = _run_pair(app, monkeypatch, user_id)

    assert ("pin", 1234) in log
    assert app.redis.get(f"fitzflix:infuse-pair:{user_id}:state") == b"ok"

    from app import db
    from app.models import User

    with app.app_context():
        user = db.session.get(User, user_id)
        assert user.infuse_player_address == "192.168.1.63:49153"
        assert user.infuse_player_credentials == CREDENTIALS
        user.infuse_player_address = None
        user.infuse_player_credentials = None
        db.session.commit()


def test_pair_task_reports_a_refused_pin(app, monkeypatch):
    user_id = _member_id(app)
    app.redis.set(f"fitzflix:infuse-pair:{user_id}:pin", "9999", ex=300)

    _run_pair(app, monkeypatch, user_id, accept=False)

    state = app.redis.get(f"fitzflix:infuse-pair:{user_id}:state").decode()
    assert state.startswith("error:")

    from app import db
    from app.models import User

    with app.app_context():
        assert db.session.get(User, user_id).infuse_player_credentials is None


def test_pairing_outcome_reads_the_task_verdict(app):
    from app.infuse_player import pairing_outcome

    with app.test_request_context():
        app.redis.set("fitzflix:infuse-pair:7:state", "ok", ex=300)
        assert pairing_outcome(7, wait_seconds=1) == (
            True,
            "Paired — your play buttons can now open films in Infuse.",
        )

        app.redis.set("fitzflix:infuse-pair:7:state", "error:The Apple TV refused")
        ok, message = pairing_outcome(7, wait_seconds=1)
        assert ok is False and message == "The Apple TV refused"

        app.redis.delete("fitzflix:infuse-pair:7:state")
        ok, message = pairing_outcome(7, wait_seconds=1)
        assert ok is False and "timed out" in message


# --- Pairing: the Profile page flow ---


def _profile_post(client, data):
    page = client.get("/profile").get_data(as_text=True)
    match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page)
    assert match, "no csrf token found on the profile page"
    return client.post(
        "/profile",
        data={"csrf_token": match.group(1), **data},
        follow_redirects=True,
    )


def test_profile_address_submit_starts_a_pairing(app, monkeypatch, user_client):
    import app.main.account as account

    started = []
    monkeypatch.setattr(
        account, "start_pairing", lambda user_id, address: started.append(address)
    )

    r = _profile_post(
        user_client,
        {"infuse_player_address": "192.168.1.63:49153", "infuse_player_submit": "1"},
    )
    assert started == ["192.168.1.63:49153"]
    assert "PIN" in r.get_data(as_text=True)


def test_profile_bare_address_gets_the_default_companion_port(
    app, monkeypatch, user_client
):
    import app.main.account as account

    started = []
    monkeypatch.setattr(
        account, "start_pairing", lambda user_id, address: started.append(address)
    )

    _profile_post(
        user_client,
        {"infuse_player_address": "192.168.1.63", "infuse_player_submit": "1"},
    )
    assert started == ["192.168.1.63:49152"]


def test_profile_shows_the_pin_form_while_pairing_waits(app, user_client):
    user_id = _member_id(app)
    app.redis.set(f"fitzflix:infuse-pair:{user_id}:state", "show-pin", ex=300)

    page = user_client.get("/profile").get_data(as_text=True)
    assert "infuse_pin" in page


def test_profile_pin_submit_reports_the_outcome(app, monkeypatch, user_client):
    import app.main.account as account

    handed_over = []
    monkeypatch.setattr(
        account, "submit_pin", lambda user_id, pin: handed_over.append(pin)
    )
    monkeypatch.setattr(
        account, "pairing_outcome", lambda user_id: (True, "Paired — done.")
    )
    user_id = _member_id(app)
    app.redis.set(f"fitzflix:infuse-pair:{user_id}:state", "show-pin", ex=300)

    r = _profile_post(user_client, {"infuse_pin": "1234", "infuse_pin_submit": "1"})
    assert handed_over == ["1234"]
    assert "Paired" in r.get_data(as_text=True)


def test_profile_blank_address_clears_the_device(app, user_client, member_players):
    _profile_post(
        user_client, {"infuse_player_address": "", "infuse_player_submit": "1"}
    )

    from app.models import User
    from tests.conftest import MEMBER_EMAIL

    with app.app_context():
        user = User.query.filter_by(email=MEMBER_EMAIL).one()
        assert user.infuse_player_address is None
        assert user.infuse_player_credentials is None


def test_profile_default_player_choice_sticks(app, user_client, member_players):
    r = _profile_post(
        user_client, {"default_player": "infuse", "default_player_submit": "1"}
    )
    assert "default to Infuse" in r.get_data(as_text=True)

    from app.models import User
    from tests.conftest import MEMBER_EMAIL

    with app.app_context():
        user = User.query.filter_by(email=MEMBER_EMAIL).one()
        assert user.default_player == "infuse"
        assert user.preferred_player == "infuse"
