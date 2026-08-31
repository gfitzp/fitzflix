"""Virtual DVR channels (#182): the token gate on all three endpoints,
the nightly lineup build (genre channels, duration cache), the cyclic
schedule math, guide/playlist agreement, and the stream generator's
program-to-program roll."""

import io
import json
import xml.etree.ElementTree as ElementTree

from datetime import date

import pytest

from app.models import TMDBGenre, TMDBKeyword, TMDBNetwork, db
from tests.factories import make_movie, make_movie_file, make_tv_file, make_tv_series

TOKEN = "dvr-test-token"

ENDPOINTS = [
    "/dvr/{token}/playlist.m3u",
    "/dvr/{token}/guide.xml",
    "/dvr/{token}/stream/fitzflix-mix.ts",
    "/dvr/{token}/discover.json",
    "/dvr/{token}/lineup_status.json",
    "/dvr/{token}/lineup.json",
]


def _build_library(app, monkeypatch, horror_films=0, other_films=2, duration=3600.0):
    """Seed owned films (optionally enough Horror for a genre channel),
    stub the ffprobe duration, and run the lineup build."""

    from app import dvr

    monkeypatch.setattr(dvr, "_probe_duration", lambda path: duration)
    with app.app_context():
        genre = TMDBGenre(id=27, name="Horror")
        db.session.add(genre)
        for n in range(horror_films):
            movie = make_movie(f"Scary {n}", 1970 + n)
            movie.genres.append(genre)
            make_movie_file(movie, "Bluray-1080p")
        for n in range(other_films):
            movie = make_movie(f"Drama {n}", 1990 + n)
            make_movie_file(movie, "Bluray-1080p")
        db.session.commit()
        assert dvr.build_channel_lineups() is True


@pytest.mark.parametrize("endpoint", ENDPOINTS)
class TestDvrGate:
    """Every endpoint 404s on a wrong token or while the feature is
    unconfigured — indistinguishable from a missing route."""

    def test_rejects_wrong_token(self, client, endpoint):
        assert client.get(endpoint.format(token="0" * 24)).status_code == 404

    def test_rejects_when_unconfigured(self, app, client, monkeypatch, endpoint):
        monkeypatch.setitem(app.config, "DVR_TOKEN", None)
        assert client.get(endpoint.format(token=TOKEN)).status_code == 404


def test_build_makes_mix_channel_and_caches_durations(app, monkeypatch):
    from app import dvr

    _build_library(app, monkeypatch, other_films=3)

    channels = dvr.channel_index(app.redis)
    assert [channel["slug"] for channel in channels] == ["fitzflix-mix"]
    assert channels[0]["number"] == 100

    lineup = dvr.channel_lineup(app.redis, "fitzflix-mix")
    assert len(lineup["programs"]) == 3
    program = lineup["programs"][0]
    assert program["duration"] == 3600.0
    assert program["file_path"].endswith(".mkv")
    assert app.redis.hlen(dvr.DURATIONS_KEY) == 3

    # A rebuild reads every duration from the cache, never the prober

    def boom(path):
        raise AssertionError("cached duration was re-probed")

    monkeypatch.setattr(dvr, "_probe_duration", boom)
    with app.app_context():
        assert dvr.build_channel_lineups() is True


def test_build_makes_genre_channels_from_deep_genres(app, monkeypatch):
    from app import dvr

    _build_library(app, monkeypatch, horror_films=8, other_films=2)

    channels = {channel["slug"]: channel for channel in dvr.channel_index(app.redis)}
    assert set(channels) == {"fitzflix-mix", "horror"}
    assert channels["horror"]["number"] == 101

    horror = dvr.channel_lineup(app.redis, "horror")
    assert len(horror["programs"]) == 8
    assert all("Horror" in program["genres"] for program in horror["programs"])
    assert len(dvr.channel_lineup(app.redis, "fitzflix-mix")["programs"]) == 10


