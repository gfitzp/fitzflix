"""Test the Plex library refresh.

The refresh does a scan and an emptyTrash for each section. Fitzflix
refuses the full refresh of a section if one of the declared locations
of the section is missing. This guard prevents a dropped SMB mount from
deleting the library."""


class FakePlexServer:
    """Fake Plex sections with locations. Records the refresh and emptyTrash calls."""

    def __init__(self, sections, trash=None):
        # The shape of sections: [(key, type, title, [paths])]
        self.sections = sections
        # The number of items in the trash of each section key (#280).
        # None for a key makes the count query fail.
        self.trash = trash or {}
        self.refreshed = []
        self.trashed = []

    def get(self, path, params=None):
        if path.endswith("/all"):
            assert params["trash"] == 1
            count = self.trash.get(path.split("/")[3], 0)
            if count is None:
                raise RuntimeError("count query failed")
            return {"MediaContainer": {"size": 0, "totalSize": count}}
        assert path == "/library/sections"
        return {
            "MediaContainer": {
                "Directory": [
                    {
                        "key": key,
                        "type": kind,
                        "title": title,
                        "Location": [{"path": p} for p in paths],
                    }
                    for key, kind, title, paths in self.sections
                ]
            }
        }

    def command(self, method, path):
        if path.endswith("/refresh"):
            assert method == "GET"
            self.refreshed.append(path.split("/")[3])
        elif path.endswith("/emptyTrash"):
            assert method == "PUT"
            self.trashed.append(path.split("/")[3])
        else:
            raise AssertionError(f"unexpected command {method} {path}")


def test_refresh_guards_every_declared_location(app, monkeypatch, tmp_path):
    import app.plex_library as plex_library

    present = tmp_path / "Movies"
    present.mkdir()
    also_present = tmp_path / "transcoded"
    also_present.mkdir()
    missing = tmp_path / "TV Shows"  # this path is never created

    fake = FakePlexServer(
        [
            ("5", "movie", "Movies", [str(present), str(also_present)]),
            ("4", "show", "TV Shows", [str(missing)]),
            ("9", "artist", "Music", []),
        ]
    )
    monkeypatch.setitem(app.config, "PLEX_URL", "http://plex.test")
    monkeypatch.setitem(app.config, "PLEX_TOKEN", "token")
    monkeypatch.setattr(plex_library, "_plex_get", fake.get)
    monkeypatch.setattr(plex_library, "_plex_command", fake.command)

    assert plex_library.refresh_plex_libraries() is True

    # Movies has all of its locations. Thus, Plex scans it and empties its
    # trash. TV has a missing location. Thus, Fitzflix does not touch it.
    # That is the catastrophic case. Music is not a movie or show section.
    # Thus, Fitzflix ignores it.

    assert fake.refreshed == ["5"]
    assert fake.trashed == ["5"]


def test_refresh_skips_sections_on_dead_mounts(app, monkeypatch, tmp_path):
    """Treat a mount that HANGS as dead, the same as a missing mount.

    The refresh obeys the verdict of volume_alive."""

    import app.plex_library as plex_library

    existing = tmp_path / "Movies"
    existing.mkdir()
    fake = FakePlexServer([("5", "movie", "Movies", [str(existing)])])
    monkeypatch.setitem(app.config, "PLEX_URL", "http://plex.test")
    monkeypatch.setitem(app.config, "PLEX_TOKEN", "token")
    monkeypatch.setattr(plex_library, "_plex_get", fake.get)
    monkeypatch.setattr(plex_library, "_plex_command", fake.command)
    monkeypatch.setattr(plex_library, "volume_alive", lambda path, timeout=10: False)

    assert plex_library.refresh_plex_libraries() is True
    assert fake.refreshed == []
    assert fake.trashed == []


def _guarded_refresh(app, monkeypatch, tmp_path, trash=None):
    """Return the module and a fake server with 1 healthy Movies section."""

    import app.plex_library as plex_library

    movies = tmp_path / "Movies"
    movies.mkdir(exist_ok=True)
    fake = FakePlexServer([("5", "movie", "Movies", [str(movies)])], trash=trash)
    monkeypatch.setitem(app.config, "PLEX_URL", "http://plex.test")
    monkeypatch.setitem(app.config, "PLEX_TOKEN", "token")
    monkeypatch.setattr(plex_library, "_plex_get", fake.get)
    monkeypatch.setattr(plex_library, "_plex_command", fake.command)
    return plex_library, fake


def test_refresh_keeps_a_trash_over_the_limit(app, monkeypatch, tmp_path):
    """A trash with more items than PLEX_TRASH_LIMIT stays (#280).

    The hold makes a health issue. The first run under the limit empties
    the trash and removes the issue."""

    from app.maintenance import PLEX_TRASH_HELD_KEY

    monkeypatch.setitem(app.config, "PLEX_TRASH_LIMIT", 25)
    plex_library, fake = _guarded_refresh(app, monkeypatch, tmp_path, trash={"5": 3356})

    assert plex_library.refresh_plex_libraries() is True
    assert fake.refreshed == ["5"]
    assert fake.trashed == []
    held = app.redis.hget(PLEX_TRASH_HELD_KEY, "Movies").decode()
    assert "3356 items" in held and "PLEX_TRASH_LIMIT (25)" in held

    # The next scan restored the items. The trash is small again.

    fake.trash["5"] = 2
    assert plex_library.refresh_plex_libraries() is True
    assert fake.trashed == ["5"]
    assert app.redis.hget(PLEX_TRASH_HELD_KEY, "Movies") is None


def test_refresh_keeps_the_trash_for_one_run_after_a_remount(
    app, monkeypatch, tmp_path
):
    """A new device number on a mount keeps the trash for that run (#280)."""

    plex_library, fake = _guarded_refresh(app, monkeypatch, tmp_path)
    devices = iter(["1", "2", "2"])
    monkeypatch.setattr(
        plex_library,
        "_mount_ids",
        lambda targets: {target: next(devices) for target in targets},
    )

    # The first run has no earlier number to compare. It empties the trash.

    assert plex_library.refresh_plex_libraries() is True
    assert fake.trashed == ["5"]

    # The share came back with a new number. The scan still runs.

    assert plex_library.refresh_plex_libraries() is True
    assert fake.refreshed == ["5", "5"]
    assert fake.trashed == ["5"]

    # The number did not change again. The trash empties.

    assert plex_library.refresh_plex_libraries() is True
    assert fake.trashed == ["5", "5"]


def test_refresh_keeps_the_trash_when_the_count_fails(app, monkeypatch, tmp_path):
    """No count means no empty (#280)."""

    plex_library, fake = _guarded_refresh(app, monkeypatch, tmp_path, trash={"5": None})

    assert plex_library.refresh_plex_libraries() is True
    assert fake.refreshed == ["5"]
    assert fake.trashed == []


def test_refresh_clears_the_issue_of_a_removed_library(app, monkeypatch, tmp_path):
    """A held trash of a library that no longer exists is not a health issue."""

    from app.maintenance import PLEX_TRASH_HELD_KEY

    app.redis.hset(PLEX_TRASH_HELD_KEY, "Old Movies", "held")
    plex_library, fake = _guarded_refresh(app, monkeypatch, tmp_path)

    assert plex_library.refresh_plex_libraries() is True
    assert app.redis.hexists(PLEX_TRASH_HELD_KEY, "Old Movies") is False
