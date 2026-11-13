"""Pin the follow-ups of finalize_localization that its phase split moved.

The phase split of tranche 3 cut finalize_localization into the record
build, the file placement, the commit, and the follow-ups after the
commit. Each test here passed against the old function.
"""

import os
import shutil

import pytest


import app.videos as videos
from app import importing

from app.models import File
from app.videos import finalize_localization, localization_task

from tests.localization_helpers import run_full_chain, separate_library_volume

# The pins of the finalize_localization follow-ups that the phase split
# moves (tranche 3). Each test passed against the old function.

# The codec names of MediaInfo that the audio alerts read.

AC3 = "Dolby Digital"
DDP = "Dolby Digital Plus"
DDP_ATMOS = "Dolby Digital Plus with Dolby Atmos"
TRUEHD_ATMOS = "Dolby TrueHD with Dolby Atmos"
FLAC = "FLAC"


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
        inspection = importing.inspect_localized_file(hidden, "Matroska")
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

    from app import importing

    def broken(*args, **kwargs):
        raise RuntimeError("Redis is not reachable")

    monkeypatch.setattr(importing, "_delete_worse_copies", broken)
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
        ([], (False, False, False)),
        ([{"language": "eng", "compression_mode": "Lossy"}], (False, False, False)),
        ([{"language": "und"}, {"language": "fre"}], (False, False, False)),
        ([{"language": "fre", "compression_mode": "Lossless"}], (True, False, False)),
        (
            [
                {"language": "eng", "compression_mode": "Lossy"},
                {"language": "eng", "compression_mode": "Lossless"},
            ],
            (False, True, False),
        ),
        (
            [
                {"language": "eng", "compression_mode": "Lossy", "codec": AC3},
                {"language": "eng", "compression_mode": "Lossy", "codec": DDP_ATMOS},
            ],
            (False, False, True),
        ),
    ],
)
def test_audio_alerts(app, tracks, expected):
    """The foreign-audio, lossy-lead and Atmos-order flags of the import emails are pure."""

    from app.importing import _audio_alerts

    assert _audio_alerts(tracks, "eng") == expected


def _audio(codec, language="eng"):
    """Return a track dict with the fields that the Atmos rules read."""

    return {
        "language": language,
        "language_name": {"eng": "English", "fre": "French"}[language],
        "codec": codec,
        "format": "FLAC" if codec == FLAC else None,
        "channels": "5.1",
        "default": False,
        "compression_mode": "Lossless" if codec in (FLAC, TRUEHD_ATMOS) else "Lossy",
    }


@pytest.mark.parametrize(
    "codecs, expected",
    [
        # No Atmos track of this type.
        ([AC3, DDP], False),
        ([FLAC, TRUEHD_ATMOS], False),
        # The Atmos track leads.
        ([DDP_ATMOS], False),
        ([DDP_ATMOS, AC3], False),
        # The Atmos track is behind a lossy track.
        ([AC3, DDP_ATMOS], True),
        # The shape of file 34161: the Atmos track is behind the lossless pair.
        ([FLAC, TRUEHD_ATMOS, DDP_ATMOS], True),
        # The 3-track set of the Atmos pipeline.
        ([DDP_ATMOS, FLAC, TRUEHD_ATMOS], False),
        # The pipeline put the twin before a source that was not first.
        ([FLAC, DDP_ATMOS, FLAC, TRUEHD_ATMOS], False),
        ([AC3, DDP_ATMOS, TRUEHD_ATMOS], False),
        # A placed twin does not excuse a second Atmos track.
        ([AC3, DDP_ATMOS, TRUEHD_ATMOS, DDP_ATMOS], True),
        # The twin position needs the language of the source.
        ([AC3, (DDP_ATMOS, "fre"), TRUEHD_ATMOS], True),
        ([AC3, (DDP_ATMOS, "fre"), (FLAC, "fre"), (TRUEHD_ATMOS, "fre")], False),
    ],
)
def test_atmos_not_lead(app, codecs, expected):
    """A Dolby Digital Plus Atmos track behind the first track is flagged.

    The position that the Atmos pipeline gives a twin is not flagged."""

    from app.importing import _audio_alerts

    tracks = [
        _audio(*codec) if isinstance(codec, tuple) else _audio(codec)
        for codec in codecs
    ]
    assert _audio_alerts(tracks, "eng")[2] is expected


