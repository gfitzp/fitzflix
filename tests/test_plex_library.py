"""The Plex library refresh: scan + emptyTrash per
section, refused entirely while any of the section's declared
locations is missing — the guard that keeps a dropped SMB mount from
wiping the library."""


class FakePlexServer:
    """Sections with locations, recording refresh/emptyTrash calls."""

    def __init__(self, sections):
        # sections: [(key, type, title, [paths])]
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
    missing = tmp_path / "TV Shows"  # never created

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

    # Movies (all locations present): scanned and trash emptied. TV
    # (location missing): untouched — the catastrophic case. Music:
    # not a movie/show section, ignored

    assert fake.refreshed == ["5"]
    assert fake.trashed == ["5"]


def test_refresh_skips_sections_on_dead_mounts(app, monkeypatch, tmp_path):
    """A mount that HANGS (not just missing) must also be treated as
    dead — volume_alive's verdict is honored."""

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
