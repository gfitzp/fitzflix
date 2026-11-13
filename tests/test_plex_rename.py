"""Test the Plex match pin and the add-date guard of a rename.

A rename pins the match of the Plex item to the TMDB id of the record
before the file moves, and records the item. After the next scan, the
refresh writes the old add date back on a title that Plex made new."""

import json
import os
import time

import pytest

from app import db
from app.plex_rename import (
    PENDING_KEY,
    find_plex_item,
    remember_rename,
    restore_add_dates,
)

from tests.factories import make_movie, make_movie_file, make_tv_file, make_tv_series


class FakePlex:
    """A Plex server with one movie section and one show section.

    items: {rating_key: {"section", "type", "file", "guid", "ids",
    "addedAt", "show", "edition"}}. As on the live server, an edition
    item has the guid of the film and an editionTitle (#679). The fake answers the section list, the
    section pages, the metadata of one item, the match search and the
    writes. A match PUT gives the item the ids of the match on the
    second metadata read, like the background fetch of Plex."""

    def __init__(self, root):
        self.root = root
        self.items = {}
        self.matches = {}
        self.puts = []
        self.pending_ids = {}

    def add(
        self,
        key,
        section,
        kind,
        file,
        guid="",
        ids=(),
        added=100,
        show=None,
        edition=None,
    ):
        self.items[key] = {
            "section": section,
            "type": kind,
            "file": file,
            "guid": guid,
            "ids": set(ids),
            "addedAt": added,
            "show": show,
            "edition": edition,
        }

    def get(self, path, params=None, timeout=None):
        params = params or {}
        if path == "/library/sections":
            return {
                "MediaContainer": {
                    "Directory": [
                        {
                            "key": "5",
                            "type": "movie",
                            "title": "Movies",
                            "Location": [{"path": os.path.join(self.root, "Movies")}],
                        },
                        {
                            "key": "4",
                            "type": "show",
                            "title": "TV Shows",
                            "Location": [{"path": os.path.join(self.root, "TV Shows")}],
                        },
                    ]
                }
            }
        if path.endswith("/all"):
            section = path.split("/")[3]
            page = [
                {
                    "ratingKey": key,
                    "grandparentRatingKey": item["show"],
                    "Media": [{"Part": [{"file": item["file"]}]}],
                }
                for key, item in self.items.items()
                if item["section"] == section and item["file"]
            ]
            return {"MediaContainer": {"Metadata": page, "totalSize": len(page)}}
        if path.endswith("/matches"):
            key = path.split("/")[3]
            return {"MediaContainer": {"SearchResult": self.matches.get(key, [])}}
        key = path.split("/")[3]
        item = self.items[key]
        if key in self.pending_ids:
            item["ids"] |= self.pending_ids.pop(key)
        meta = {
            "ratingKey": key,
            "guid": item["guid"],
            "Guid": [{"id": guid} for guid in sorted(item["ids"])],
            "addedAt": item["addedAt"],
        }
        if item["edition"]:
            meta["editionTitle"] = item["edition"]
        return {"MediaContainer": {"Metadata": [meta]}}

    def command(self, method, path, params=None):
        self.puts.append((method, path, dict(params or {})))
        if path.endswith("/match"):
            key = path.split("/")[3]
            self.items[key]["guid"] = params["guid"]
            self.pending_ids[key] = {
                f"{match['id']}" for match in self.matches[key] if "id" in match
            }


@pytest.fixture
def plex(app, monkeypatch, tmp_path):
    """A fake Plex, wired into app.plex_rename, with Plex configured."""

    import app.plex_rename as plex_rename

    fake = FakePlex(str(tmp_path))
    monkeypatch.setitem(app.config, "PLEX_URL", "http://plex.test")
    monkeypatch.setitem(app.config, "PLEX_TOKEN", "token")
    monkeypatch.setattr(plex_rename, "plex_get", fake.get)
    monkeypatch.setattr(plex_rename, "plex_command", fake.command)
    monkeypatch.setattr(plex_rename, "_sleep", lambda seconds: None)
    return fake


