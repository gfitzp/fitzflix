"""Test that an outage of the database keeps the source of an import (#681).

A lost or refused connection to MariaDB is a wait, like a stop signal.
The source stays where it is, 1 warning says why, and the job fails.
The hourly sweeps then queue the file again. A constraint error or a
deadlock still rejects the source."""

import logging
import os

import pytest
from pymysql import err as mysql_err
from sqlalchemy import exc as sa_exc

OUTAGE = "The database did not answer"


def outage(code=2003, message="Can't connect to MySQL server on '127.0.0.1'"):
    """Return the SQLAlchemy error of a pymysql connection fault."""

    return sa_exc.OperationalError(
        "SELECT 1", {}, mysql_err.OperationalError(code, message)
    )


def _raiser(error):
    """Return a function that raises error."""

    def fail(*args, **kwargs):
        raise error

    return fail


def _source_in(directory, basename, sample_mkv):
    """Copy the sample file to directory under basename and return the path."""

    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, basename)
    with open(sample_mkv, "rb") as f_in, open(path, "wb") as f_out:
        f_out.write(f_in.read())
    return path


def _rejected(app, basename, reason="exception"):
    return os.path.join(app.config["REJECTS_DIR"], reason, basename)


def _app_records(caplog, level):
    return [
        record
        for record in caplog.records
        if record.name == "app" and record.levelno == level
    ]


def _remove(*paths):
    for path in paths:
        if os.path.exists(path):
            os.remove(path)


def test_database_outage_knows_the_connection_faults(app):
    from app.importing import database_outage

    for code in (2002, 2003, 2006, 2013):
        assert database_outage(outage(code, "gone"))
    assert database_outage(
        sa_exc.InterfaceError("SELECT 1", {}, mysql_err.InterfaceError(2013, "lost"))
    )
    invalidated = sa_exc.DBAPIError(
        "SELECT 1", {}, mysql_err.InterfaceError(0, ""), connection_invalidated=True
    )
    assert database_outage(invalidated)

    assert not database_outage(outage(1213, "Deadlock found"))
    assert not database_outage(outage(1205, "Lock wait timeout exceeded"))
    assert not database_outage(outage(1020, "Record has changed"))
    assert not database_outage(
        sa_exc.IntegrityError(
            "INSERT", {}, mysql_err.IntegrityError(1062, "Duplicate entry")
        )
    )
    assert not database_outage(RuntimeError("x"))
    assert not database_outage(SystemExit(1))


def test_an_outage_at_the_first_read_keeps_the_import_and_the_sweep_queues_it(
    app, sample_mkv, monkeypatch, caplog
):
    """The source stays in the import directory. The hourly sweep takes it again."""

    from app import importing, safe_job_id

    basename = "Outage Read (2001) - [DVD].mkv"
    source = _source_in(app.config["IMPORT_DIR"], basename, sample_mkv)
    real_cataloged = importing.already_cataloged
    monkeypatch.setattr(importing, "already_cataloged", _raiser(outage(2003)))
    caplog.set_level(logging.INFO)
    try:
        with app.app_context(), pytest.raises(sa_exc.OperationalError):
            importing.localization_task(source)
        assert os.path.exists(source)
        assert not os.path.exists(_rejected(app, basename))
        warnings = [r for r in _app_records(caplog, logging.WARNING)]
        assert [OUTAGE in r.getMessage() for r in warnings] == [True]
        assert _app_records(caplog, logging.ERROR) == []

        monkeypatch.setattr(importing, "already_cataloged", real_cataloged)
        with app.app_context():
            importing.manual_import_task()
        assert safe_job_id(basename) in app.import_queue.job_ids
    finally:
        job = app.import_queue.fetch_job(safe_job_id(basename))
        if job is not None:
            job.delete()
        _remove(source, _rejected(app, basename))


def test_an_outage_after_the_title_lock_releases_the_lock(
    app, sample_mkv, monkeypatch, caplog
):
    from app import importing

    basename = "Outage Lock (2001) - [DVD].mkv"
    source = _source_in(app.config["IMPORT_DIR"], basename, sample_mkv)
    details = {
        "media_library": "Movies",
        "title": "Outage Lock",
        "year": 2001,
        "basename": basename,
    }
    monkeypatch.setattr(importing, "_identify", lambda path, run: (details, None))
    monkeypatch.setattr(
        importing, "_quality_and_space_gate", _raiser(outage(2013, "Lost connection"))
    )
    caplog.set_level(logging.INFO)
    try:
        with app.app_context():
            lock_key = importing.lock_key_for_details(details)
            with pytest.raises(sa_exc.OperationalError):
                importing.localization_task(source)
            assert app.redis.get(lock_key) is None
        assert os.path.exists(source)
        assert not os.path.exists(_rejected(app, basename))
        assert len(_app_records(caplog, logging.WARNING)) == 1
        assert _app_records(caplog, logging.ERROR) == []
    finally:
        _remove(source, _rejected(app, basename))


@pytest.mark.parametrize(
    "error",
    [
        outage(1213, "Deadlock found"),
        sa_exc.IntegrityError(
            "INSERT", {}, mysql_err.IntegrityError(1062, "Duplicate entry")
        ),
    ],
    ids=["deadlock", "integrity"],
)
def test_a_database_error_that_is_no_outage_still_rejects(
    app, sample_mkv, monkeypatch, error
):
    from app import importing

    basename = f"No Outage {type(error).__name__} (2001) - [DVD].mkv"
    source = _source_in(app.config["IMPORT_DIR"], basename, sample_mkv)
    monkeypatch.setattr(importing, "already_cataloged", _raiser(error))
    try:
        with app.app_context():
            assert importing.localization_task(source) is True
        assert not os.path.exists(source)
        assert os.path.exists(_rejected(app, basename))
    finally:
        _remove(source, _rejected(app, basename))