def _finalize_with_audio(app, sample_mkv, incoming_dir, basename, tracks):
    """Import a sample and finalize it with the given audio tracks. Return the output path."""

    import copy

    source = os.path.join(incoming_dir, basename)
    with open(sample_mkv, "rb") as f_in, open(source, "wb") as f_out:
        f_out.write(f_in.read())
    assert localization_task(source) is True
    (move,) = list(app.handoff_queue.jobs)
    assert videos.move_localized_file(*move.args) is True
    (finalize,) = list(app.sql_queue.jobs)
    args = list(finalize.args)
    inspection = copy.deepcopy(args[4])
    lead = inspection["audio_tracks"][0]
    inspection["audio_tracks"] = [dict(lead, **track) for track in tracks]
    args[4] = inspection
    finalize_localization(*args, **finalize.kwargs)
    return os.path.join(
        app.config["LIBRARY_DIR"],
        File.query.filter_by(basename=basename).one().file_path,
    )


def test_an_atmos_track_behind_the_lead_sends_its_email(
    app, sample_mkv, incoming_dir, monkeypatch
):
    """The email names the file, links its page and lists the tracks in order."""

    from flask import render_template

    from app import importing

    sent = []
    monkeypatch.setattr(
        importing,
        "notify_admin",
        lambda subject, template, **context: sent.append((template, context)),
    )
    basename = "Atmos Order (2021) - [WEBDL-1080p].mkv"
    tracks = [_audio(FLAC), _audio(TRUEHD_ATMOS), _audio(DDP_ATMOS)]
    with app.app_context():
        output = _finalize_with_audio(app, sample_mkv, incoming_dir, basename, tracks)
        ((template, context),) = sent
        assert template == "ddp_atmos_not_first"
        file_id = File.query.filter_by(basename=basename).one().id
        with app.test_request_context(base_url="http://fitzflix.test/"):
            text = render_template(f"email/{template}.txt", **context)
            html = render_template(f"email/{template}.html", **context)
    link = f"http://fitzflix.test/file/{file_id}"
    for body in (text, html):
        assert basename in body
        assert link in body
        assert (
            body.index(FLAC) < body.index(TRUEHD_ATMOS) < body.index(DDP_ATMOS + ", ")
        )
    assert (
        "\n1. English, FLAC, 5.1 channels"
        "\n2. English, Dolby TrueHD with Dolby Atmos, 5.1 channels"
        "\n3. English, Dolby Digital Plus with Dolby Atmos, 5.1 channels\n\nGo here"
    ) in text
    assert "<li>English, FLAC, 5.1 channels</li>" in html
    os.remove(output)


@pytest.mark.parametrize("queued, mailed", [(True, False), (False, True)])
def test_a_coming_atmos_lead_sends_no_atmos_order_email(
    app, sample_mkv, incoming_dir, monkeypatch, queued, mailed
):
    """The queued Atmos job puts its twin first. Without the job, the email goes out."""

    from app import importing

    sent = _capture_emails(monkeypatch)
    monkeypatch.setattr(importing, "_atmos_handoff", lambda *args: None)
    monkeypatch.setattr(importing, "_enqueue_atmos_twin", lambda *args: queued)
    basename = f"Atmos Coming {int(queued)} (2021) - [Bluray-1080p].mkv"
    tracks = [_audio(FLAC), _audio(TRUEHD_ATMOS), _audio(DDP_ATMOS, "fre")]
    with app.app_context():
        output = _finalize_with_audio(app, sample_mkv, incoming_dir, basename, tracks)
        assert (("notify", "ddp_atmos_not_first") in sent) is mailed
    os.remove(output)


