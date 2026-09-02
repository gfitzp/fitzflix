"""Test the virtual DVR channels (#182).

These tests cover the token gate on all 3 endpoints and the nightly
lineup build (genre channels, duration cache). They also cover the
cyclic schedule math and the agreement between the guide and the
playlist. The roll of the stream generator from program to program
completes the set."""

import io
import json
import xml.etree.ElementTree as ElementTree

from datetime import date

import pytest

from app.models import TMDBGenre, TMDBKeyword, TMDBNetwork, db
from tests.factories import make_movie, make_movie_file, make_tv_file, make_tv_series

TOKEN = "dvr-test-token"


@pytest.fixture(autouse=True)
def library_present(monkeypatch):
    """Make every row read as on disk and every share as online.

    These tests seed rows, not files. A test can say otherwise."""

    from app import dvr

    monkeypatch.setattr(dvr, "_on_disk", lambda file: True)
    monkeypatch.setattr(dvr, "_library_online", lambda: True)


ENDPOINTS = [
    "/dvr/{token}/playlist.m3u",
    "/dvr/{token}/guide.xml",
    "/dvr/{token}/stream/fitzflix-mix.ts",
    "/dvr/{token}/discover.json",
    "/dvr/{token}/lineup_status.json",
    "/dvr/{token}/lineup.json",
]


def _build_library(app, monkeypatch, horror_films=0, other_films=2, duration=3600.0):
    """Seed owned films, stub the ffprobe duration, and build the lineups.

    The horror_films count can be large enough for a genre channel."""

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
    """Test that every endpoint answers 404 on a wrong token.

    The same occurs while the feature is not configured. The answer is
    the same as for a missing route."""

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

    # A rebuild reads every duration from the cache, never from the prober.

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
    """Test that owned Criterion films make the Criterion channel.

    Owned films with a flat rate on provider 258 get the Criterion
    channel. Below the special-channel minimum, no channel appears."""

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

    # One film drops off Criterion. Only 2 remain, below the minimum.

    def fake_thinner(tmdb_ids, **kwargs):
        return {t: (streaming if t < 9002 else nothing) for t in tmdb_ids}, []

    monkeypatch.setattr(dvr, "batch_title_availability", fake_thinner)
    with app.app_context():
        assert dvr.build_channel_lineups() is True
    assert "criterion" not in {c["slug"] for c in dvr.channel_index(app.redis)}


def test_build_makes_leaving_channel_with_last_call_note(app, monkeypatch):
    """Test that the leaving set makes a last-call channel.

    Owned films in the leaving set air as a last-call marathon. The
    departure date leads every guide description."""

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

    # The copies of the same films on the mix channel carry no note.

    mix = dvr.channel_lineup(app.redis, "fitzflix-mix")
    assert not any(p["overview"].startswith("Leaving") for p in mix["programs"])


def test_build_makes_genre_tv_channels_with_interleaved_blocks(
    app, client, monkeypatch
):
    """Test that a deep TV genre gets a channel with interleaved blocks.

    The channel airs its series in short interleaved blocks. Each series
    airs in cyclic broadcast order, rotated by the day. Specials are
    excluded. The guide shows the sub-title and the episode-num."""

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
        make_tv_file(first, 0, 1, "Bluray-1080p")  # a special never airs
        db.session.commit()
        assert dvr.build_channel_lineups(day=day) is True

    channels = {c["slug"]: c for c in dvr.channel_index(app.redis)}
    assert channels["tv-animation"]["number"] == 200

    programs = dvr.channel_lineup(app.redis, "tv-animation")["programs"]
    assert len(programs) == 18
    assert all(p["episode_num"].startswith("S01") for p in programs)

    # Blocks of 2 alternate between the series. Both series are complete.

    assert [p["title"] for p in programs[:4]] == [
        "Alpha Toons",
        "Alpha Toons",
        "Beta Toons",
        "Beta Toons",
    ]

    # Each series airs in cyclic broadcast order from its day-rotated
    # start. With equal depth, the quota is the full series.

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
    """Test the themed TV channels.

    British Sitcoms needs the sitcom keyword AND a GB-registered network.
    Game Shows matches the keyword or a title pin. Match Game PM carries
    no keywords at all."""

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
        pinned = make_tv_series("Match Game PM")  # no keywords, thus a title pin
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
    # A join before the epoch still goes into the cycle.
    index, offset = dvr.program_at(lineup, 940.0)
    assert (index, offset) == (0, 90.0)

    airings = list(dvr.programs_between(lineup, 1120.0, 1300.0))
    assert [(a, b) for a, b, _ in airings] == [
        (1100.0, 1150.0),
        (1150.0, 1250.0),
        (1250.0, 1300.0),
    ]
    # The airings are contiguous. Each airing stops where the next one starts.
    assert all(airings[n][1] == airings[n + 1][0] for n in range(len(airings) - 1))


