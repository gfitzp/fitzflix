"""Test the Plex library refresh.

The refresh does a scan and an emptyTrash for each section. Fitzflix
refuses the full refresh of a section if one of the declared locations
of the section is missing. This guard prevents a dropped SMB mount from
deleting the library."""


class FakePlexServer:
    """Fake Plex sections with locations. Records the refresh and emptyTrash calls."""

    def __init__(self, sections):
        # The shape of sections: [(key, type, title, [paths])]
        self.sections = sections
        self.refreshed = []
        self.trashed = []

    def get(self, path, params=None):
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