def test_an_atmos_twin_behind_another_track_does_not_lead(app):
    """A twin leads only if the insertion point of a source that wants it is 0."""

    from app.importing import _atmos_twin_will_lead

    assert _atmos_twin_will_lead([_audio(FLAC), _audio(TRUEHD_ATMOS)])
    assert _atmos_twin_will_lead([_audio(TRUEHD_ATMOS), _audio(AC3)])
    assert not _atmos_twin_will_lead([_audio(AC3), _audio(FLAC), _audio(TRUEHD_ATMOS)])
    assert not _atmos_twin_will_lead(
        [_audio(FLAC), _audio(TRUEHD_ATMOS), _audio(DDP_ATMOS)]
    )


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


def _booked_analyzes(app):
    """Return {path: scheduled time} of each booked Plex analyze."""

    from rq.registry import ScheduledJobRegistry

    registry = ScheduledJobRegistry(queue=app.maintenance_queue)
    booked = {}
    for job_id in registry.get_job_ids():
        job = app.maintenance_queue.fetch_job(job_id)
        if job and job.func_name == "app.plex_library.analyze_plex_media":
            booked[job.args[0][0]] = registry.get_scheduled_time(job_id)
    return booked


@pytest.mark.parametrize("second", ["upgrade", "same name"])
def test_a_replacement_import_books_the_plex_analyze_after_the_scan(
    app, sample_mkv, incoming_dir, monkeypatch, second
):
    """A new part of an item that Plex has gets no webhook (#661).

    The analyze after the next scan finds the item by its path and
    aligns the stream selection. A first import books nothing. The
    library.new webhook covers it."""

    from datetime import datetime, timedelta, timezone

    from rq.registry import ScheduledJobRegistry

    monkeypatch.setitem(app.config, "PLEX_URL", "http://plex.test:32400")
    monkeypatch.setitem(app.config, "PLEX_TOKEN", "test-token")
    _capture_emails(monkeypatch)
    title = f"Plex Part {second.title()}"
    movies = os.path.join(app.config["LIBRARY_DIR"], f"Movies/{title} (2021)")
    dvd = os.path.join(movies, f"{title} (2021) - [DVD].mkv")
    bluray = os.path.join(movies, f"{title} (2021) - [Bluray-1080p].mkv")
    output = bluray if second == "upgrade" else dvd
    registry = ScheduledJobRegistry(queue=app.maintenance_queue)
    try:
        with app.app_context():
            _import(app, sample_mkv, incoming_dir, os.path.basename(dvd))
            assert _booked_analyzes(app) == {}
            _import(app, sample_mkv, incoming_dir, os.path.basename(output))

            booked = _booked_analyzes(app)
            assert list(booked) == [output]
            due = booked[output].replace(tzinfo=timezone.utc)
            assert due > datetime.now(timezone.utc) + timedelta(minutes=15)
            assert not [
                job
                for job in app.maintenance_queue.jobs
                if job.func_name == "app.plex_library.analyze_plex_media"
            ]
    finally:
        for job_id in registry.get_job_ids():
            registry.remove(job_id, delete_job=True)
        for path in (dvd, bluray):
            if os.path.exists(path):
                os.remove(path)


def test_a_failed_booking_of_the_plex_analyze_keeps_the_import(
    app, sample_mkv, incoming_dir, monkeypatch, caplog
):
    """The booking never fails the import (#661)."""

    import logging

    from app import plex_library

    monkeypatch.setitem(app.config, "PLEX_URL", "http://plex.test:32400")
    monkeypatch.setitem(app.config, "PLEX_TOKEN", "test-token")
    _capture_emails(monkeypatch)
    movies = os.path.join(app.config["LIBRARY_DIR"], "Movies/Plex Fault (2021)")
    dvd = os.path.join(movies, "Plex Fault (2021) - [DVD].mkv")

    def refuse(*args, **kwargs):
        raise RuntimeError("simulated Redis fault")

    try:
        with app.app_context():
            _import(app, sample_mkv, incoming_dir, os.path.basename(dvd))
            monkeypatch.setattr(plex_library, "enqueue_task", refuse)
            with caplog.at_level(logging.WARNING):
                source = _import(app, sample_mkv, incoming_dir, os.path.basename(dvd))
            assert os.path.exists(dvd) and not os.path.exists(source)
            assert File.query.filter_by(basename=os.path.basename(dvd)).one()
            assert any(
                "simulated Redis fault" in record.getMessage()
                and record.levelno == logging.WARNING
                for record in caplog.records
            )
    finally:
        if os.path.exists(dvd):
            os.remove(dvd)


