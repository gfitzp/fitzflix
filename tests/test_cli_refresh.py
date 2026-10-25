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


def test_a_wrong_tmdb_id_is_an_error(app, monkeypatch):
    """Test a typo in the TMDB id (#344).

    A non-integer id is a usage error. An id that matches nothing says so
    and exits with an error. Neither queues anything."""

    from app import db

    with app.app_context():
        make_movie("Typo Movie", 1990, tmdb_id=12)
        db.session.commit()

    result, queued = _refreshed(app, monkeypatch, "movie", "12a")
    assert result.exit_code == 2
    assert queued == []

    result, queued = _refreshed(app, monkeypatch, "movie", "999999")
    assert result.exit_code == 1
    assert "No title in the library has TMDB id 999999" in result.output
    assert queued == []


def test_refresh_file_checks_its_id(app, monkeypatch):
    queued = []

    class FakeQueue:
        def enqueue(self, func, args=(), **kwargs):
            queued.append(args)

    monkeypatch.setattr(app, "file_queue", FakeQueue())
    if "refresh" not in app.cli.commands:
        from app.cli import register

        register(app)
    runner = app.test_cli_runner()

    result = runner.invoke(args=["refresh", "file", "abc"])
    assert result.exit_code == 2
    assert result.exception is None or isinstance(result.exception, SystemExit)

    result = runner.invoke(args=["refresh", "file", "424242"])
    assert result.exit_code == 1
    assert "No file has id 424242" in result.output
    assert queued == []


def test_refresh_criterion_runs_on_the_maintenance_queue(app, monkeypatch):
    """Test the queue and the timeout of the CLI Criterion refresh (#343)."""

    queued = []

    class FakeQueue:
        def __init__(self, name):
            self.name = name

        def enqueue(self, func, **kwargs):
            queued.append((self.name, func, kwargs.get("job_timeout")))

    monkeypatch.setattr(app, "sql_queue", FakeQueue("sql"))
    monkeypatch.setattr(app, "maintenance_queue", FakeQueue("maintenance"))
    if "refresh" not in app.cli.commands:
        from app.cli import register

        register(app)
    result = app.test_cli_runner().invoke(args=["refresh", "criterion"])
    assert result.exit_code == 0
    assert queued == [
        ("maintenance", "app.videos.refresh_criterion_collection_info", 3600)
    ]
