"""Test the generic Plex re-analyze.

Each task that rewrites a library file in place asks Plex to read the
file again. This is necessary because the path of the item does not
change. Nothing else makes Plex look again before its next scan (#194).
"""

import os


class FakePlexServer:
    """Fake Plex sections with locations and items.

    Records the sections that Fitzflix paged and the rating keys that
    Fitzflix analyzed."""

    def __init__(self, sections, items=None, extras=None):
        # The shape of sections: [(key, type, title, [paths])]
        # The shape of items: {section key: [(ratingKey, [part file paths])]}
        # or, for an episode, (ratingKey, [paths], {parentRatingKey, ...}).
        # The shape of extras: {holder ratingKey: [(ratingKey, [paths])]}
        self.sections = sections
        self.items = items or {}
        self.extras = extras or {}
        self.paged = []
        self.analyzed = []
        self.extras_asked = []

    def get(self, path, params=None):
        if path == "/library/sections":
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
        if path.startswith("/library/metadata/"):
            holder = path.split("/")[3]
            assert params == {"includeExtras": 1}, params
            self.extras_asked.append(holder)
            return {
                "MediaContainer": {
                    "Metadata": [
                        {
                            "ratingKey": holder,
                            "Extras": {
                                "Metadata": [
                                    {
                                        "ratingKey": rating_key,
                                        "Media": [
                                            {"Part": [{"file": p} for p in parts]}
                                        ],
                                    }
                                    for rating_key, parts in self.extras.get(holder, [])
                                ]
                            },
                        }
                    ]
                }
            }
        key = path.split("/")[3]
        self.paged.append(key)
        page = self.items.get(key, [])
        return {
            "MediaContainer": {
                "totalSize": len(page),
                "Metadata": [
                    {
                        "ratingKey": item[0],
                        "Media": [{"Part": [{"file": p} for p in item[1]]}],
                        **(item[2] if len(item) > 2 else {}),
                    }
                    for item in page
                ],
            }
        }

    def command(self, method, path):
        assert method == "PUT", path
        assert path.endswith("/analyze"), path
        self.analyzed.append(path.split("/")[3])


def _library(tmp_path):
    """Build a movies and TV library on disk. Return the absolute path of the film."""

    movies = tmp_path / "Movies"
    movies.mkdir()
    tv = tmp_path / "TV Shows"
    tv.mkdir()
    film = movies / "Serial Experiments (1998) - [Bluray-1080p].mkv"
    film.write_bytes(b"")
    return movies, tv, str(film)


def _wire(app, monkeypatch, fake):
    import app.plex_library as plex_library

    monkeypatch.setitem(app.config, "PLEX_URL", "http://plex.test")
    monkeypatch.setitem(app.config, "PLEX_TOKEN", "token")
    monkeypatch.setattr(plex_library, "plex_get", fake.get)
    monkeypatch.setattr(plex_library, "plex_command", fake.command)
    return plex_library


def test_analyze_hits_the_item_holding_the_file(app, monkeypatch, tmp_path):
    movies, tv, film = _library(tmp_path)
    fake = FakePlexServer(
        [
            ("5", "movie", "Movies", [str(movies)]),
            ("4", "show", "TV Shows", [str(tv)]),
        ],
        {
            "5": [("101", ["/elsewhere/other.mkv"]), ("202", [film])],
            "4": [("303", [str(tv / "Show - S01E01 - Pilot.mkv")])],
        },
    )
    plex_library = _wire(app, monkeypatch, fake)

    assert plex_library.analyze_plex_media([film]) is True

    # Fitzflix analyzed the item of the film, and nothing else.

    assert fake.analyzed == ["202"]

    # Fitzflix paged only the section that has the location of the file.
    # The analyze of a movie never walks the TV section. That section is
    # much larger.

    assert fake.paged == ["5"]


def test_analyze_matches_on_basename_when_plex_mounts_elsewhere(
    app, monkeypatch, tmp_path
):
    """Resolve the file when Plex reaches the library through a different mount path.

    The basenames in the library carry the title, the year, and the
    quality. Thus, a basename identifies a file on its own."""

    movies, tv, film = _library(tmp_path)
    plex_side = "/mnt/plex-media/Movies/" + os.path.basename(film)
    fake = FakePlexServer(
        [("5", "movie", "Movies", ["/mnt/plex-media/Movies"])],
        {"5": [("202", [plex_side])]},
    )
    plex_library = _wire(app, monkeypatch, fake)

    assert plex_library.analyze_plex_media([film]) is True
    assert fake.analyzed == ["202"]


def test_analyze_refuses_a_file_whose_mount_is_gone(app, monkeypatch, tmp_path):
    """Guard each file, not each section.

    Fitzflix does not touch a file on a dead or hung mount. An analyze
    of such a file makes a record of what Plex could not read."""

    movies, tv, film = _library(tmp_path)
    fake = FakePlexServer(
        [("5", "movie", "Movies", [str(movies)])], {"5": [("202", [film])]}
    )
    plex_library = _wire(app, monkeypatch, fake)
    monkeypatch.setattr(plex_library, "volume_alive", lambda path, timeout=10: False)

    with app.app_context():
        assert plex_library.analyze_plex_media([film]) is True
        assert fake.analyzed == []
        assert fake.paged == []

        # Fitzflix defers the unfinished work. It does not drop the work.

        scheduled = app.maintenance_queue.scheduled_job_registry.get_job_ids()
        assert len(scheduled) == 1