def test_build_makes_criterion_channel_from_availability(app, monkeypatch):
    """Owned films flat-rate on provider 258 get the Criterion channel;
    below the special-channel bench, no channel appears."""

    from app import dvr

    monkeypatch.setattr(dvr, "_probe_duration", lambda path: 3600.0)
    streaming = {
        "link": None,
        "flatrate": [
            {
                "provider_id": 258,
                "provider_name": "The Criterion Channel",
                "logo_path": None,
            }
        ],
        "ads": [],
        "rent": [],
        "buy": [],
    }
    nothing = {"link": None, "flatrate": [], "ads": [], "rent": [], "buy": []}

    def fake_availability(tmdb_ids, **kwargs):
        return {t: (streaming if t < 9500 else nothing) for t in tmdb_ids}, []

    monkeypatch.setattr(dvr, "batch_title_availability", fake_availability)
    with app.app_context():
        for n in range(3):
            make_movie_file(
                make_movie(f"Criterion {n}", 1950 + n, tmdb_id=9000 + n),
                "Bluray-1080p",
            )
        make_movie_file(make_movie("Other", 1990, tmdb_id=9500), "Bluray-1080p")
        db.session.commit()
        assert dvr.build_channel_lineups() is True

    channels = {c["slug"]: c for c in dvr.channel_index(app.redis)}
    assert channels["criterion"]["number"] == 140
    lineup = dvr.channel_lineup(app.redis, "criterion")
    assert {p["title"] for p in lineup["programs"]} == {
        "Criterion 0",
        "Criterion 1",
        "Criterion 2",
    }

    # One film drops off Criterion: only two remain, below the bench

    def fake_thinner(tmdb_ids, **kwargs):
        return {t: (streaming if t < 9002 else nothing) for t in tmdb_ids}, []

    monkeypatch.setattr(dvr, "batch_title_availability", fake_thinner)
    with app.app_context():
        assert dvr.build_channel_lineups() is True
    assert "criterion" not in {c["slug"] for c in dvr.channel_index(app.redis)}


def test_build_makes_leaving_channel_with_last_call_note(app, monkeypatch):
    """Owned films in the leaving set air as a last-call marathon, the
    departure date leading every guide description."""

    from app import dvr

    monkeypatch.setattr(dvr, "_probe_duration", lambda path: 3600.0)
    monkeypatch.setattr(
        dvr, "_leaving_set", lambda: ({7000, 7001, 7002}, date(2026, 9, 30))
    )
    with app.app_context():
        for n in range(3):
            make_movie_file(
                make_movie(f"Departing {n}", 1960 + n, tmdb_id=7000 + n),
                "Bluray-1080p",
            )
        make_movie_file(make_movie("Staying", 1990, tmdb_id=7900), "Bluray-1080p")
        db.session.commit()
        assert dvr.build_channel_lineups() is True

    channels = {c["slug"]: c for c in dvr.channel_index(app.redis)}
    assert channels["leaving-soon"]["number"] == 141
    lineup = dvr.channel_lineup(app.redis, "leaving-soon")
    assert len(lineup["programs"]) == 3
    assert all(
        p["overview"].startswith("Leaving the Criterion Channel September 30.")
        for p in lineup["programs"]
    )

    # The mix channel's copies of the same films carry no note

    mix = dvr.channel_lineup(app.redis, "fitzflix-mix")
    assert not any(p["overview"].startswith("Leaving") for p in mix["programs"])


def test_build_makes_genre_tv_channels_with_interleaved_blocks(
    app, client, monkeypatch
):
    """A deep TV genre gets a channel airing its series in short
    interleaved blocks: each series in cyclic broadcast order rotated
    by the day, specials excluded, sub-title/episode-num in the
    guide."""

    from app import dvr

    monkeypatch.setattr(dvr, "_probe_duration", lambda path: 1500.0)
    day = date(2026, 8, 31)
    with app.app_context():
        animation = TMDBGenre(id=16, name="Animation")
        db.session.add(animation)
        first = make_tv_series("Alpha Toons", tmdb_overview="Toons all day.")
        second = make_tv_series("Beta Toons")
        for series in (first, second):
            series.genres.append(animation)
            for episode in range(1, 10):
                make_tv_file(series, 1, episode, "Bluray-1080p")
        make_tv_file(first, 0, 1, "Bluray-1080p")  # a special: never airs
        db.session.commit()
        assert dvr.build_channel_lineups(day=day) is True

    channels = {c["slug"]: c for c in dvr.channel_index(app.redis)}
    assert channels["tv-animation"]["number"] == 200

    programs = dvr.channel_lineup(app.redis, "tv-animation")["programs"]
    assert len(programs) == 18
    assert all(p["episode_num"].startswith("S01") for p in programs)

    # Blocks of two, alternating series, both fully represented

    assert [p["title"] for p in programs[:4]] == [
        "Alpha Toons",
        "Alpha Toons",
        "Beta Toons",
        "Beta Toons",
    ]

    # Each series airs in cyclic broadcast order from its day-rotated
    # start (equal depth: quota is the full series)

    start = (day.toordinal() * 9) % 9
    expected = [f"S01E{((start + n) % 9) + 1:02d}" for n in range(9)]
    for title in ("Alpha Toons", "Beta Toons"):
        aired = [p["episode_num"] for p in programs if p["title"] == title]
        assert aired == expected

    guide = client.get(f"/dvr/{TOKEN}/guide.xml")
    tv = ElementTree.fromstring(guide.get_data(as_text=True))
    airing = next(
        p for p in tv.findall("programme") if p.get("channel") == "tv-animation"
    )
    assert airing.find("episode-num").get("system") == "onscreen"
    assert airing.find("episode-num").text.startswith("S01E")


