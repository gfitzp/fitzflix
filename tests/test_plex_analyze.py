"""The generic Plex re-analyze: every task that rewrites a library file
in place asks Plex to re-read it, since the item's path is unchanged
and nothing else prompts Plex to look again until its next scan (#194).
"""

import os


class FakePlexServer:
    """Sections with locations and items, recording which sections were
    paged and which rating keys were analyzed."""

    def __init__(self, sections, items=None):
        # sections: [(key, type, title, [paths])]
        # items: {section key: [(ratingKey, [part file paths])]}
        self.sections = sections
        self.items = items or {}
        self.paged = []
        self.analyzed = []

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
        key = path.split("/")[3]
        self.paged.append(key)
        page = self.items.get(key, [])
        return {
            "MediaContainer": {
                "totalSize": len(page),
                "Metadata": [
                    {
                        "ratingKey": rating_key,
                        "Media": [{"Part": [{"file": p} for p in parts]}],
                    }
                    for rating_key, parts in page
                ],
            }
        }

    def command(self, method, path):
        assert method == "PUT", path
        assert path.endswith("/analyze"), path
        self.analyzed.append(path.split("/")[3])


def _library(tmp_path):
    """A movies + TV library on disk, and the film's absolute path."""

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
    monkeypatch.setattr(plex_library, "_plex_get", fake.get)
    monkeypatch.setattr(plex_library, "_plex_command", fake.command)
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

    # The film's own item was analyzed, and nothing else

    assert fake.analyzed == ["202"]

    # Only the section whose location holds the file was paged: a
    # movie's analyze never walks the (much larger) TV section

    assert fake.paged == ["5"]


def test_analyze_matches_on_basename_when_plex_mounts_elsewhere(
    app, monkeypatch, tmp_path
):
    """Plex reaching the library by another mount path still resolves:
    the library's basenames carry title, year and quality, so they
    identify a file on their own."""

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
    """Guarded per file, not per section: a file on a dead or hung
    mount is left alone rather than analyzed into a record of what Plex
    couldn't read."""

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

        # Unfinished business is deferred, not dropped

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

        # The retry follows the quarter-hourly scan; after it, the
        # matter is dropped rather than retried forever

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

        # The queued job hasn't read the file yet, so a second edit
        # needs no second job

        assert plex_library.enqueue_plex_analyze(path) is False

        jobs = app.maintenance_queue.jobs
        assert len(jobs) == 1
        assert jobs[0].func_name == "app.plex_library.analyze_plex_media"
        assert jobs[0].args[0] == [path]
