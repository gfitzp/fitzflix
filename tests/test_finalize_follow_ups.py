"""Pin the follow-ups of finalize_localization that its phase split moved.

The phase split of tranche 3 cut finalize_localization into the record
build, the file placement, the commit, and the follow-ups after the
commit. Each test here passed against the old function.
"""

import os

import pytest


import app.videos as videos

from app.models import File
from app.videos import finalize_localization, localization_task

from tests.localization_helpers import run_full_chain, separate_library_volume

# The pins of the finalize_localization follow-ups that the phase split
# moves (tranche 3). Each test passed against the old function.


def _roster_jobs(app, func_name):
    """Return the queued jobs of func_name on every queue of the roster."""

    from app.queues import roster_queues

    return [
        job
        for queue in roster_queues(app)
        for job in queue.jobs
        if job.func_name == func_name
    ]


def _capture_emails(monkeypatch):
    """Record each admin email and each direct email of the importing module."""

    from app import importing

    sent = []
    monkeypatch.setattr(
        importing,
        "notify_admin",
        lambda subject, template, **kwargs: sent.append(("notify", template)),
    )

    def admin_email(subject, template, **kwargs):
        return lambda: sent.append(("admin", template))

    monkeypatch.setattr(importing, "admin_email", admin_email)
    monkeypatch.setattr(
        importing,
        "send_email",
        lambda subject, **kwargs: sent.append(("todo", subject)),
    )
    return sent


def _import(app, sample_mkv, directory, basename):
    """Write a copy of the sample under basename and run the full chain."""

    source = os.path.join(directory, basename)
    os.makedirs(directory, exist_ok=True)
    with open(sample_mkv, "rb") as f_in, open(source, "wb") as f_out:
        f_out.write(f_in.read())
    run_full_chain(app, source)
    for job in app.handoff_queue.jobs + app.sql_queue.jobs:
        job.delete()
    return source


def test_a_physical_replacement_runs_every_follow_up(
    app, sample_mkv, incoming_dir, monkeypatch
):
    """A Blu-ray replaces a DVD: delete it, send 2 emails, and queue the TMDB refresh."""

    monkeypatch.setitem(app.config, "TODO_EMAIL", "todo@example.test")
    sent = _capture_emails(monkeypatch)
    movies = os.path.join(app.config["LIBRARY_DIR"], "Movies/Replacement Pin (2021)")
    dvd = os.path.join(movies, "Replacement Pin (2021) - [DVD].mkv")
    bluray = os.path.join(movies, "Replacement Pin (2021) - [Bluray-1080p].mkv")
    with app.app_context():
        _import(app, sample_mkv, incoming_dir, os.path.basename(dvd))
        assert os.path.exists(dvd)
        sent.clear()
        source = _import(app, sample_mkv, incoming_dir, os.path.basename(bluray))

        assert not os.path.exists(dvd)
        assert os.path.exists(bluray)
        assert not os.path.exists(source)
        assert [f.basename for f in File.query.all()] == [os.path.basename(bluray)]
        assert ("admin", "replaced_physical_media") in sent
        assert (
            "todo",
            "Find and dispose of the media for 'Replacement Pin (2021) - [DVD].mkv'",
        ) in sent
        # The DVD import queued the first refresh, and the Blu-ray the second.
        refreshes = _roster_jobs(app, "app.videos.refresh_tmdb_info")
        movie_id = File.query.one().movie_id
        assert [job.args[:2] for job in refreshes] == [("Movies", movie_id)] * 2
        assert refreshes[1].kwargs == {"notify_if_missing": True}
        lock = app.lock_manager.lock(File.query.one().file_identifier(), 1000)
        assert lock
        app.lock_manager.unlock(lock)
    os.remove(bluray)


def test_a_tv_import_creates_its_series_and_queues_its_refresh(
    app, sample_mkv, incoming_dir
):
    """An episode gets a TVSeries row and a TV Shows TMDB refresh."""

    from app.models import TVSeries

    basename = "Pin Show - S01E01 - [DVD].mkv"
    with app.app_context():
        _import(app, sample_mkv, incoming_dir, basename)
        file = File.query.filter_by(basename=basename).one()
        assert file.tv_series.title == "Pin Show"
        assert TVSeries.query.count() == 1
        refreshes = _roster_jobs(app, "app.videos.refresh_tmdb_info")
        assert [job.args[:2] for job in refreshes] == [("TV Shows", file.series_id)]
        output = os.path.join(app.config["LIBRARY_DIR"], file.file_path)
    assert os.path.exists(output)
    os.remove(output)


def test_finalize_gives_back_an_output_that_is_on_another_volume(
    app, sample_mkv, incoming_dir, monkeypatch
):
    """The output is not on the library volume. The library copy is queued again.

    The records roll back, and the title lock stays for the new chain."""

    basename = "Wrong Volume (2021) - [DVD].mkv"
    source = os.path.join(incoming_dir, basename)
    with open(sample_mkv, "rb") as f_in, open(source, "wb") as f_out:
        f_out.write(f_in.read())
    with app.app_context():
        assert localization_task(source) is True
        (move,) = [
            job
            for job in app.handoff_queue.jobs
            if job.func_name == "app.videos.move_localized_file"
        ]
        move.delete()
        source_path, details, lock, hidden = move.args
        inspection = videos.inspect_localized_file(hidden, "Matroska")
        separate_library_volume(app, monkeypatch)
        assert (
            finalize_localization(source_path, details, lock, hidden, inspection)
            is False
        )
        (again,) = [
            job
            for job in app.handoff_queue.jobs
            if job.func_name == "app.videos.move_localized_file"
        ]
        assert again.args[0] == source
        assert again.args[3] == hidden
        assert File.query.count() == 0
        assert app.redis.get(lock[1]) is not None
        app.lock_manager.unlock(lock)
        again.delete()
    os.remove(hidden)
    os.remove(source)


