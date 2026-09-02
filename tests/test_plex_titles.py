"""Test the sync of episode titles into Plex.

A file with an edition sets the title of its Plex episode. The sync
matches the episode by basename and locks the title. It does not touch
anything else."""

from app import db
from tests.factories import make_file, make_tv_series


class FakePlexServer:
    """Fake the part of the local Plex API that the title sync uses."""

    def __init__(self, episodes):
        # episodes: [(ratingKey, title, filename)]
        self.episodes = [
            {"ratingKey": rk, "title": title, "file": name}
            for rk, title, name in episodes
        ]
        self.puts = []

    def get(self, path, params=None):
        params = params or {}
        if path == "/library/sections":
            return {
                "MediaContainer": {
                    "Directory": [
                        {"key": "5", "type": "movie"},
                        {"key": "4", "type": "show"},
                    ]
                }
            }
        if path == "/library/sections/4/all":
            start = int(params.get("X-Plex-Container-Start", 0))
            size = int(params.get("X-Plex-Container-Size", 1000))
            page = [
                {
                    "ratingKey": e["ratingKey"],
                    "title": e["title"],
                    "Media": [{"Part": [{"file": f"/Volumes/TV/{e['file']}"}]}],
                }
                for e in self.episodes[start : start + size]
            ]
            return {
                "MediaContainer": {
                    "totalSize": len(self.episodes),
                    "Metadata": page,
                }
            }
        raise AssertionError(f"unexpected GET {path}")

    def put(self, path, params):
        assert path == "/library/sections/4/all"
        assert params["title.locked"] == 1
        self.puts.append((params["id"], params["title.value"]))
        for e in self.episodes:
            if e["ratingKey"] == params["id"]:
                e["title"] = params["title.value"]


def make_titled_file(basename, edition=None, season=0, episode=90001):
    from app.models import TVSeries

    series = TVSeries.query.filter_by(title="Titled Show").first()
    if series is None:
        series = make_tv_series("Titled Show")
    return make_file(
        basename,
        "TV Shows/Titled Show/Specials",
        basename.rsplit(".", 1)[0],
        "TV Shows",
        "DVD",
        series_id=series.id,
        season=season,
        episode=episode,
        last_episode=episode,
        edition=edition,
    )


def test_edition_titles_reach_plex_locked(app, monkeypatch):
    import app.plex_titles as plex_titles

    with app.app_context():
        make_titled_file(
            "Titled Show - S00E90001 - The Lost Special [DVD].mkv",
            edition="The Lost Special",
            episode=90001,
        )
        make_titled_file(
            "Titled Show - S00E90002 - Already Right [DVD].mkv",
            edition="Already Right",
            episode=90002,
        )
        make_titled_file(
            "Titled Show - S01E01 - [DVD].mkv", edition=None, season=1, episode=1
        )
        db.session.commit()

        fake = FakePlexServer(
            [
                # Wrong title: the sync rewrites and locks it.
                (
                    "201",
                    "Episode 90001",
                    "Titled Show - S00E90001 - The Lost Special [DVD].mkv",
                ),
                # Already correct: the sync does not touch it.
                (
                    "202",
                    "Already Right",
                    "Titled Show - S00E90002 - Already Right [DVD].mkv",
                ),
                # No edition on the file: the agent title stays, even if
                # it is odd.
                ("203", "Agent Title", "Titled Show - S01E01 - [DVD].mkv"),
                # This is not a Fitzflix file.
                ("204", "Someone Else", "Other Show - S01E01 - [DVD].mkv"),
            ]
        )
        monkeypatch.setitem(app.config, "PLEX_URL", "http://plex.test")
        monkeypatch.setitem(app.config, "PLEX_TOKEN", "token")
        monkeypatch.setattr(plex_titles, "_plex_get", fake.get)
        monkeypatch.setattr(plex_titles, "_plex_put", fake.put)

        assert plex_titles.sync_plex_episode_titles() is True
        assert fake.puts == [("201", "The Lost Special")]

        # The sync is idempotent. A second run changes nothing.
        assert plex_titles.sync_plex_episode_titles() is True
        assert fake.puts == [("201", "The Lost Special")]


def test_pagination_walks_every_episode(app, monkeypatch):
    import app.plex_titles as plex_titles

    with app.app_context():
        make_titled_file(
            "Titled Show - S00E90003 - Deep Cut [DVD].mkv",
            edition="Deep Cut",
            episode=90003,
        )
        db.session.commit()

        # The target is after the first page.
        episodes = [
            (str(n), f"Filler {n}", f"Filler - S01E{n:02d} [DVD].mkv")
            for n in range(1, 1500)
        ]
        episodes.append(
            ("9999", "Episode 90003", "Titled Show - S00E90003 - Deep Cut [DVD].mkv")
        )
        fake = FakePlexServer(episodes)
        monkeypatch.setitem(app.config, "PLEX_URL", "http://plex.test")
        monkeypatch.setitem(app.config, "PLEX_TOKEN", "token")
        monkeypatch.setattr(plex_titles, "_plex_get", fake.get)
        monkeypatch.setattr(plex_titles, "_plex_put", fake.put)

        assert plex_titles.sync_plex_episode_titles() is True
        assert fake.puts == [("9999", "Deep Cut")]