def test_a_file_plex_hasnt_scanned_is_retried_once_then_dropped(
    app, monkeypatch, tmp_path
):
    movies, tv, film = _library(tmp_path)
    fake = FakePlexServer([("5", "movie", "Movies", [str(movies)])], {"5": []})
    plex_library = _wire(app, monkeypatch, fake)

    with app.app_context():
        assert plex_library.analyze_plex_media([film]) is True
        assert fake.analyzed == []

        registry = app.maintenance_queue.scheduled_job_registry
        job_ids = registry.get_job_ids()
        assert len(job_ids) == 1
        job = app.maintenance_queue.fetch_job(job_ids[0])
        assert job.func_name == "app.plex_library.analyze_plex_media"
        assert job.args[0] == [film]
        assert job.kwargs["retries"] == 1

        # The retry occurs after the scan that runs every 15 minutes.
        # After that retry, Fitzflix drops the matter. It does not retry
        # forever.

        assert (
            plex_library.analyze_plex_media(
                [film], retries=plex_library.ANALYZE_MAX_RETRIES
            )
            is True
        )
        assert registry.get_job_ids() == job_ids


def test_enqueue_is_a_no_op_without_a_plex_server(app, monkeypatch, tmp_path):
    import app.plex_library as plex_library

    monkeypatch.setitem(app.config, "PLEX_URL", None)
    monkeypatch.setitem(app.config, "PLEX_TOKEN", None)

    with app.app_context():
        assert plex_library.enqueue_plex_analyze("/library/Movies/Film.mkv") is False
        assert app.maintenance_queue.jobs == []


def test_enqueue_collapses_a_second_edit_onto_the_queued_analyze(
    app, monkeypatch, tmp_path
):
    import app.plex_library as plex_library

    monkeypatch.setitem(app.config, "PLEX_URL", "http://plex.test")
    monkeypatch.setitem(app.config, "PLEX_TOKEN", "token")
    path = str(tmp_path / "Film (2020) - [Bluray-1080p].mkv")

    with app.app_context():
        assert plex_library.enqueue_plex_analyze(path) is True

        # The queued job has not read the file yet. Thus, a second edit
        # does not need a second job.

        assert plex_library.enqueue_plex_analyze(path) is False

        jobs = app.maintenance_queue.jobs
        assert len(jobs) == 1
        assert jobs[0].func_name == "app.plex_library.analyze_plex_media"
        assert jobs[0].args[0] == [path]


def _extra(folder, kind, name):
    """Create an extra file under the folder of a title. Return its path."""

    path = folder / kind / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return str(path)


def test_an_extra_is_analyzed_through_its_film(app, monkeypatch, tmp_path):
    """Resolve an extra through the film that holds it (#651).

    Plex lists an extra only with its film, never in a section page. A
    track edit on a featurette rewrote the file, and the analyze found
    nothing to analyze. Plex kept the old track list until a metadata
    refresh of the film by hand."""

    movies, tv, film = _library(tmp_path)
    folder = movies / "The Shining (1980)"
    folder.mkdir()
    feature = folder / "The Shining (1980) - [Bluray-1080p].mkv"
    feature.write_bytes(b"")
    extra = _extra(folder, "Behind The Scenes", "30 Impossible Shots.mkv")
    fake = FakePlexServer(
        [("5", "movie", "Movies", [str(movies)])],
        {"5": [("202", [film]), ("300", [str(feature)])]},
        {"300": [("301", [extra])]},
    )
    plex_library = _wire(app, monkeypatch, fake)

    assert plex_library.analyze_plex_media([extra]) is True

    # Fitzflix analyzed the clip of the extra, not the film, and asked
    # only the film whose folder holds the extra.

    assert fake.analyzed == ["301"]
    assert fake.extras_asked == ["300"]


def test_a_same_named_extra_resolves_to_its_own_film(app, monkeypatch, tmp_path):
    """An extra name repeats across films. The folder of the film decides."""

    movies, tv, film = _library(tmp_path)
    first = movies / "Alien (1979)"
    second = movies / "Aliens (1986)"
    for folder in (first, second):
        folder.mkdir()
        (folder / f"{folder.name} - [Bluray-1080p].mkv").write_bytes(b"")
    first_trailer = _extra(first, "Trailers", "Trailer.mkv")
    second_trailer = _extra(second, "Trailers", "Trailer.mkv")
    fake = FakePlexServer(
        [("5", "movie", "Movies", [str(movies)])],
        {
            "5": [
                ("300", [str(first / "Alien (1979) - [Bluray-1080p].mkv")]),
                ("400", [str(second / "Aliens (1986) - [Bluray-1080p].mkv")]),
            ]
        },
        {"300": [("301", [first_trailer])], "400": [("401", [second_trailer])]},
    )
    plex_library = _wire(app, monkeypatch, fake)

    assert plex_library.analyze_plex_media([second_trailer]) is True
    assert fake.analyzed == ["401"]
    assert fake.extras_asked == ["400"]


def test_a_show_extra_is_analyzed_through_the_show(app, monkeypatch, tmp_path):
    """An extra in the folder of a show belongs to the show, not to an episode."""

    movies, tv, film = _library(tmp_path)
    show = tv / "Show"
    season = show / "Season 01"
    season.mkdir(parents=True)
    episode = season / "Show - S01E01 - Pilot.mkv"
    episode.write_bytes(b"")
    extra = _extra(show, "Behind The Scenes", "Making Of.mkv")
    fake = FakePlexServer(
        [("4", "show", "TV Shows", [str(tv)])],
        {
            "4": [
                (
                    "303",
                    [str(episode)],
                    {"parentRatingKey": "302", "grandparentRatingKey": "301"},
                )
            ]
        },
        {"301": [("310", [extra])]},
    )
    plex_library = _wire(app, monkeypatch, fake)

    assert plex_library.analyze_plex_media([extra]) is True
    assert fake.analyzed == ["310"]
    assert fake.extras_asked == ["301"]