def test_foreign_and_lossy_audio_send_their_emails(
    app, sample_mkv, incoming_dir, monkeypatch
):
    """A foreign first track with a second track emails, and so does a lossy lead."""

    import copy

    sent = _capture_emails(monkeypatch)
    basename = "Audio Mails (2021) - [DVD].mkv"
    source = os.path.join(incoming_dir, basename)
    with open(sample_mkv, "rb") as f_in, open(source, "wb") as f_out:
        f_out.write(f_in.read())
    with app.app_context():
        assert localization_task(source) is True
        (move,) = list(app.handoff_queue.jobs)
        assert videos.move_localized_file(*move.args) is True
        (finalize,) = list(app.sql_queue.jobs)
        args = list(finalize.args)
        inspection = copy.deepcopy(args[4])
        lead = inspection["audio_tracks"][0]
        lead["language"] = "fre"
        lead["compression_mode"] = "Lossy"
        second = dict(lead, language="eng", compression_mode="Lossless")
        inspection["audio_tracks"].append(second)
        args[4] = inspection
        finalize_localization(*args, **finalize.kwargs)
        assert ("notify", "possibly_foreign_audio") in sent
        assert ("notify", "lossy_audio") in sent
        output = os.path.join(
            app.config["LIBRARY_DIR"],
            File.query.filter_by(basename=basename).one().file_path,
        )
    os.remove(output)


def test_an_in_place_import_keeps_the_file_that_it_made(app, sample_mkv):
    """The source is at its library path. The finished file replaces it there."""

    directory = os.path.join(app.config["LIBRARY_DIR"], "Movies/In Place Pin (2021)")
    basename = "In Place Pin (2021) - [DVD].mkv"
    with app.app_context():
        source = _import(app, sample_mkv, directory, basename)
        assert os.path.exists(source)
        assert File.query.filter_by(basename=basename).one()
    assert os.listdir(directory) == [basename]
    os.remove(source)


def test_a_failed_follow_up_does_not_skip_the_next_ones(
    app, sample_mkv, incoming_dir, monkeypatch
):
    """Each follow-up after the commit is best effort on its own.

    Before the phase split, the first failure skipped every later step:
    here the source removal and the TMDB refresh."""

    import app.atmos as atmos

    def broken(*args, **kwargs):
        raise RuntimeError("Redis is not reachable")

    monkeypatch.setattr(atmos, "staged_handoff", broken)
    basename = "Later Steps (2021) - [DVD].mkv"
    with app.app_context():
        source = _import(app, sample_mkv, incoming_dir, basename)
        file = File.query.filter_by(basename=basename).one()
        output = os.path.join(app.config["LIBRARY_DIR"], file.file_path)
        assert [
            job.args[:2] for job in _roster_jobs(app, "app.videos.refresh_tmdb_info")
        ] == [("Movies", file.movie_id)]
        lock = app.lock_manager.lock(file.file_identifier(), 1000)
        assert lock
        app.lock_manager.unlock(lock)
    assert not os.path.exists(source)
    assert os.listdir(app.config["STAGING_DIR"]) == []
    os.remove(output)


@pytest.mark.parametrize(
    "tracks, expected",
    [
        ([], (False, False)),
        ([{"language": "eng", "compression_mode": "Lossy"}], (False, False)),
        ([{"language": "und"}, {"language": "fre"}], (False, False)),
        ([{"language": "fre", "compression_mode": "Lossless"}], (True, False)),
        (
            [
                {"language": "eng", "compression_mode": "Lossy"},
                {"language": "eng", "compression_mode": "Lossless"},
            ],
            (False, True),
        ),
    ],
)
def test_audio_alerts(app, tracks, expected):
    """The foreign-audio and lossy-lead flags of the import emails are pure."""

    from app.importing import _audio_alerts

    assert _audio_alerts(tracks, "eng") == expected


def test_a_failed_radarr_report_does_not_skip_the_tmdb_refresh(
    app, sample_mkv, incoming_dir, monkeypatch
):
    """The movie_id read of the Radarr report runs inside its own guarded step."""

    from app import importing

    def broken(*args, **kwargs):
        raise RuntimeError("Radarr is away")

    monkeypatch.setattr(importing, "_report_import_move_to_radarr", broken)
    basename = "Radarr Away (2021) - [DVD].mkv"
    with app.app_context():
        _import(app, sample_mkv, incoming_dir, basename)
        file = File.query.filter_by(basename=basename).one()
        output = os.path.join(app.config["LIBRARY_DIR"], file.file_path)
        assert _roster_jobs(app, "app.videos.refresh_tmdb_info")
    assert os.listdir(app.config["STAGING_DIR"]) == []
    os.remove(output)