def test_hdhomerun_trio_describes_the_tuner(app, client, monkeypatch):
    """Test that the 3 HDHomeRun documents describe the tuner.

    The manual tuner entry of Plex probes discover.json, also through
    the alias for a pasted playlist URL. Then it reads the lineup. The 3
    documents must agree with the stored channels."""

    _build_library(app, monkeypatch, horror_films=8, other_films=2)

    # In production, SERVER_NAME pins url_for(_external=True) to the
    # public hostname. These documents must ignore it. They must answer
    # on the host that the request came in on. If not, Plex tunes
    # through CloudFront.

    monkeypatch.setitem(app.config, "SERVER_NAME", "public.example.com")

    discover = client.get(f"/dvr/{TOKEN}/discover.json", base_url="http://localhost")
    assert discover.status_code == 200
    device = discover.get_json()
    assert device["BaseURL"].endswith(f"/dvr/{TOKEN}")
    assert "public.example.com" not in device["BaseURL"]
    assert device["BaseURL"].startswith("http://localhost")
    assert device["LineupURL"] == f'{device["BaseURL"]}/lineup.json'
    assert device["TunerCount"] == 4

    # The alias gives the same answer when the user pasted the playlist
    # URL as the device address.

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
    # The wall is contiguous. Each programme starts when the previous one stops.
    stamps = [(a.get("start"), a.get("stop")) for a in airings]
    assert all(stamps[n][1] == stamps[n + 1][0] for n in range(len(stamps) - 1))


def test_stream_rolls_programs_and_dies_cleanly(app, client, monkeypatch):
    """Test that the stream rolls programs and ends cleanly.

    The stream serves the bytes of ffmpeg. It rolls to the next program
    on EOF. When ffmpeg cannot spawn, the stream ends. It does not
    spin."""

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
            """Do nothing. There is no real process to kill."""

        def wait(self):
            """Return 0. There is no real process to reap."""
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

    # The first spawn joined in the middle of the program, because epoch
    # 0 is long past. The roll after EOF started the next program from
    # the top.

    first, second = spawned
    assert "-ss" in first
    assert "-ss" not in second
    assert "h264_videotoolbox" in first

    # An unknown channel gets the same answer as a missing route.

    assert client.get(f"/dvr/{TOKEN}/stream/nope.ts").status_code == 404


def test_build_prefers_the_copy_on_disk(app, monkeypatch, tmp_path):
    """Test that the build prefers the copy that is on disk.

    A row can outlive its local file. The WEBDL rebuild leaves WEBRip
    rows beside the renamed WEBDL files. The better-ranked absent row
    yields to the present one. A movie with no copy on disk leaves the
    pool. Fitzflix does not probe it and does not air it. This is true
    even when a duration was cached before its file went."""

    import os

    from app import dvr

    monkeypatch.setitem(app.config, "LIBRARY_DIR", str(tmp_path))
    monkeypatch.setattr(
        dvr,
        "_on_disk",
        lambda file: os.path.exists(os.path.join(str(tmp_path), file.file_path)),
    )
    monkeypatch.setattr(dvr, "_probe_duration", lambda path: 3600.0)
    with app.app_context():
        movie = make_movie("Rebuilt", 1938)
        make_movie_file(movie, "WEBRip-1080p")
        present = make_movie_file(movie, "WEBDL-720p")
        absent = make_movie_file(make_movie("Absent", 1990), "Bluray-1080p")
        db.session.commit()
        app.redis.hset(dvr.DURATIONS_KEY, str(absent.id), "5400.000")
        on_disk = tmp_path / present.file_path
        on_disk.parent.mkdir(parents=True)
        on_disk.write_bytes(b"mkv")
        assert dvr.build_channel_lineups() is True

    lineup = dvr.channel_lineup(app.redis, "fitzflix-mix")
    by_title = {p["title"]: p["file_path"] for p in lineup["programs"]}
    assert by_title["Rebuilt"].endswith("[WEBDL-720p].mkv")
    assert "Absent" not in by_title