class _Row:
    """A committed File row of an import. A listed column fails like an outage."""

    def __init__(self, failing=(), **columns):
        object.__setattr__(self, "_failing", set(failing))
        self.__dict__.update(
            id=7,
            basename="Follow Up (2021) - [DVD].mkv",
            aws_key=None,
            movie_id=None,
            converted_from_quality=None,
        )
        self.__dict__.update(columns)

    def __getattribute__(self, name):
        if name != "_failing" and name in object.__getattribute__(self, "_failing"):
            from pymysql import err as mysql_err
            from sqlalchemy import exc as sa_exc

            raise sa_exc.OperationalError(
                "SELECT", {}, mysql_err.OperationalError(2013, "Lost connection")
            )
        return object.__getattribute__(self, name)


def _record_steps(monkeypatch):
    """Record the source steps and the arr record step of _after_commit."""

    from app import importing

    calls = []
    monkeypatch.setattr(
        importing, "_remove_source", lambda path: calls.append(("removal", path))
    )
    monkeypatch.setattr(
        importing,
        "move_to_rejects",
        lambda path, reason="": calls.append(("hold", path, reason)),
    )
    monkeypatch.setattr(
        importing,
        "_restore_arr_record",
        lambda path, output: calls.append(("arr record", path)),
    )
    return calls


def test_a_failed_first_read_after_the_commit_stops_no_later_follow_up(
    app, tmp_path, monkeypatch
):
    """The read of the archive key is a step. The later steps run (#689).

    The state of the archive is not known. Thus, the source goes to
    rejects/unarchived, as a missing pending upload does."""

    from app import importing

    calls = _record_steps(monkeypatch)
    source = str(tmp_path / "import" / "Follow Up.mkv")
    output = str(tmp_path / "Movies" / "Follow Up (2021) - [DVD].mkv")
    with app.app_context():
        assert (
            importing._after_commit(
                _Row(failing={"aws_key"}),
                source,
                {"media_library": "Movies"},
                output,
                str(tmp_path / "Movies"),
                None,
                [],
                [],
                [],
                [],
            )
            is False
        )
    assert calls == [("hold", source, "unarchived"), ("arr record", source)]


def test_the_time_limit_of_rq_in_a_follow_up_ends_the_cataloging(
    app, tmp_path, monkeypatch
):
    """The time limit passes the handler of the caller (#376, #689). The lock goes."""

    from rq.timeouts import JobTimeoutException

    from app import importing

    def hang(path, output):
        raise JobTimeoutException("Task exceeded maximum timeout value")

    name = "Time Limit (2021) - [DVD].mkv"
    details = {
        "basename": name,
        "dirname": "Movies/Time Limit (2021)",
        "file_path": f"Movies/Time Limit (2021)/{name}",
        "media_library": "Movies",
    }
    monkeypatch.setitem(app.config, "LIBRARY_DIR", str(tmp_path))
    monkeypatch.setattr(
        importing, "_build_records", lambda *a, **k: (_Row(), [], [], [], [])
    )
    monkeypatch.setattr(importing, "_place_file", lambda *a, **k: "renamed")
    monkeypatch.setattr(importing, "_commit_records", lambda *a, **k: None)
    monkeypatch.setattr(importing, "_remove_source", lambda path: None)
    monkeypatch.setattr(importing, "_restore_arr_record", hang)
    removed = []
    monkeypatch.setattr(
        importing, "_enqueue_tmdb_enrichment", lambda *a: removed.append("tmdb")
    )
    with app.app_context():
        lock = app.lock_manager.lock('{"title": "Time Limit"}', 60000)
        with pytest.raises(JobTimeoutException):
            importing.finalize_localization(
                str(tmp_path / "import" / name),
                details,
                lock,
                str(tmp_path / f".{name}"),
                {"audio_tracks": []},
            )
    assert app.redis.get('{"title": "Time Limit"}') is None
    assert removed == []


