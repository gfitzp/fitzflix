"""Test the scope of the `flask refresh tmdb` command.

Before the fix, a library with no TMDB id refreshed the whole library,
movies and TV. A refresh queues 1 TMDB job for each title."""

from tests.factories import make_movie, make_tv_series


def _refreshed(app, monkeypatch, *args):
    """Run the command with fake queue. Return the queued (library, tmdb id) pairs."""

    queued = []

    class FakeQueue:
        def enqueue(self, func, args=(), **kwargs):
            queued.append((args[0], args[2]))

    monkeypatch.setattr(app, "request_queue", FakeQueue())
    if "refresh" not in app.cli.commands:
        from app.cli import register

        register(app)
    result = app.test_cli_runner().invoke(args=["refresh", "tmdb", *args])
    return result, sorted(queued)


def test_the_refresh_command_keeps_to_its_library(app, monkeypatch):
    from app import db

    with app.app_context():
        make_movie("Refresh Movie", 1990, tmdb_id=9101)
        make_movie("Refresh Other", 1991, tmdb_id=9102)
        make_tv_series("Refresh Show", tmdb_id=9201)
        db.session.commit()

    _, queued = _refreshed(app, monkeypatch, "tv")
    assert [library for library, _ in queued] == ["TV Shows"]

    _, queued = _refreshed(app, monkeypatch, "movie")
    assert queued == [("Movies", 9101), ("Movies", 9102)]

    _, queued = _refreshed(app, monkeypatch, "movie", "9102")
    assert queued == [("Movies", 9102)]

    _, queued = _refreshed(app, monkeypatch)
    assert len(queued) == 3

    result, queued = _refreshed(app, monkeypatch, "films")
    assert result.exit_code != 0
    assert queued == []
