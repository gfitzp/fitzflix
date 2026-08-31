"""Virtual DVR channels (#182): the token gate on all three endpoints,
the nightly lineup build (genre channels, duration cache), the cyclic
schedule math, guide/playlist agreement, and the stream generator's
program-to-program roll."""

import io
import json
import xml.etree.ElementTree as ElementTree

import pytest

from app.models import TMDBGenre, db
from tests.factories import make_movie, make_movie_file

TOKEN = "dvr-test-token"

ENDPOINTS = [
    "/dvr/{token}/playlist.m3u",
    "/dvr/{token}/guide.xml",
    "/dvr/{token}/stream/fitzflix-mix.ts",
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