def test_a_failed_move_request_keeps_the_source_and_the_replaced_archive(
    app, tmp_path, monkeypatch
):
    """The row stays on its pending key. Nothing that it needs goes (#690)."""

    from app import importing

    calls = _record_steps(monkeypatch)
    monkeypatch.setattr(importing, "ensure_pending_upload", lambda file, path: True)

    def refuse(file):
        raise RuntimeError("simulated commit fault")

    monkeypatch.setattr(importing, "_request_final_archive_key", refuse)
    monkeypatch.setattr(
        importing,
        "_delete_replaced_archive",
        lambda key: calls.append(("archive delete", key)),
    )
    source = str(tmp_path / "import" / "Follow Up.mkv")
    output = str(tmp_path / "Movies" / "Follow Up (2021) - [DVD].mkv")
    row = _Row(aws_key="pending-untouched/run2/Follow Up.mkv")
    with app.app_context():
        importing._after_commit(
            row,
            source,
            {"media_library": "Movies"},
            output,
            str(tmp_path / "Movies"),
            None,
            [],
            ["untouched/Follow Up.mkv"],
            [],
            [],
        )
    assert calls == [("hold", source, "unarchived"), ("arr record", source)]


def _respelled_import(app, monkeypatch, title):
    """Build the records of an import whose path differs from its record by letter case.

    MariaDB finds the record of the other spelling. The test database
    does not. Thus, the lookup is given that record directly. Return
    (file id, old path, details, worse local copies)."""

    from app import db, importing
    from tests.factories import make_movie, make_movie_file

    file = make_movie_file(make_movie(title, 1962), "DVD")
    db.session.commit()
    old_path = os.path.join(app.config["LIBRARY_DIR"], file.file_path)
    new_basename = file.basename.replace(title, title.upper())
    details = {
        "file_path": os.path.join(file.dirname, new_basename),
        "dirname": file.dirname,
        "basename": new_basename,
        "plex_title": f"{title.upper()} (1962)",
        "edition": None,
        "media_library": "Movies",
        "title": title,
        "year": 1962,
        "quality_title": "DVD",
        "untouched_basename": new_basename,
    }
    inspection = {
        "video": {},
        "audio_tracks": [],
        "subtitle_tracks": [],
        "filesize_bytes": 5,
    }
    monkeypatch.setattr(importing, "_record_at_path", lambda path: file)
    records = importing._build_records(details, inspection, new_basename)
    db.session.commit()
    return file.id, old_path, details, records[1]


def test_a_record_with_an_other_letter_case_takes_the_path_of_the_new_file(
    app, monkeypatch
):
    """The path lookup of MariaDB ignores letter case, and the shares do not (#739).

    The record got the tracks and the archive of the new file and kept
    its old path. That path named no file. The record must follow the
    file, and the file at the old spelling is the replaced file."""

    from app import db
    from app.models import File

    with app.app_context():
        file_id, old_path, details, worse = _respelled_import(
            app, monkeypatch, "Mixed Case Film"
        )
        row = db.session.get(File, file_id)
        assert row.file_path == details["file_path"]
        assert row.basename == details["basename"]
        assert row.plex_title == details["plex_title"]
        assert worse == [(old_path, None)]


def test_a_file_at_the_old_spelling_goes_to_the_rejects(app, monkeypatch, tmp_path):
    """The file at the old spelling can be a different extra of the film (#739).

    "Trailer.mkv" and "trailer.mkv" are 2 files on the shares, and only
    1 can have a record. Thus, the file is never deleted. It goes to
    the rejects. A path that names the new file itself is not touched.
    A volume that ignores letter case has 1 file for the 2 spellings."""

    from app import importing

    removed, rejected = [], []
    monkeypatch.setattr(importing.os, "remove", removed.append)
    monkeypatch.setattr(
        importing,
        "move_to_rejects",
        lambda path, reason: rejected.append((path, reason)),
    )
    new_file = tmp_path / "TRAILER.mkv"
    new_file.write_bytes(b"new")
    link = tmp_path / "an other name of the new file.mkv"
    os.link(new_file, link)
    other_extra = tmp_path / "A Different Trailer.mkv"
    other_extra.write_bytes(b"old")
    with app.app_context():
        importing._delete_worse_copies(
            [(str(link), None), (str(other_extra), None)], str(new_file)
        )
    assert removed == []
    assert rejected == [(str(other_extra), "other spelling")]


