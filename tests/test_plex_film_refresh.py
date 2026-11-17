"""Test the Plex refresh of a film after the import of an extra (#775).

The scan of Plex reads a film folder again only when the list of that
folder changes. An extra that lands in an extras folder that exists
already changes no such list. A refresh of the film item makes Plex
read the extras folders again.
"""

import os

from rq.registry import ScheduledJobRegistry


class FakePlex:
    """Fake movie sections. Record the file filters and the refreshes."""

    def __init__(self, location, items):
        # The shape of items: [(ratingKey, [part file paths])]
        self.location = location
        self.items = items
        self.filters = []
        self.refreshed = []

    def get(self, path, params=None):
        if path == "/library/sections":
            return {
                "MediaContainer": {
                    "Directory": [
                        {
                            "key": "5",
                            "type": "movie",
                            "title": "Movies",
                            "Location": [{"path": self.location}],
                        },
                        {
                            "key": "4",
                            "type": "show",
                            "title": "TV Shows",
                            "Location": [{"path": self.location + "-tv"}],
                        },
                    ]
                }
            }
        assert path == "/library/sections/5/all", path
        self.filters.append(params)
        return {
            "MediaContainer": {
                "Metadata": [
                    {
                        "ratingKey": key,
                        "Media": [{"Part": [{"file": p} for p in parts]}],
                    }
                    for key, parts in self.items
                ]
            }
        }

    def command(self, method, path):
        assert method == "PUT", path
        assert path.endswith("/refresh"), path
        self.refreshed.append(path.split("/")[3])


def _wire(app, monkeypatch, fake):
    import app.plex_library as plex_library

    monkeypatch.setitem(app.config, "PLEX_URL", "http://plex.test")
    monkeypatch.setitem(app.config, "PLEX_TOKEN", "token")
    monkeypatch.setattr(plex_library, "plex_get", fake.get)
    monkeypatch.setattr(plex_library, "plex_command", fake.command)
    return plex_library


def _film(movies, folder):
    """Create a film folder with an extras folder. Return the folder path."""

    path = movies / folder
    (path / "Featurettes").mkdir(parents=True)
    return str(path)


def test_the_refresh_hits_only_the_film_of_the_folder(app, monkeypatch, tmp_path):
    movies = tmp_path / "Movies"
    film = _film(movies, "Holiday (1938)")
    edition = _film(movies, "Holiday (1938) {edition-1930 Version}")

    # The file filter is a text match. Thus, Plex also answers with the
    # edition, whose folder name starts with the same text.

    fake = FakePlex(
        str(movies),
        [
            ("10", [os.path.join(film, "Holiday (1938) - [Bluray-1080p].mkv")]),
            (
                "11",
                [os.path.join(edition, "Holiday (1938) {edition-1930 Version}.mkv")],
            ),
        ],
    )
    plex_library = _wire(app, monkeypatch, fake)

    assert plex_library.refresh_plex_film(film) is True
    assert fake.refreshed == ["10"]
    assert fake.filters == [{"type": 1, "file": "Holiday (1938)"}]


def test_a_film_that_plex_reaches_by_another_mount_is_refreshed(
    app, monkeypatch, tmp_path
):
    movies = tmp_path / "Movies"
    film = _film(movies, "Holiday (1938)")
    fake = FakePlex(
        str(movies),
        [("10", ["/data/Movies/Holiday (1938)/Holiday (1938) - [DVD].mkv"])],
    )
    plex_library = _wire(app, monkeypatch, fake)

    assert plex_library.refresh_plex_film(film) is True
    assert fake.refreshed == ["10"]


def test_a_folder_that_plex_has_no_film_for_gets_no_refresh(
    app, monkeypatch, tmp_path, caplog
):
    import logging

    movies = tmp_path / "Movies"
    film = _film(movies, "Holiday (1938)")
    fake = FakePlex(str(movies), [])
    plex_library = _wire(app, monkeypatch, fake)

    with caplog.at_level(logging.INFO):
        assert plex_library.refresh_plex_film(film) is True
    assert fake.refreshed == []
    assert any("no Plex film is in" in r.getMessage() for r in caplog.records)


def test_a_folder_that_is_not_readable_gets_no_refresh(app, monkeypatch, tmp_path):
    movies = tmp_path / "Movies"
    movies.mkdir()
    gone = str(movies / "Holiday (1938)")
    fake = FakePlex(str(movies), [("10", [os.path.join(gone, "Holiday.mkv")])])
    plex_library = _wire(app, monkeypatch, fake)

    assert plex_library.refresh_plex_film(gone) is True
    assert fake.refreshed == []
    assert fake.filters == []


def test_a_plex_fault_does_not_fail_the_task(app, monkeypatch, tmp_path):
    movies = tmp_path / "Movies"
    film = _film(movies, "Holiday (1938)")
    fake = FakePlex(str(movies), [])
    plex_library = _wire(app, monkeypatch, fake)

    def refuse(path, params=None):
        raise RuntimeError("simulated Plex fault")

    monkeypatch.setattr(plex_library, "plex_get", refuse)
    assert plex_library.refresh_plex_film(film) is True


def test_the_extras_of_one_disc_share_one_booked_refresh(app, monkeypatch, tmp_path):
    import app.plex_library as plex_library

    monkeypatch.setitem(app.config, "PLEX_URL", "http://plex.test")
    monkeypatch.setitem(app.config, "PLEX_TOKEN", "token")
    film = str(tmp_path / "Movies" / "Holiday (1938)")
    registry = ScheduledJobRegistry(queue=app.maintenance_queue)

    try:
        with app.app_context():
            assert plex_library.enqueue_plex_film_refresh(film) is True
            assert plex_library.enqueue_plex_film_refresh(film) is False

            job_ids = registry.get_job_ids()
            assert len(job_ids) == 1
            job = app.maintenance_queue.fetch_job(job_ids[0])
            assert job.func_name == "app.plex_library.refresh_plex_film"
            assert job.args == (film,)
            assert app.maintenance_queue.jobs == []
    finally:
        for job_id in registry.get_job_ids():
            registry.remove(job_id, delete_job=True)


def test_no_refresh_is_booked_without_a_plex_server(app, monkeypatch, tmp_path):
    import app.plex_library as plex_library

    monkeypatch.setitem(app.config, "PLEX_URL", None)
    monkeypatch.setitem(app.config, "PLEX_TOKEN", None)

    with app.app_context():
        assert plex_library.enqueue_plex_film_refresh(str(tmp_path)) is False
        assert ScheduledJobRegistry(queue=app.maintenance_queue).get_job_ids() == []