def test_build_fills_the_cap_past_a_failed_probe(app, monkeypatch):
    """Test that a failed probe does not shrink the lineup of the day.

    A film whose probe fails gives its slot to the next candidate."""

    from app import dvr

    monkeypatch.setitem(app.config, "DVR_CHANNEL_FILMS", 2)
    monkeypatch.setattr(
        dvr, "_probe_duration", lambda path: None if "Drama 0" in path else 3600.0
    )
    with app.app_context():
        for n in range(3):
            make_movie_file(make_movie(f"Drama {n}", 1990 + n), "Bluray-1080p")
        db.session.commit()
        assert dvr.build_channel_lineups() is True

    lineup = dvr.channel_lineup(app.redis, "fitzflix-mix")
    titles = {p["title"] for p in lineup["programs"]}
    assert len(titles) == 2 and "Drama 0" not in titles


def test_criterion_channel_counts_scraped_arrivals(app, monkeypatch):
    """Test that the Criterion channel counts the scraped arrivals.

    Day-one arrivals on the newly-added page of the Channel join the
    Criterion channel before the payload of TMDB catches up."""

    from app import dvr
    from app.newly_added import NEWLY_ADDED_KEY

    monkeypatch.setattr(dvr, "_probe_duration", lambda path: 3600.0)
    # A cache-only read answers for no film. The availability entries of
    # the films are cold (imported after the refresh). The real
    # fetch_limit=0 call reports them the same way. Thus, the scraped
    # store alone must carry the match
    monkeypatch.setattr(
        dvr, "batch_title_availability", lambda tmdb_ids, **kwargs: ({}, [])
    )
    app.redis.set(
        NEWLY_ADDED_KEY.format(provider_id=258),
        json.dumps(
            {
                "items": [
                    {"tmdb_id": 9000 + n, "first_seen": date.today().isoformat()}
                    for n in range(3)
                ]
            }
        ),
    )
    with app.app_context():
        for n in range(3):
            make_movie_file(
                make_movie(f"Criterion {n}", 1950 + n, tmdb_id=9000 + n),
                "Bluray-1080p",
            )
        make_movie_file(make_movie("Other", 1990, tmdb_id=9500), "Bluray-1080p")
        db.session.commit()
        assert dvr.build_channel_lineups() is True

    lineup = dvr.channel_lineup(app.redis, "criterion")
    assert {p["title"] for p in lineup["programs"]} == {
        "Criterion 0",
        "Criterion 1",
        "Criterion 2",
    }


def test_build_keeps_stored_lineups_while_a_share_is_offline(app, monkeypatch):
    """Test that a build keeps the stored lineups while a share is offline.

    Off a dead share, every row reads as absent. A build on that reading
    must not replace the working dial with nothing."""

    from app import dvr

    monkeypatch.setattr(dvr, "_library_online", lambda: False)
    app.redis.set(dvr.CHANNELS_KEY, json.dumps([{"slug": "fitzflix-mix"}]))
    app.redis.set(dvr.LINEUP_KEY.format(slug="fitzflix-mix"), json.dumps({"x": 1}))
    with app.app_context():
        make_movie_file(make_movie("Drama", 1990), "Bluray-1080p")
        db.session.commit()
        assert dvr.build_channel_lineups() is True
    assert json.loads(app.redis.get(dvr.LINEUP_KEY.format(slug="fitzflix-mix"))) == {
        "x": 1
    }


def test_build_keeps_stored_lineups_when_every_probe_fails(app, monkeypatch):
    """Test that a build keeps the stored lineups when every probe fails.

    Files on disk but not one built program is a probe outage. It is not
    an empty dial."""

    from app import dvr

    monkeypatch.setattr(dvr, "_probe_duration", lambda path: None)
    app.redis.set(dvr.LINEUP_KEY.format(slug="fitzflix-mix"), json.dumps({"x": 1}))
    with app.app_context():
        make_movie_file(make_movie("Drama", 1990), "Bluray-1080p")
        db.session.commit()
        assert dvr.build_channel_lineups() is True
    assert app.redis.get(dvr.LINEUP_KEY.format(slug="fitzflix-mix")) is not None


def test_series_episodes_prefer_the_copy_on_disk(app, monkeypatch):
    """Test that the episode selector skips absent rows.

    The movie selector does the same. An absent best copy yields to a
    present lesser one. An episode with no copy on disk is left out."""

    from app import dvr

    monkeypatch.setattr(dvr, "_on_disk", lambda file: "HDTV" in file.basename)
    with app.app_context():
        series = make_tv_series("Show")
        make_tv_file(series, 1, 1, "Bluray-1080p")
        make_tv_file(series, 1, 1, "HDTV-720p")
        make_tv_file(series, 1, 2, "Bluray-1080p")
        db.session.commit()
        episodes = dvr._series_episodes(series.id)
        assert [(f.season, f.episode, f.quality.quality_title) for f in episodes] == [
            (1, 1, "HDTV-720p")
        ]