def _movie_path(plex, folder, name):
    return os.path.join(plex.root, "Movies", folder, name)


def test_a_rename_pins_the_match_and_records_the_item(app, plex):
    old = _movie_path(plex, "War and Peace (1968)", "War and Peace (1968).mkv")
    new = _movie_path(plex, "War and Peace (1966)", "War and Peace (1966).mkv")
    plex.add("192459", "5", "movie", old, "plex://movie/hindi", added=1000)
    plex.matches["192459"] = [
        {"guid": "plex://movie/bondarchuk", "name": "War and Peace", "year": 1966},
        {"id": "tmdb://29266"},
    ]

    with app.app_context():
        remember_rename(old, new, "movie", tmdb_id=29266)

        assert plex.puts == [
            (
                "PUT",
                "/library/metadata/192459/match",
                {
                    "guid": "plex://movie/bondarchuk",
                    "name": "War and Peace",
                    "year": 1966,
                },
            )
        ]
        record = json.loads(app.redis.hget(PENDING_KEY, new))
        assert record["rating_key"] == "192459"
        assert record["added_at"] == 1000
        assert record["kind"] == "movie"


def test_an_item_that_carries_the_id_needs_no_pin(app, plex):
    old = _movie_path(plex, "Queen (1952)", "Queen (1952).mkv")
    new = _movie_path(plex, "Queen (1951)", "Queen (1951).mkv")
    plex.add("7", "5", "movie", old, "plex://movie/queen", ids=["tmdb://488"])

    with app.app_context():
        remember_rename(old, new, "movie", tmdb_id=488)

    assert plex.puts == []
    with app.app_context():
        assert app.redis.hexists(PENDING_KEY, new)


def test_an_edition_and_a_missing_match_keep_their_guid(app, plex):
    """The pin sends no Fix Match request for an edition item (#679).

    As on the live server, the edition has the guid of the theatrical
    item and an editionTitle. Plex has a match for it. Thus, only the
    edition guard stops the request."""

    theatrical = _movie_path(plex, "Shine (1980)", "Shine (1980).mkv")
    edition = _movie_path(plex, "Shine (1980) {edition-Cut}", "Shine.mkv")
    lost = _movie_path(plex, "Lost (1999)", "Lost (1999).mkv")
    plex.add("7", "5", "movie", theatrical, "plex://movie/5d77shine")
    plex.add("8", "5", "movie", edition, "plex://movie/5d77shine", edition="Cut")
    plex.add("9", "5", "movie", lost, "plex://movie/lost")
    plex.matches["8"] = [
        {"guid": "plex://movie/5d77other", "name": "Shine", "year": 1980},
        {"id": "tmdb://1"},
    ]

    with app.app_context():
        remember_rename(edition, edition + ".new", "movie", tmdb_id=1)
        remember_rename(lost, lost + ".new", "movie", tmdb_id=2)

    # No match PUT for either. Both are still recorded for the guard.
    assert plex.puts == []
    assert plex.items["8"]["guid"] == plex.items["7"]["guid"]
    with app.app_context():
        assert app.redis.hlen(PENDING_KEY) == 2


def test_a_title_that_plex_does_not_list_records_nothing(app, plex):
    with app.app_context():
        remember_rename("/nowhere/a.mkv", "/nowhere/b.mkv", "movie", tmdb_id=3)
        assert app.redis.hlen(PENDING_KEY) == 0


def test_a_plex_fault_never_reaches_the_rename(app, plex, monkeypatch):
    import app.plex_rename as plex_rename

    def refused(path, params=None, timeout=None):
        raise RuntimeError("Plex is down")

    monkeypatch.setattr(plex_rename, "plex_get", refused)
    with app.app_context():
        remember_rename("/a.mkv", "/b.mkv", "movie", tmdb_id=3)


