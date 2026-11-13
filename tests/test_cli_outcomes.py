"""Test that the CLI commands report the true outcome (#409, #410).

A command that cannot do its work says why and exits with an error. It
does not print a success line and leave the refusal to a worker log."""

import json

from datetime import date

from tests.factories import make_movie, make_movie_file, make_tv_series
from tests.fakes import RecordingQueue


def _runner(app):
    if "tv" not in app.cli.commands:
        from app.cli import register

        register(app)
    return app.test_cli_runner()


def test_a_tmdb_id_of_0_refreshes_nothing(app, monkeypatch):
    """An id of 0 is an error. It does not refresh the whole library (#409)."""

    from app import db

    queue = RecordingQueue()
    monkeypatch.setattr(app, "request_queue", queue)
    with app.app_context():
        make_movie("Zero Movie", 1990, tmdb_id=9301)
        db.session.commit()

    result = _runner(app).invoke(args=["refresh", "tmdb", "movie", "0"])
    assert result.exit_code == 1
    assert "A TMDB id is 1 or more" in result.output
    assert queue.calls == []


def test_tv_rename_refuses_before_it_queues(app):
    """The rename command runs the cheap checks of the task first (#410)."""

    from app import db

    def renames():
        return [
            job
            for job in app.file_queue.jobs
            if job.func_name == "app.series_rename.rename_tv_series_task"
        ]

    with app.app_context():
        series = make_tv_series("Rename Cli Show")
        make_tv_series("Rename Cli Taken")
        db.session.commit()
        series_id = series.id

    runner = _runner(app)
    for title, reason in (
        ("Rename Cli Show", "the title does not change"),
        ("Rename Cli: Show?", "is not safe as a folder name"),
        ("Rename Cli Taken", "already exists"),
    ):
        result = runner.invoke(args=["tv", "rename", str(series_id), title])
        assert result.exit_code == 1, title
        assert reason in result.output
    assert renames() == []

    result = runner.invoke(args=["tv", "rename", str(series_id), "Rename Cli New"])
    assert result.exit_code == 0
    assert "Enqueued series rename" in result.output
    assert len(renames()) == 1


def test_recs_evaluate_refuses_a_bad_weight(app):
    """A weight pair that is not a number is a usage error, not a traceback (#410)."""

    runner = _runner(app)
    for weights in ("genre", "genre=abc"):
        result = runner.invoke(args=["recs", "evaluate", "--weights", weights])
        assert result.exit_code == 1, weights
        assert "must be a number" in result.output


def test_audio_atmos_says_why_it_skips(app):
    """The Atmos command names the real reason for a skip (#410)."""

    from app import db
    from app.atmos import EAC3_ATMOS_CODEC, TRUEHD_ATMOS_CODEC
    from app.models import FileAudioTrack

    with app.app_context():
        plain = make_movie_file(make_movie("Atmos Cli Plain", 2001), "Bluray-1080p")
        twinned = make_movie_file(make_movie("Atmos Cli Twinned", 2002), "Bluray-1080p")
        db.session.add(
            FileAudioTrack(
                file_id=plain.id,
                track=1,
                codec="DTS-HD MA",
                language="eng",
                language_name="English",
            )
        )
        for track, codec in ((1, EAC3_ATMOS_CODEC), (2, TRUEHD_ATMOS_CODEC)):
            db.session.add(
                FileAudioTrack(
                    file_id=twinned.id,
                    track=track,
                    codec=codec,
                    language="eng",
                    language_name="English",
                )
            )
        db.session.commit()
        plain_id, twinned_id = plain.id, twinned.id

    runner = _runner(app)
    result = runner.invoke(args=["audio", "atmos", str(plain_id)])
    assert "(no TrueHD Atmos track)" in result.output
    result = runner.invoke(args=["audio", "atmos", str(twinned_id)])
    assert "(twin present)" in result.output


def _plant_current_leaving_set(app):
    """Store a leaving set that is current and complete. Return its JSON."""

    from app.leaving_criterion import LEAVING_KEY

    planted = json.dumps(
        {
            "departs": date.today().isoformat(),
            "source": "https://example.test/leaving",
            "items": [{"title": "Kept Film", "tmdb_id": None}],
        }
    )
    app.redis.set(LEAVING_KEY, planted)
    return planted


def test_recs_leaving_says_that_it_kept_a_current_set(app, monkeypatch):
    """A current stored set is kept, and the command says so (#770)."""

    from app.leaving_criterion import LEAVING_KEY

    planted = _plant_current_leaving_set(app)
    fetches = []
    monkeypatch.setattr(
        "app.leaving_criterion.fetch_leaving_films",
        lambda: fetches.append(1) or (None, None, []),
    )

    result = _runner(app).invoke(args=["recs", "leaving"])
    assert result.exit_code == 0, result.output
    assert "Kept the stored set of 1 film(s)" in result.output
    assert "Nothing was fetched" in result.output
    assert "refreshed" not in result.output
    assert fetches == []
    assert app.redis.get(LEAVING_KEY).decode() == planted


def test_recs_leaving_force_fetches_a_current_set(app, monkeypatch):
    """--force fetches the page also when the stored set is current (#770)."""

    from app.leaving_criterion import LEAVING_KEY

    _plant_current_leaving_set(app)
    departs = date.today()
    monkeypatch.setattr(
        "app.leaving_criterion.fetch_leaving_films",
        lambda: (
            departs,
            "https://example.test/leaving",
            [
                {"title": "New Film", "year": 1960, "director": None},
                {"title": "Other Film", "year": 1961, "director": None},
            ],
        ),
    )
    monkeypatch.setattr("app.leaving_criterion.match_tmdb_id", lambda *a, **k: None)

    result = _runner(app).invoke(args=["recs", "leaving", "--force"])
    assert result.exit_code == 0, result.output
    assert (
        f"Stored 2 film(s) that depart on {departs.isoformat()} "
        "from https://example.test/leaving"
    ) in result.output
    stored = json.loads(app.redis.get(LEAVING_KEY))
    assert [item["title"] for item in stored["items"]] == ["New Film", "Other Film"]


def test_recs_leaving_says_that_the_page_had_no_films(app, monkeypatch):
    """A page with no films keeps the stored set, and the command says so (#770)."""

    monkeypatch.setattr(
        "app.leaving_criterion.fetch_leaving_films",
        lambda: (date.today(), "https://example.test/leaving", []),
    )

    result = _runner(app).invoke(args=["recs", "leaving"])
    assert result.exit_code == 0, result.output
    assert "Found no films on https://example.test/leaving" in result.output
    assert "Kept the stored set" in result.output


def test_recs_newly_added_counts_the_feeds(app, monkeypatch):
    """The command prints the count of the read and the failed feeds (#770).

    When each feed failed, it exits with code 1."""

    films = []
    monkeypatch.setattr("app.newly_added.fetch_collection_films", lambda url: films)
    monkeypatch.setattr("app.newly_added.match_tmdb_id", lambda *a, **k: None)

    runner = _runner(app)
    result = runner.invoke(args=["recs", "newly-added"])
    assert result.exit_code == 1, result.output
    assert "Read 0 newly-added feed(s). 1 failed." in result.output
    assert "Each feed failed" in result.output

    films.append({"title": "New Arrival", "year": 1960, "director": None})
    result = runner.invoke(args=["recs", "newly-added"])
    assert result.exit_code == 0, result.output
    assert "Read 1 newly-added feed(s). 0 failed." in result.output