def test_build_makes_themed_tv_channels(app, monkeypatch):
    """Theme specs: British Sitcoms needs the sitcom keyword AND a
    GB-registered network; Game Shows matches the keyword or a title
    pin (Match Game PM carries no keywords at all)."""

    from app import dvr

    monkeypatch.setattr(dvr, "_probe_duration", lambda path: 1500.0)
    with app.app_context():
        sitcom = TMDBKeyword(id=1, name="Sitcom")
        game_show = TMDBKeyword(id=2, name="game show")
        bbc = TMDBNetwork(id=1, name="BBC One", origin_country="GB")
        cbs = TMDBNetwork(id=2, name="CBS", origin_country="US")
        db.session.add_all([sitcom, game_show, bbc, cbs])

        brit = make_tv_series("Fawlty Towers")
        brit.keywords.append(sitcom)
        brit.networks.append(bbc)
        yank = make_tv_series("Cheers Clone")
        yank.keywords.append(sitcom)
        yank.networks.append(cbs)
        host = make_tv_series("The Match Game")
        host.keywords.append(game_show)
        pinned = make_tv_series("Match Game PM")  # no keywords: title pin
        for series in (brit, yank, host, pinned):
            for episode in range(1, 10):
                make_tv_file(series, 1, episode, "Bluray-1080p")
        db.session.commit()
        assert dvr.build_channel_lineups() is True

    channels = {c["slug"]: c for c in dvr.channel_index(app.redis)}
    assert channels["tv-game-shows"]["number"] == 240
    assert channels["tv-british-sitcoms"]["number"] == 241

    games = dvr.channel_lineup(app.redis, "tv-game-shows")["programs"]
    assert {p["title"] for p in games} == {"The Match Game", "Match Game PM"}

    brits = dvr.channel_lineup(app.redis, "tv-british-sitcoms")["programs"]
    assert {p["title"] for p in brits} == {"Fawlty Towers"}


def test_schedule_math_wraps_the_lineup(app):
    from app import dvr

    lineup = {
        "epoch": 1000.0,
        "programs": [{"duration": 100.0}, {"duration": 50.0}],
    }
    assert dvr.program_at(lineup, 1000.0) == (0, 0.0)
    assert dvr.program_at(lineup, 1120.0) == (1, 20.0)
    assert dvr.program_at(lineup, 1170.0) == (0, 20.0)
    # Joining before the epoch still lands inside the cycle
    index, offset = dvr.program_at(lineup, 940.0)
    assert (index, offset) == (0, 90.0)

    airings = list(dvr.programs_between(lineup, 1120.0, 1300.0))
    assert [(a, b) for a, b, _ in airings] == [
        (1100.0, 1150.0),
        (1150.0, 1250.0),
        (1250.0, 1300.0),
    ]
    # Contiguous: each airing stops exactly where the next starts
    assert all(airings[n][1] == airings[n + 1][0] for n in range(len(airings) - 1))


