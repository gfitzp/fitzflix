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
    monkeypatch.setitem(app.config, "IMPORT_DIR", str(tmp_path / "import"))
    (tmp_path / "import").mkdir()
    source = tmp_path / "import" / "Outage Convert (2001) - [Remux-2160p].mkv"
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
    monkeypatch.setitem(app.config, "IMPORT_DIR", str(tmp_path / "import"))
    (tmp_path / "import").mkdir()
    source = tmp_path / "import" / name
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


def test_a_later_stage_rejects_a_source_that_no_sweep_finds(app, tmp_path, monkeypatch):
    """An arr download is outside the import directory. No sweep retries its cataloging.

    Thus, an outage in the cataloging rejects it with an ERROR, as before
    #681. A person then sees it on the Rejects page."""

    import app.importing as importing
    import app.videos as videos

    name = "Outage Library (2022) - [DVD].mkv"
    hidden = tmp_path / f".{name}"
    hidden.write_bytes(b"library copy")
    source = tmp_path / "library" / name
    source.parent.mkdir()
    source.write_bytes(b"source")
    details = {
        "basename": name,
        "dirname": "Movies/Outage Library (2022)",
        "file_path": f"Movies/Outage Library (2022)/{name}",
        "media_library": "Movies",
    }
    monkeypatch.setitem(app.config, "IMPORT_DIR", str(tmp_path / "import"))
    monkeypatch.setattr(importing, "_build_records", _raiser(outage(2006, "gone away")))
    rejected = _rejected(app, name)
    with app.app_context():
        lock = app.lock_manager.lock('{"title": "Outage Library"}', 60000)
        try:
            assert (
                videos.finalize_localization(
                    str(source), details, lock, str(hidden), {}
                )
                is None
            )
            assert not source.exists()
            assert os.path.exists(rejected)
        finally:
            _remove(rejected)
    assert app.redis.get('{"title": "Outage Library"}') is None


def _cataloging_over_the_source(
    app, tmp_path, monkeypatch, name, error, record, upload=None
):
    """Run a cataloging whose new file replaces its own source, and whose commit fails.

    The real _place_file renames the hidden copy over the library file
    of the source. upload holds the archive columns of the import.
    Return (source, rejected path, File id or None)."""

    import app.importing as importing
    import app.videos as videos
    from app import db
    from tests.factories import make_file

    dirname = f"Movies/{name[:-4]}"
    library = tmp_path / dirname
    library.mkdir(parents=True)
    source = library / name
    source.write_bytes(b"source")
    hidden = library / f".{name}"
    hidden.write_bytes(b"library copy")
    details = {
        "basename": name,
        "dirname": dirname,
        "file_path": f"{dirname}/{name}",
        "media_library": "Movies",
        **(upload or {}),
    }
    monkeypatch.setitem(app.config, "LIBRARY_DIR", str(tmp_path))
    monkeypatch.setitem(app.config, "IMPORT_DIR", str(tmp_path / "import"))
    monkeypatch.setattr(importing, "_build_records", lambda *a, **k: (None,) * 5)
    monkeypatch.setattr(importing, "_commit_records", _raiser(error))
    with app.app_context():
        file_id = None
        if record:
            file_id = make_file(name, dirname, name[:-4], "Movies", "DVD").id
            db.session.commit()
        lock = app.lock_manager.lock(f'{{"title": "{name}"}}', 60000)
        if isinstance(error, Exception):
            assert (
                videos.finalize_localization(
                    str(source), details, lock, str(hidden), {}
                )
                is None
            )
        else:
            with pytest.raises(type(error)):
                videos.finalize_localization(
                    str(source), details, lock, str(hidden), {}
                )
        assert app.redis.get(f'{{"title": "{name}"}}') is None
    assert not hidden.exists()
    return source, _rejected(app, name), file_id