def _chain_until_the_cataloging(app, source):
    """Run the localization and the move of a source. Return the cataloging job."""

    assert videos.localization_task(source) is True
    (move,) = [
        job
        for job in app.handoff_queue.jobs
        if job.func_name == "app.videos.move_localized_file"
    ]
    assert videos.move_localized_file(*move.args) is True
    (finalize,) = [
        job
        for job in app.sql_queue.jobs
        if job.func_name == "app.videos.finalize_localization"
    ]
    return finalize


def test_a_second_download_during_an_in_place_import_is_not_replaced(app, sample_mkv):
    """Radarr puts a second download at the path during the chain of the first (#742).

    Its webhook queues no job, because the chain of the first names the
    path. The cataloging must not put the localized copy of the first
    download over the second. The record does not change, and the
    import of the new file is booked."""

    from rq.registry import ScheduledJobRegistry

    directory = os.path.join(app.config["LIBRARY_DIR"], "Movies/Second Grab (2021)")
    basename = "Second Grab (2021) - [DVD].mkv"
    os.makedirs(directory, exist_ok=True)
    source = os.path.join(directory, basename)
    shutil.copy(sample_mkv, source)
    with app.app_context():
        finalize = _chain_until_the_cataloging(app, source)
        assert finalize.args[1]["source_stamp"]["size"] == os.path.getsize(sample_mkv)

        os.remove(source)
        with open(source, "wb") as handle:
            handle.write(b"the second download")

        assert finalize_localization(*finalize.args, **finalize.kwargs) is False

        with open(source, "rb") as handle:
            assert handle.read() == b"the second download"
        assert os.listdir(directory) == [basename]
        assert File.query.filter_by(basename=basename).count() == 0
        registry = ScheduledJobRegistry(queue=app.import_queue)
        booked = [app.import_queue.fetch_job(i) for i in registry.get_job_ids()]
        assert [
            job.args
            for job in booked
            if job.func_name == "app.videos.localization_task"
        ] == [(source,)]
        for job in booked:
            registry.remove(job, delete_job=True)
    os.remove(source)


def test_a_source_of_the_import_directory_gets_no_stamp(app, sample_mkv, incoming_dir):
    """Only a source in the library can be a download of Radarr or Sonarr (#742)."""

    basename = "Import Dir Stamp (2021) - [DVD].mkv"
    source = os.path.join(incoming_dir, basename)
    shutil.copy(sample_mkv, source)
    with app.app_context():
        finalize = _chain_until_the_cataloging(app, source)
        assert "source_stamp" not in finalize.args[1]
        finalize_localization(*finalize.args, **finalize.kwargs)
        output = os.path.join(
            app.config["LIBRARY_DIR"],
            File.query.filter_by(basename=basename).one().file_path,
        )
    os.remove(output)


def test_a_changed_source_of_a_conversion_is_not_removed(app, tmp_path, monkeypatch):
    """A conversion has an other name than its source. The source goes at the end (#742).

    A source that an other program changed during the chain is a new
    download. It stays, and its import is booked."""

    booked = []
    monkeypatch.setattr(
        importing, "requeue", lambda name, **kw: booked.append(kw["args"])
    )
    monkeypatch.setitem(app.config, "LIBRARY_DIR", str(tmp_path))
    source = tmp_path / "Converted (2021) - [Remux-2160p].mkv"
    source.write_bytes(b"first download")
    with app.app_context():
        details = {"source_stamp": importing.library_stamp(str(source))}
        assert importing._source_changed(str(source), details) is False
        source.write_bytes(b"the second download, of an other size")
        assert importing._source_changed(str(source), details) is True
        assert importing._source_changed(str(source), {}) is False
        importing._import_changed_source(str(source))
    assert booked == [(str(source),)]
    assert source.exists()