def test_hdhomerun_trio_describes_the_tuner(app, client, monkeypatch):
    """Plex's manual tuner entry probes discover.json (also via the
    pasted-playlist-URL alias), then reads the lineup; the three
    documents must agree with the stored channels."""

    _build_library(app, monkeypatch, horror_films=8, other_films=2)

    # SERVER_NAME pins url_for(_external=True) to the public hostname in
    # production; these documents must ignore it and answer on the host
    # the request came in on, or Plex tunes through CloudFront

    monkeypatch.setitem(app.config, "SERVER_NAME", "public.example.com")

    discover = client.get(f"/dvr/{TOKEN}/discover.json", base_url="http://localhost")
    assert discover.status_code == 200
    device = discover.get_json()
    assert device["BaseURL"].endswith(f"/dvr/{TOKEN}")
    assert "public.example.com" not in device["BaseURL"]
    assert device["BaseURL"].startswith("http://localhost")
    assert device["LineupURL"] == f'{device["BaseURL"]}/lineup.json'
    assert device["TunerCount"] == 4

    # The alias answers identically when the playlist URL was pasted
    # as the device address

    alias = client.get(
        f"/dvr/{TOKEN}/playlist.m3u/discover.json", base_url="http://localhost"
    )
    assert alias.get_json()["BaseURL"] == device["BaseURL"]

    status = client.get(f"/dvr/{TOKEN}/lineup_status.json").get_json()
    assert status["ScanInProgress"] == 0

    lineup = client.get(f"/dvr/{TOKEN}/lineup.json").get_json()
    assert [entry["GuideNumber"] for entry in lineup] == ["100", "101"]
    assert lineup[0]["GuideName"] == "Fitzflix Mix"
    assert lineup[1]["URL"].endswith(f"/dvr/{TOKEN}/stream/horror.ts")


def test_playlist_and_guide_agree_on_channel_ids(app, client, monkeypatch):
    _build_library(app, monkeypatch, horror_films=8, other_films=2)

    playlist = client.get(f"/dvr/{TOKEN}/playlist.m3u")
    assert playlist.status_code == 200
    body = playlist.get_data(as_text=True)
    assert body.startswith("#EXTM3U")
    assert 'tvg-id="fitzflix-mix"' in body
    assert 'tvg-id="horror"' in body
    assert f"/dvr/{TOKEN}/stream/horror.ts" in body

    guide = client.get(f"/dvr/{TOKEN}/guide.xml")
    assert guide.status_code == 200
    tv = ElementTree.fromstring(guide.get_data(as_text=True))
    channel_ids = {channel.get("id") for channel in tv.findall("channel")}
    assert channel_ids == {"fitzflix-mix", "horror"}

    airings = [p for p in tv.findall("programme") if p.get("channel") == "horror"]
    assert airings, "guide carries no horror airings"
    assert all(a.find("title").text.startswith("Scary") for a in airings)
    # Contiguous wall: each programme starts when the previous stops
    stamps = [(a.get("start"), a.get("stop")) for a in airings]
    assert all(stamps[n][1] == stamps[n + 1][0] for n in range(len(stamps) - 1))


def test_stream_rolls_programs_and_dies_cleanly(app, client, monkeypatch):
    """The stream serves ffmpeg's bytes, rolls to the next program on
    EOF, and ends (rather than spinning) when ffmpeg can't spawn."""

    from app.main import dvr as dvr_routes

    lineup = {
        "slug": "unit",
        "name": "Unit",
        "number": 100,
        "epoch": 0.0,
        "programs": [
            {
                "title": "Unit Film",
                "file_path": "Movies/Unit/Unit.mkv",
                "duration": 3600.0,
                "audio_channels": 6,
            }
        ],
    }
    app.redis.set("fitzflix:dvr:lineup:unit", json.dumps(lineup))

    class FakeProcess:
        def __init__(self, data):
            self.stdout = io.BytesIO(data)

        def kill(self):
            """No real process to kill."""

        def wait(self):
            """No real process to reap."""
            return 0

    spawned = []

    def fake_popen(command, stdout=None, stderr=None):
        spawned.append(command)
        if len(spawned) == 1:
            return FakeProcess(b"T" * 70000)
        raise OSError("ffmpeg exploded")

    monkeypatch.setattr(dvr_routes.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(dvr_routes.os.path, "isfile", lambda path: True)

    response = client.get(f"/dvr/{TOKEN}/stream/unit.ts")
    assert response.status_code == 200
    assert response.mimetype == "video/mp2t"
    assert response.get_data() == b"T" * 70000
    assert len(spawned) == 2

    # The first spawn joined mid-program (epoch 0 is long past) and
    # the roll after EOF started the next program from the top

    first, second = spawned
    assert "-ss" in first
    assert "-ss" not in second
    assert "h264_videotoolbox" in first

    # An unknown channel is indistinguishable from a missing route

    assert client.get(f"/dvr/{TOKEN}/stream/nope.ts").status_code == 404