def test_nothing_happens_without_a_plex_server(app, monkeypatch):
    """With no Plex settings, the 2 functions make no call and record nothing.

    remember_rename catches each error. Thus, the fake records its calls
    and raises nothing (#731). The record of an earlier run stays, and
    restore_add_dates does not read Plex for it."""

    import app.plex_rename as plex_rename

    calls = []
    monkeypatch.setattr(
        plex_rename, "plex_get", lambda *args, **kwargs: calls.append(args)
    )
    _record(app, "/earlier.mkv", "9", 1000)
    with app.app_context():
        assert not plex_rename.plex_configured()
        remember_rename("/a.mkv", "/b.mkv", "movie", tmdb_id=3)
        restore_add_dates()
        assert calls == []
        assert [field.decode() for field in app.redis.hkeys(PENDING_KEY)] == [
            "/earlier.mkv"
        ]


def _record(app, new_path, rating_key, added, since=None, kind="movie"):
    with app.app_context():
        app.redis.hset(
            PENDING_KEY,
            new_path,
            json.dumps(
                {
                    "rating_key": rating_key,
                    "added_at": added,
                    "kind": kind,
                    "since": int(time.time()) if since is None else since,
                }
            ),
        )


def test_the_guard_restores_the_add_date_of_a_new_item(app, plex):
    new = _movie_path(plex, "War and Peace (1966)", "War and Peace (1966).mkv")
    plex.add("218397", "5", "movie", new, "plex://movie/bondarchuk", added=5000)
    _record(app, new, "192459", 1000)

    with app.app_context():
        restore_add_dates()
        assert plex.puts == [
            (
                "PUT",
                "/library/sections/5/all",
                {
                    "type": 1,
                    "id": "218397",
                    "addedAt.value": 1000,
                    "addedAt.locked": 1,
                },
            )
        ]
        assert app.redis.hlen(PENDING_KEY) == 0


def test_the_guard_leaves_an_item_that_survived(app, plex):
    new = _movie_path(plex, "Queen (1951)", "Queen (1951).mkv")
    plex.add("7", "5", "movie", new, "plex://movie/queen", added=5000)
    _record(app, new, "7", 1000)

    with app.app_context():
        restore_add_dates()
        assert plex.puts == []
        assert app.redis.hlen(PENDING_KEY) == 0


def test_the_guard_waits_for_the_scan_and_gives_up_after_a_day(app, plex):
    waiting = _movie_path(plex, "Soon (2000)", "Soon (2000).mkv")
    stale = _movie_path(plex, "Never (2000)", "Never (2000).mkv")
    _record(app, waiting, "1", 1000)
    _record(app, stale, "2", 1000, since=int(time.time()) - 25 * 3600)

    with app.app_context():
        restore_add_dates()
        assert plex.puts == []
        assert app.redis.hexists(PENDING_KEY, waiting)
        assert not app.redis.hexists(PENDING_KEY, stale)


def test_a_show_is_found_through_its_episodes(app, plex):
    old = os.path.join(plex.root, "TV Shows", "Batman")
    new = os.path.join(plex.root, "TV Shows", "Batman (1966)")
    plex.add("30", "4", "show", None, "plex://show/batman", added=700)
    plex.add(
        "31",
        "4",
        "episode",
        os.path.join(old, "Season 01", "Batman - S01E01.mkv"),
        show="30",
    )

    with app.app_context():
        assert find_plex_item(old, "show")["rating_key"] == "30"
        remember_rename(old, new, "show", tmdb_id=2287)
        assert plex.puts == []
        record = json.loads(app.redis.hget(PENDING_KEY, new))
        assert record == {
            "rating_key": "30",
            "added_at": 700,
            "kind": "show",
            "since": record["since"],
        }

        # The scan gives the renamed folder to a new show item.
        plex.add("40", "4", "show", None, "plex://show/batman", added=9000)
        plex.items["31"]["file"] = os.path.join(
            new, "Season 01", "Batman (1966) - S01E01.mkv"
        )
        plex.items["31"]["show"] = "40"
        restore_add_dates()
        assert plex.puts[-1] == (
            "PUT",
            "/library/sections/4/all",
            {"type": 2, "id": "40", "addedAt.value": 700, "addedAt.locked": 1},
        )