@pytest.mark.parametrize(
    "error", [SystemExit(), RuntimeError("commit")], ids=["stop", "error"]
)
def test_a_file_that_its_record_points_to_stays_at_its_path(
    app, tmp_path, monkeypatch, error, caplog
):
    """The new file replaced the file of a record. The repair of #592 covers it (#688).

    A stop signal is no fault of the import. Its record in the log is a
    warning."""

    import app.importing as importing
    from app import db
    from app.models import File

    scans = []
    monkeypatch.setattr(
        importing,
        "_rescan_after_a_failed_edit",
        lambda file_id, name: scans.append(file_id),
    )
    name = f"Over Record {type(error).__name__} (2023) - [DVD].mkv"
    source, rejected, file_id = _cataloging_over_the_source(
        app, tmp_path, monkeypatch, name, error, record=True
    )
    assert source.read_bytes() == b"library copy"
    assert not os.path.exists(rejected)
    assert scans == [file_id]
    with app.app_context():
        assert db.session.get(File, file_id).aws_stale
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert bool(errors) == isinstance(error, Exception)


def _run_scoped_upload(name):
    """Return the archive columns of an import whose upload went to a run-scoped key."""

    from datetime import datetime, timezone

    return {
        "aws_key": f"pending-untouched/0123456789ab/{name}",
        "aws_date_uploaded": datetime(2026, 11, 12, 9, 0, tzinfo=timezone.utc),
        "aws_filesize_bytes": 6,
        "untouched_basename": name,
    }


def _discards(monkeypatch):
    """Record each run-scoped upload that the import deletes."""

    import app.importing as importing

    deleted = []
    monkeypatch.setattr(importing, "discard_pending_archive", deleted.append)
    return deleted


def test_the_record_takes_the_original_of_a_file_that_replaced_its_source(
    app, tmp_path, monkeypatch
):
    """The upload holds the only copy of the original of the new file (#727).

    The import must not delete it. The record at the path claims it,
    and its archive is current."""

    import app.importing as importing
    from app import db
    from app.models import File

    monkeypatch.setattr(importing, "_rescan_after_a_failed_edit", lambda *a: None)
    deleted = _discards(monkeypatch)
    name = "Over Record Upload (2023) - [DVD].mkv"
    upload = _run_scoped_upload(name)
    source, rejected, file_id = _cataloging_over_the_source(
        app, tmp_path, monkeypatch, name, RuntimeError("commit"), True, upload
    )
    assert source.read_bytes() == b"library copy"
    assert deleted == []
    with app.app_context():
        row = db.session.get(File, file_id)
        assert row.aws_key == upload["aws_key"]
        assert row.aws_filesize_bytes == 6
        assert row.aws_date_uploaded is not None
        assert row.untouched_basename == name
        assert not row.aws_stale


def test_a_conversion_does_not_take_the_original_of_the_new_file(
    app, tmp_path, monkeypatch
):
    """The library file of a conversion has its archive in transcoded/ (#727).

    The upload still stays, and the stale flag asks for the repair."""

    import app.importing as importing
    from app import db
    from app.models import File

    monkeypatch.setattr(importing, "_rescan_after_a_failed_edit", lambda *a: None)
    deleted = _discards(monkeypatch)
    real = importing._claim_at_path

    def claim_of_a_conversion(library_file_path, output_file, upload=None):
        File.query.filter_by(file_path=library_file_path).update(
            {"converted_from_quality": "Remux-2160p"}
        )
        db.session.commit()
        return real(library_file_path, output_file, upload)

    monkeypatch.setattr(importing, "_claim_at_path", claim_of_a_conversion)
    name = "Over Conversion (2023) - [DVD].mkv"
    upload = _run_scoped_upload(name)
    source, rejected, file_id = _cataloging_over_the_source(
        app, tmp_path, monkeypatch, name, RuntimeError("commit"), True, upload
    )
    assert deleted == []
    with app.app_context():
        row = db.session.get(File, file_id)
        assert row.aws_key != upload["aws_key"]
        assert row.aws_stale


def test_the_original_of_a_rejected_localized_file_stays_in_s3(
    app, tmp_path, monkeypatch, caplog
):
    """No record points to the path. The file over the source goes to rejects.

    That file is not the download. The upload is the only copy of the
    original, and the import does not delete it (#727)."""

    deleted = _discards(monkeypatch)
    name = "Over Nothing Upload (2023) - [DVD].mkv"
    upload = _run_scoped_upload(name)
    source, rejected, _ = _cataloging_over_the_source(
        app, tmp_path, monkeypatch, name, RuntimeError("commit"), False, upload
    )
    try:
        assert os.path.exists(rejected)
        assert deleted == []
        assert any(upload["aws_key"] in r.getMessage() for r in caplog.records)
    finally:
        _remove(rejected)