def test_an_outage_before_the_rewrite_keeps_a_library_file_for_the_in_place_sweep(
    app, sample_mkv, incoming_dir, monkeypatch
):
    """A Sonarr or Radarr download stays in the library. Its failed job is swept.

    The job runs in a worker. Thus, it really fails, and the hourly retry
    of the failed in-place imports finds it."""

    from rq.registry import FailedJobRegistry

    from app import importing
    from app.pipeline import PipelineWorker

    basename = "Outage In Place (2001) - [WEBDL-1080p].mkv"
    source = _source_in(incoming_dir, basename, sample_mkv)
    real_cataloged = importing.already_cataloged
    monkeypatch.setattr(importing, "already_cataloged", _raiser(outage(2003)))
    queue = app.import_queue
    job = queue.enqueue("app.videos.localization_task", source)
    try:
        PipelineWorker([queue], connection=app.redis).work(burst=True)
        assert job.id in FailedJobRegistry(queue=queue).get_job_ids()
        assert os.path.exists(source)
        assert not os.path.exists(_rejected(app, basename))

        monkeypatch.setattr(importing, "already_cataloged", real_cataloged)
        with app.app_context():
            importing._retry_failed_in_place_imports()
        queued = [
            queued
            for queued in queue.jobs
            if queued.func_name == "app.videos.localization_task"
            and queued.args == (source,)
        ]
        assert len(queued) == 1
    finally:
        for stale in queue.jobs:
            if stale.args == (source,):
                stale.delete()
        FailedJobRegistry(queue=queue).remove(job, delete_job=True)
        _remove(source, _rejected(app, basename))


@pytest.mark.parametrize(
    "phase, kept", [("_archive_original", True), ("_localize", False)]
)
def test_an_in_place_outage_keeps_the_source_only_before_the_rewrite(
    app, sample_mkv, incoming_dir, monkeypatch, phase, kept
):
    """A source processed in place can be rewritten from _localize on (#424)."""

    from app import importing

    basename = f"Outage {phase} (2003) - [DVD].mkv"
    source = _source_in(incoming_dir, basename, sample_mkv)
    monkeypatch.setattr(importing, "_stage_source", lambda path, run: (path, False))
    monkeypatch.setattr(importing, phase, _raiser(outage(2006, "gone away")))
    try:
        with app.app_context():
            if kept:
                with pytest.raises(sa_exc.OperationalError):
                    importing.localization_task(source)
            else:
                importing.localization_task(source)
        assert os.path.exists(source) is kept
        assert os.path.exists(_rejected(app, basename)) is not kept
    finally:
        _remove(source, _rejected(app, basename))


def test_a_conversion_whose_database_goes_away_keeps_the_source(
    app, tmp_path, monkeypatch
):
    """Mirror the stop-signal test of the conversion with a 2006 (#497)."""

    from app import sdr_conversion

    hidden = tmp_path / ".Outage Convert (2001) - [Bluray-1080p].mkv"
    hidden.write_bytes(b"localized")
    source = tmp_path / "Outage Convert (2001) - [Remux-2160p].mkv"
    source.write_bytes(b"source")
    monkeypatch.setattr(
        sdr_conversion, "probe_video", _raiser(outage(2006, "gone away"))
    )
    with app.app_context():
        lock = app.lock_manager.lock('{"title": "Outage Convert"}', 60000)
        with pytest.raises(sa_exc.OperationalError):
            sdr_conversion.convert_localized_file(
                str(source),
                {"basename": "Outage Convert (2001) - [Bluray-1080p].mkv"},
                lock,
                str(hidden),
                {
                    "source_format": "HDR10",
                    "source_quality": "x",
                    "target_quality": "y",
                    "max_height": 1080,
                },
            )
    assert source.exists()
    assert not hidden.exists()
    assert app.redis.get('{"title": "Outage Convert"}') is None
    rejected = os.path.join(app.config["REJECTS_DIR"], "conversion failed")
    assert not os.path.exists(os.path.join(rejected, source.name))


def test_a_cataloging_whose_database_goes_away_keeps_the_source(
    app, tmp_path, monkeypatch
):
    """The commit fails with a 2006, and the rollback fails too. The source stays."""

    import app.importing as importing
    import app.videos as videos
    from app import db

    name = "Outage Catalog (2021) - [DVD].mkv"
    hidden = tmp_path / f".{name}"
    hidden.write_bytes(b"library copy")
    staged = tmp_path / f"staged-{name}"
    staged.write_bytes(b"staged copy")
    source = tmp_path / name
    source.write_bytes(b"source")
    details = {
        "basename": name,
        "dirname": "Movies/Outage Catalog (2021)",
        "file_path": f"Movies/Outage Catalog (2021)/{name}",
        "media_library": "Movies",
    }
    monkeypatch.setattr(importing, "_build_records", _raiser(outage(2006, "gone away")))
    with app.app_context():
        real_rollback = db.session.rollback
        calls = []

        def rollback():
            calls.append(1)
            if len(calls) == 1:
                raise outage(2013, "Lost connection")
            real_rollback()

        monkeypatch.setattr(db.session, "rollback", rollback)
        lock = app.lock_manager.lock('{"title": "Outage Catalog"}', 60000)
        with pytest.raises(sa_exc.OperationalError):
            videos.finalize_localization(
                str(source), details, lock, str(hidden), {}, staged_output=str(staged)
            )
        monkeypatch.undo()
    assert app.redis.get('{"title": "Outage Catalog"}') is None
    assert source.exists()
    assert not hidden.exists() and not staged.exists()
    assert not os.path.exists(
        os.path.join(app.config["REJECTS_DIR"], "exception", name)
    )