def test_the_movie_rename_keeps_the_main_feature_only(app, monkeypatch):
    import app.plex_rename as plex_rename
    from app.tmdb_refresh import _rename_movie_file

    kept = []
    monkeypatch.setattr(
        plex_rename, "remember_rename", lambda *args, **kwargs: kept.append(args)
    )
    with app.app_context():
        movie = make_movie("Desert Tune", 1943, tmdb_id=77)
        feature = make_movie_file(movie, "HDTV-720p")
        trailer = make_movie_file(movie, "HDTV-720p", "Trailers", plex_title="Teaser")
        feature.untouched_basename = "Desert Tune (1944) - [HDTV-720p].mkv"
        trailer.untouched_basename = (
            "Desert Tune (1944) - Trailers - Teaser [HDTV-720p].mkv"
        )
        db.session.commit()
        for file in (feature, trailer):
            path = os.path.join(app.config["LIBRARY_DIR"], file.file_path)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as handle:
                handle.write(b"payload")

        assert _rename_movie_file(feature, None) is True
        assert _rename_movie_file(trailer, None) is True

    library = app.config["LIBRARY_DIR"]
    assert kept == [
        (
            os.path.join(
                library,
                "Movies/Desert Tune (1943)/Desert Tune (1943) - [HDTV-720p].mkv",
            ),
            os.path.join(
                library,
                "Movies/Desert Tune (1944)/Desert Tune (1944) - [HDTV-720p].mkv",
            ),
            "movie",
        )
    ]


def test_the_series_rename_keeps_the_show_folder(app, monkeypatch):
    import app.series_rename as series_rename
    from app.series_rename import rename_tv_series_task

    kept = []
    monkeypatch.setattr(
        series_rename,
        "remember_rename",
        lambda *args, **kwargs: kept.append((args, kwargs)),
    )
    with app.app_context():
        series = make_tv_series("Batman", tmdb_id=2287, tvdb_id=77633)
        file = make_tv_file(series, 1, 1, "Bluray-1080p")
        db.session.commit()
        path = os.path.join(app.config["LIBRARY_DIR"], file.file_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            handle.write("video")
        series_id = series.id

        assert rename_tv_series_task(series_id, "Batman (1966)") is True

    library = app.config["LIBRARY_DIR"]
    assert kept == [
        (
            (
                os.path.join(library, "TV Shows", "Batman"),
                os.path.join(library, "TV Shows", "Batman (1966)"),
                "show",
            ),
            {"tmdb_id": 2287, "tvdb_id": 77633},
        )
    ]


def test_the_refresh_settles_the_records_before_its_scan(app, monkeypatch, tmp_path):
    import app.plex_library as plex_library
    import app.plex_rename as plex_rename
    from tests.test_plex_library import FakePlexServer

    present = tmp_path / "Movies"
    present.mkdir()
    fake = FakePlexServer([("5", "movie", "Movies", [str(present)])])
    order = []
    monkeypatch.setitem(app.config, "PLEX_URL", "http://plex.test")
    monkeypatch.setitem(app.config, "PLEX_TOKEN", "token")
    monkeypatch.setattr(plex_library, "plex_get", fake.get)
    monkeypatch.setattr(
        plex_library,
        "plex_command",
        lambda *args: order.append("scan") or fake.command(*args),
    )
    monkeypatch.setattr(
        plex_rename, "restore_add_dates", lambda: order.append("settle")
    )

    assert plex_library.refresh_plex_libraries() is True
    assert order[:2] == ["settle", "scan"]