def test_a_failed_cataloging_deletes_the_upload_while_the_source_is_whole(
    app, tmp_path, monkeypatch
):
    """The source keeps its original bytes. Then the upload is a copy, and it goes."""

    import app.importing as importing
    import app.videos as videos

    deleted = _discards(monkeypatch)
    name = "Whole Source (2022) - [DVD].mkv"
    upload = _run_scoped_upload(name)
    hidden = tmp_path / f".{name}"
    hidden.write_bytes(b"library copy")
    source = tmp_path / "downloads" / name
    source.parent.mkdir()
    source.write_bytes(b"source")
    details = {
        "basename": name,
        "dirname": "Movies/Whole Source (2022)",
        "file_path": f"Movies/Whole Source (2022)/{name}",
        "media_library": "Movies",
        **upload,
    }
    monkeypatch.setattr(importing, "_build_records", _raiser(RuntimeError("records")))
    rejected = _rejected(app, name)
    with app.app_context():
        lock = app.lock_manager.lock('{"title": "Whole Source"}', 60000)
        try:
            videos.finalize_localization(str(source), details, lock, str(hidden), {})
            assert os.path.exists(rejected)
        finally:
            _remove(rejected)
    assert deleted == [upload["aws_key"]]


@pytest.mark.parametrize("in_place", [True, False], ids=["in place", "staged"])
def test_a_failed_localization_keeps_the_upload_of_a_source_that_it_rewrote(
    app, sample_mkv, incoming_dir, monkeypatch, in_place
):
    """A source that is localized in place can change before the failure (#727).

    Then the run-scoped upload has the only original, and it stays. The
    source of a staged run does not change, and its upload goes."""

    from app import importing

    deleted = _discards(monkeypatch)
    basename = f"Upload Kept {in_place} (2003) - [DVD].mkv"
    upload = _run_scoped_upload(basename)
    source = _source_in(incoming_dir, basename, sample_mkv)
    monkeypatch.setattr(
        importing, "_archive_original", lambda path, details: details.update(upload)
    )
    if in_place:
        monkeypatch.setattr(importing, "_stage_source", lambda path, run: (path, False))
    monkeypatch.setattr(importing, "_localize", _raiser(RuntimeError("localize")))
    try:
        with app.app_context():
            importing.localization_task(source)
        assert os.path.exists(_rejected(app, basename))
        assert deleted == ([] if in_place else [upload["aws_key"]])
    finally:
        _remove(source, _rejected(app, basename))


def test_a_fault_in_the_record_lookup_does_not_keep_the_title_lock(
    app, tmp_path, monkeypatch
):
    """The helper of the failure branch raises. The lock is free and the file stays."""

    import app.importing as importing

    def broken(library_file_path, output_file, upload=None):
        raise OSError("the repair failed")

    monkeypatch.setattr(importing, "_claim_at_path", broken)
    name = "Over Broken (2023) - [DVD].mkv"
    source, rejected, _ = _cataloging_over_the_source(
        app, tmp_path, monkeypatch, name, RuntimeError("commit"), record=False
    )
    assert source.read_bytes() == b"library copy"
    assert not os.path.exists(rejected)


@pytest.mark.parametrize(
    "error", [SystemExit(), RuntimeError("commit")], ids=["stop", "error"]
)
def test_a_file_over_its_source_with_no_record_goes_to_rejects(
    app, tmp_path, monkeypatch, error
):
    """That file is not the rip. A kept file would import again as an original (#424)."""

    name = f"Over Nothing {type(error).__name__} (2023) - [DVD].mkv"
    source, rejected, _ = _cataloging_over_the_source(
        app, tmp_path, monkeypatch, name, error, record=False
    )
    try:
        assert not source.exists()
        with open(rejected, "rb") as handle:
            assert handle.read() == b"library copy"
    finally:
        _remove(rejected)