def test_enqueue_lineup_rebuild_gates_and_dedupes(app, monkeypatch):
    """Test the gates of the rebuild enqueue and its duplicate check.

    A rebuild queues only with a configured DVR. It queues only when the
    given films include an owned one. Only 1 rebuild queues at a
    time."""

    from app import dvr
    from tests.conftest import dvr_rebuild_jobs

    with app.app_context():
        make_movie_file(make_movie("Owned", 1990, tmdb_id=9000), "Bluray-1080p")
        db.session.commit()

        monkeypatch.setitem(app.config, "DVR_TOKEN", None)
        assert dvr.enqueue_lineup_rebuild("test") is None
        monkeypatch.setitem(app.config, "DVR_TOKEN", TOKEN)

        # Arrivals that nobody owns (or that never matched) change no program
        assert dvr.enqueue_lineup_rebuild("test", tmdb_ids=[9500, None]) is None
        assert dvr.enqueue_lineup_rebuild("test", tmdb_ids=[]) is None
        assert dvr_rebuild_jobs(app) == []

        assert dvr.enqueue_lineup_rebuild("test", tmdb_ids=[9500, 9000]) is not None
        assert dvr.enqueue_lineup_rebuild("again") is None
        assert len(dvr_rebuild_jobs(app)) == 1


@pytest.mark.parametrize(
    "stored, expected",
    [("5.1", 6), ("7.1", 6), ("2.0", 2), ("1.0", 1), ("6.0", 6), ("16", 6), ("4.1", 5)],
)
def test_first_audio_channels_counts_the_lfe(app, stored, expected):
    """Test that the audio-channel count includes the LFE channel.

    The scan stores "5.1" for 6 channels. A digits-only read gave 5.
    Then ffmpeg dropped the subwoofer from every 5.1 airing."""

    from app import dvr
    from app.models import FileAudioTrack

    with app.app_context():
        file = make_movie_file(make_movie("Loud", 1990), "Bluray-1080p")
        db.session.add(
            FileAudioTrack(
                file_id=file.id,
                track=1,
                language="eng",
                language_name="English",
                channels=stored,
                default=True,
            )
        )
        db.session.commit()
        assert dvr._first_audio_channels(file.id) == expected


def test_enqueue_lineup_rebuild_never_reuses_a_job_id(app, monkeypatch):
    """Each rebuild gets its own id. rq keeps the expiry of an old record
    under a reused id, so a queued job could vanish before a worker
    reached it. A rebuild that is RUNNING is not on the queue, so a
    trigger during a build queues a new one."""

    from app import dvr
    from tests.conftest import dvr_rebuild_jobs

    with app.app_context():
        monkeypatch.setitem(app.config, "DVR_TOKEN", TOKEN)
        queue = app.maintenance_queue
        first = dvr.enqueue_lineup_rebuild("first")
        assert first.id.startswith(dvr.REBUILD_JOB_ID + "-")
        assert dvr.enqueue_lineup_rebuild("again") is None
        queue.connection.lrem(queue.key, 0, first.id)  # a worker took it
        second = dvr.enqueue_lineup_rebuild("second")
        assert second is not None and second.id != first.id
        assert first.get_status() == "queued"  # the first record is intact
        assert len(dvr_rebuild_jobs(app)) == 1


def test_build_steps_aside_for_a_queued_rebuild(app, monkeypatch):
    """The cron build has a random id that the dedupe cannot see. When
    a triggered rebuild already waits, it has the fresher inputs, so
    the build does not write anything."""

    from app import dvr

    monkeypatch.setitem(app.config, "DVR_TOKEN", TOKEN)
    app.redis.set(dvr.LINEUP_KEY.format(slug="fitzflix-mix"), json.dumps({"x": 1}))
    with app.app_context():
        make_movie_file(make_movie("Drama", 1990), "Bluray-1080p")
        db.session.commit()
        assert dvr.enqueue_lineup_rebuild("waiting") is not None
        assert dvr.build_channel_lineups() is True
    assert json.loads(app.redis.get(dvr.LINEUP_KEY.format(slug="fitzflix-mix"))) == {
        "x": 1
    }