def test_a_file_over_its_source_stays_when_the_record_lookup_fails(
    app, tmp_path, monkeypatch
):
    """A row can point to the path, and the database did not say. The file stays (#688)."""

    import app.importing as importing

    class Unreadable:
        """A File model whose id column fails like a lost connection."""

        @property
        def id(self):
            raise outage(2013, "Lost connection")

    monkeypatch.setattr(importing, "File", Unreadable())
    name = "Over Unknown (2023) - [DVD].mkv"
    source, rejected, _ = _cataloging_over_the_source(
        app, tmp_path, monkeypatch, name, RuntimeError("commit"), record=False
    )
    assert source.read_bytes() == b"library copy"
    assert not os.path.exists(rejected)


def test_a_failed_record_lookup_books_the_repair_of_the_kept_file(
    app, tmp_path, monkeypatch
):
    """The database did not answer for the lookup. The repair runs when it answers.

    Without it, the row keeps the size, the tracks and the archive of
    the old file, and nothing finds that later."""

    import app.importing as importing
    from app import db
    from app.models import File
    from rq.registry import ScheduledJobRegistry

    real_file = importing.File

    class Unreadable:
        @property
        def id(self):
            raise outage(2013, "Lost connection")

    def lose_the_database(*args, **kwargs):
        monkeypatch.setattr(importing, "File", Unreadable())
        raise outage(2013, "Lost connection")

    monkeypatch.setattr(
        "tests.test_database_outage._raiser", lambda error: lose_the_database
    )
    name = "Over Outage (2023) - [DVD].mkv"
    upload = _run_scoped_upload(name)
    deleted = _discards(monkeypatch)
    source, rejected, file_id = _cataloging_over_the_source(
        app, tmp_path, monkeypatch, name, RuntimeError("unused"), True, upload
    )
    assert deleted == []
    monkeypatch.setattr(importing, "File", real_file)
    assert source.read_bytes() == b"library copy"
    assert not os.path.exists(rejected)

    scans, stale = [], []
    monkeypatch.setattr(
        importing, "_rescan_after_a_failed_edit", lambda fid, label: scans.append(fid)
    )
    monkeypatch.setattr(
        importing, "mark_archive_stale", lambda fid, **kwargs: stale.append(fid)
    )
    with app.app_context():
        registry = ScheduledJobRegistry(queue=app.sql_queue)
        booked = [
            job
            for job in map(app.sql_queue.fetch_job, registry.get_job_ids())
            if job and job.func_name == "app.importing.repair_replaced_file_task"
        ]
        assert len(booked) == 1
        job = booked[0]
        assert job.args[0] == f"Movies/{name[:-4]}/{name}"
        try:
            # The row still has the size of the old file. The task repairs
            # it, and the row takes the original of the new file (#727).
            assert job.kwargs["upload"] == upload
            assert job.func(*job.args, **job.kwargs) is True
            assert scans == [file_id] and stale == []
            assert db.session.get(File, file_id).aws_key == upload["aws_key"]

            # A row that agrees with the file needs no repair.
            row = db.session.get(File, file_id)
            row.filesize_bytes = os.path.getsize(source)
            db.session.commit()
            assert job.func(*job.args, **job.kwargs) is False
            assert scans == [file_id] and stale == []
        finally:
            registry.remove(job, delete_job=True)


def test_the_repair_of_a_kept_file_waits_for_the_database(app, tmp_path, monkeypatch):
    """The task books itself while the database is away, up to its maximum."""

    import app.importing as importing

    booked = []
    monkeypatch.setattr(
        importing,
        "_book_replaced_file_repair",
        lambda path, output, tries, upload=None: booked.append(tries),
    )

    class Unreadable:
        @property
        def id(self):
            raise outage(2013, "Lost connection")

    monkeypatch.setattr(importing, "File", Unreadable())
    output = tmp_path / "Kept (2023) - [DVD].mkv"
    output.write_bytes(b"library copy")
    path = "Movies/Kept (2023)/Kept (2023) - [DVD].mkv"
    last = importing.REPLACED_REPAIR_MAX_TRIES
    assert importing.repair_replaced_file_task(path, str(output), 1) is False
    assert importing.repair_replaced_file_task(path, str(output), last) is False
    assert booked == [2]
