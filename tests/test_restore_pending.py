"""Test the pending restore state of a File row (#610).

A restore request sets File.restore_requested_at. While it is set, the
forms of the file are off, and each writer refuses. The placement, each
terminal end of the download and the 14-day sweep clear it.
"""

import ast
import importlib.util
import inspect
import os
import re

import pytest

from tests.fakes import S3, SQS


def _pending_file(app, title, key=None, requested=True, local=True):
    """Make a movie file with a library copy and an optional pending restore.

    Return (file id, archive key, library path)."""

    from datetime import datetime, timezone

    from app import db
    from tests.factories import make_movie, make_movie_file

    file = make_movie_file(make_movie(title, 1953), "Bluray-1080p")
    file.aws_key = key or f"untouched/{file.basename}"
    file.aws_date_uploaded = datetime(2026, 10, 1)
    if requested:
        file.restore_requested_at = datetime.now(timezone.utc)
    db.session.commit()
    path = os.path.join(app.config["LIBRARY_DIR"], file.file_path)
    if local:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(b"library file")
    return file.id, file.aws_key, path


def _pending(file_id):
    from app import db
    from app.models import File

    db.session.expire_all()
    return db.session.get(File, file_id).restore_requested_at


def _remove(*paths):
    for path in paths:
        if path and os.path.exists(path):
            os.remove(path)


def test_the_migration_adds_the_column_after_the_uid_steps():
    path = os.path.join(
        os.path.dirname(__file__),
        "..",
        "migrations",
        "versions",
        "c4e8f2a6d1b9_file_restore_requested_at.py",
    )
    spec = importlib.util.spec_from_file_location("restore_pending_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.revision == "c4e8f2a6d1b9"
    assert migration.down_revision == "b3a9d1e7c4f2"
    assert "restore_requested_at" in inspect.getsource(migration.upgrade)
    assert "drop_column" in inspect.getsource(migration.downgrade)


def test_the_refuse_helper_reads_the_database_not_the_loaded_row(app):
    """The request does not take the title lock. A writer can hold an old row."""

    from datetime import datetime

    from app import db
    from app.aws_storage import RESTORE_PENDING_MESSAGE, refuse_while_restore_pending
    from app.models import File

    with app.app_context():
        file_id, _, path = _pending_file(app, "Old Row", requested=False)
        file = db.session.get(File, file_id)
        assert refuse_while_restore_pending(file) is None
        with db.engine.begin() as other:
            other.execute(
                db.update(File)
                .where(File.id == file_id)
                .values(restore_requested_at=datetime(2026, 10, 29))
            )
        assert refuse_while_restore_pending(file) == RESTORE_PENDING_MESSAGE
    _remove(path)


def _queued_jobs(app):
    """Return the func names of the jobs that wait on any queue."""

    from rq import Queue

    return [
        job.func_name for queue in Queue.all(connection=app.redis) for job in queue.jobs
    ]


def _fieldset_of(page, field):
    """Return the opening fieldset tag of the form that holds field."""

    for match in re.finditer(r"<fieldset[^>]*>(.*?)</fieldset>", page, re.S):
        if f'name="{field}"' in match.group(1):
            return match.group(0).split(">", 1)[0]
    raise AssertionError(f"no fieldset holds {field}")


def test_a_restore_request_sets_the_state_and_turns_off_the_forms(
    app, admin_client, monkeypatch
):
    from app import aws_storage

    class FrozenS3:
        def head_object(self, **kwargs):
            return {"StorageClass": "DEEP_ARCHIVE"}

        def restore_object(self, **kwargs):
            return {}

    monkeypatch.setattr(aws_storage, "aws_s3_client", lambda **kwargs: FrozenS3())
    with app.app_context():
        file_id, key, path = _pending_file(app, "Forms Off", requested=False)
    try:
        page = admin_client.get(f"/file/{file_id}").get_data(as_text=True)
        assert "disabled" not in _fieldset_of(page, "s3_upload_submit")
        assert "disabled" not in _fieldset_of(page, "s3_download_submit")

        with app.app_context():
            aws_storage.aws_restore(key)
            assert _pending(file_id) is not None

        page = admin_client.get(f"/file/{file_id}").get_data(as_text=True)
        assert "A restore of this file from AWS is pending" in page
        for field in ("s3_upload_submit", "s3_download_submit"):
            assert "disabled" in _fieldset_of(page, field)
    finally:
        _remove(path)


@pytest.mark.parametrize(
    "submit",
    [
        "scan_submit",
        "mkvpropedit_submit",
        "mkvmerge_submit",
        "s3_upload_submit",
        "s3_download_submit",
    ],
)
def test_the_file_page_refuses_each_edit_while_a_restore_is_pending(
    app, admin_client, submit
):
    from tests.conftest import csrf_token_from

    with app.app_context():
        file_id, _, path = _pending_file(app, f"Refused Post {submit}")
    try:
        page = admin_client.get(f"/file/{file_id}").get_data(as_text=True)
        response = admin_client.post(
            f"/file/{file_id}",
            data={"csrf_token": csrf_token_from(page), submit: "1"},
            follow_redirects=True,
        )
        # The flash has the message of the helper. The page note differs.
        assert "Fitzflix does not change the file" in response.get_data(as_text=True)
        assert _queued_jobs(app) == []
    finally:
        _remove(path)


@pytest.mark.parametrize("page", ["subtitles", "lossy-audio"])
def test_the_triage_pages_turn_off_the_track_controls(app, admin_client, page):
    from app import db
    from tests.conftest import csrf_token_from

    if page == "subtitles":
        from tests.test_subtitle_triage import build_candidate

        submit = {"mark_forced_submit": "1", "track_uids": ["2"]}
    else:
        from tests.test_lossy_triage import build_candidate

        submit = {"promote_submit": "1", "lossless_track": "2"}
    from datetime import datetime

    with app.app_context():
        file, _ = build_candidate(title=f"Triage Pending {page}")
        file.restore_requested_at = datetime(2026, 10, 29)
        db.session.commit()
        file_id = file.id
        path = os.path.join(app.config["LIBRARY_DIR"], file.file_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(b"mkv bytes")
    try:
        html = admin_client.get(f"/maintenance/{page}/{file_id}").get_data(as_text=True)
        assert "A restore of this file from AWS is pending" in html
        assert "<fieldset disabled>" in html
        response = admin_client.post(
            f"/maintenance/{page}",
            data={"csrf_token": csrf_token_from(html), "file_id": file_id} | submit,
            follow_redirects=True,
        )
        assert "Fitzflix does not change the file" in response.get_data(as_text=True)
        assert _queued_jobs(app) == []
    finally:
        _remove(path)


def _run_writer(app, writer, file_id, path):
    """Run one writer on the file. Return its result."""

    from app import db
    from app.models import File

    if writer == "mkvpropedit_task":
        from app.tracks import mkvpropedit_task

        return mkvpropedit_task(file_id, "1", "0", [])
    if writer == "mkvmerge_task":
        from app.tracks import mkvmerge_task

        return mkvmerge_task(file_id, ["1"], [])
    if writer == "remux_audio_plan_task":
        from app.tracks import remux_audio_plan_task

        return remux_audio_plan_task(file_id, [["copy", 1]])
    if writer == "atmos_supplement_task":
        from app.atmos import atmos_supplement_task

        return atmos_supplement_task(file_id)
    if writer == "move_archive_to_modified_task":
        from app.aws_storage import move_archive_to_modified_task

        return move_archive_to_modified_task(file_id)
    from app.aws_storage import archive_library_file

    with app.app_context():
        return archive_library_file(db.session.get(File, file_id), path)


@pytest.mark.parametrize(
    "writer",
    [
        "mkvpropedit_task",
        "mkvmerge_task",
        "remux_audio_plan_task",
        "atmos_supplement_task",
        "move_archive_to_modified_task",
        "archive_library_file",
    ],
)
def test_each_writer_refuses_while_a_restore_is_pending(
    app, monkeypatch, log_capture, writer
):
    """A refusal changes nothing, and it releases the title lock."""

    from app import aws_storage, db
    from app.aws_storage import RESTORE_PENDING_MESSAGE
    from app.models import File

    def no_upload(*args, **kwargs):
        raise AssertionError("a refused writer must not upload")

    monkeypatch.setattr(aws_storage, "aws_upload", no_upload)
    monkeypatch.setattr(aws_storage, "aws_s3_client", no_upload)
    with app.app_context():
        file_id, key, path = _pending_file(app, f"Writer {writer}")
        before = os.stat(path)
        resource = db.session.get(File, file_id).file_identifier()
    try:
        assert not _run_writer(app, writer, file_id, path)
        assert os.stat(path).st_mtime_ns == before.st_mtime_ns
        with open(path, "rb") as handle:
            assert handle.read() == b"library file"
        with app.app_context():
            row = db.session.get(File, file_id)
            assert row.aws_key == key
            assert row.restore_requested_at is not None
        assert any(
            RESTORE_PENDING_MESSAGE in record.getMessage() for record in log_capture
        )
        lock = app.lock_manager.lock(resource, 1000)
        assert lock, "the refused writer kept the title lock"
        app.lock_manager.unlock(lock)
        assert _queued_jobs(app) == []
    finally:
        _remove(path)


def _direct_row(app, title, requested=True):
    """Return (file id, key, library path) of a row whose archive is a modified/ object."""

    with app.app_context():
        from app import db
        from app.models import File

        file_id, _, path = _pending_file(app, title, requested=requested, local=False)
        file = db.session.get(File, file_id)
        file.aws_key = f"modified/{file.basename}"
        db.session.commit()
        return file_id, file.aws_key, path


def _wire(monkeypatch, s3, sqs=None):
    import app.videos as videos
    from app import aws_storage

    monkeypatch.setattr(aws_storage, "aws_s3_client", lambda **kwargs: s3)
    monkeypatch.setattr(aws_storage, "aws_sqs_client", lambda: sqs)
    monkeypatch.setattr(aws_storage, "DOWNLOAD_RETRY_SLEEP", lambda seconds: None)
    monkeypatch.setattr(videos, "_dead_volumes", lambda paths: [])


def test_the_placement_clears_the_state_under_the_title_lock(app, monkeypatch):
    import app.videos as videos
    from app import aws_storage, db
    from app.models import File

    file_id, key, path = _direct_row(app, "Placed Clears")
    seen = []
    real_place = aws_storage.place_in_library

    def place(downloaded, library_path, expected_size, name):
        # The copy runs under the title lock, and the state is still set.
        with app.app_context():
            resource = db.session.get(File, file_id).file_identifier()
        seen.append((app.lock_manager.lock(resource, 1000), _pending(file_id)))
        real_place(downloaded, library_path, expected_size, name)

    monkeypatch.setattr(aws_storage, "place_in_library", place)
    _wire(monkeypatch, S3())
    try:
        with app.app_context():
            assert videos.download_task(key, os.path.basename(key)) is True
            assert _pending(file_id) is None
        ((lock, pending),) = seen
        assert lock is False and pending is not None
        with open(path, "rb") as handle:
            assert handle.read() == b"s3!"
        with app.app_context():
            resource = db.session.get(File, file_id).file_identifier()
        lock = app.lock_manager.lock(resource, 1000)
        assert lock, "the placement kept the title lock"
        app.lock_manager.unlock(lock)
    finally:
        _remove(path)


def test_a_busy_title_lock_defers_the_placement_and_keeps_the_copy(
    app, monkeypatch, held_lock
):
    import app.videos as videos
    from app import db
    from app.models import File
    from tests.test_scheduling import scheduled_jobs

    file_id, key, path = _direct_row(app, "Busy Lock")
    with app.app_context():
        held_lock(db.session.get(File, file_id).file_identifier())
    staged = os.path.join(app.config["IMPORT_DIR"], f".restore-{key.replace('/', '-')}")
    _wire(monkeypatch, S3())
    try:
        with app.app_context():
            assert videos.download_task(key, os.path.basename(key)) is False
            assert _pending(file_id) is not None
        assert os.path.exists(staged) and not os.path.exists(path)
        (retry,) = [
            job
            for job in scheduled_jobs(app.file_queue)
            if job.func_name == "app.videos.download_task"
        ]
        assert retry.kwargs == {"transient_retries": 0, "volume_waits": 1}
        retry.delete()
    finally:
        _remove(staged, f"{staged}.etag", path)


def _missing():
    from tests.test_aws_download import _client_error

    return _client_error("NoSuchKey", 404, "GetObject")


def _denied():
    from tests.test_aws_download import _client_error

    return _client_error("AccessDenied", 403, "GetObject")


@pytest.mark.parametrize(
    "outcome",
    ["object missing", "non-retryable", "retries used up", "access denied"],
)
def test_each_terminal_download_outcome_clears_the_state(app, monkeypatch, outcome):
    from app import aws_storage

    file_id, key, path = _direct_row(app, f"Terminal {outcome}")
    sqs = SQS()
    if outcome == "object missing":
        s3, expected = S3(errors=_missing()), aws_storage.DOWNLOAD_OBJECT_MISSING
    elif outcome == "non-retryable":
        s3, expected = S3(errors=_denied()), False
    elif outcome == "retries used up":
        s3, expected = S3(errors=RuntimeError("flaky")), False
    else:
        from tests.test_cdn_download import _FakeResponse

        s3 = S3(head={"ContentLength": 3, "ETag": '"v1"'})
        expected = aws_storage.DOWNLOAD_ACCESS_DENIED
        monkeypatch.setitem(app.config, "AWS_DOWNLOAD_VIA_CDN", True)
        monkeypatch.setitem(app.config, "CDN_DOMAIN", "d1.cloudfront.net")
        monkeypatch.setitem(app.config, "CDN_KEY_PAIR_ID", "KTEST")
        monkeypatch.setattr(aws_storage, "cdn_settings_problem", lambda: None)
        monkeypatch.setattr(
            aws_storage, "cdn_signed_url", lambda key, expires_in=None: "https://x"
        )
        monkeypatch.setattr(
            aws_storage, "CDN_HTTP_GET", lambda *a, **k: _FakeResponse(403)
        )
    _wire(monkeypatch, s3, sqs)
    with app.app_context():
        assert (
            aws_storage.aws_download(key, os.path.basename(key), "r1", place_at=path)
            == expected
        )
        assert _pending(file_id) is None
    assert not os.path.exists(path)


def test_a_pending_restore_again_keeps_the_state(app, monkeypatch):
    """The restored copy expired. The request again is for the same row."""

    from app import aws_storage
    from tests.test_aws_download import _client_error

    file_id, key, path = _direct_row(app, "Restore Again")
    s3 = S3(
        head={"ContentLength": 3, "StorageClass": "DEEP_ARCHIVE"},
        errors=_client_error("InvalidObjectState", 403, "GetObject"),
    )
    restored = []
    monkeypatch.setattr(
        aws_storage,
        "aws_restore",
        lambda key, **kwargs: restored.append((key, kwargs)),
    )
    _wire(monkeypatch, s3, SQS())
    with app.app_context():
        assert (
            aws_storage.aws_download(key, os.path.basename(key), "r1", place_at=path)
            == aws_storage.DOWNLOAD_RESTORE_PENDING
        )
        assert _pending(file_id) is not None
    assert restored == [(key, {"record_request": False})]


@pytest.mark.parametrize("exit", ["volume give-up", "error", "refused"])
def test_each_terminal_exit_of_the_task_clears_the_state(app, monkeypatch, exit):
    import app.videos as videos
    from app import aws_storage

    file_id, key, path = _direct_row(app, f"Task Exit {exit}")
    _wire(monkeypatch, S3(), SQS())
    kwargs = {}
    if exit == "volume give-up":
        monkeypatch.setattr(videos, "_dead_volumes", lambda paths: ["/Volumes/Movies"])
        kwargs["volume_waits"] = aws_storage.MAX_VOLUME_WAITS
    elif exit == "error":

        def broken(*args, **kwargs):
            raise RuntimeError("simulated fault")

        monkeypatch.setattr(aws_storage, "aws_download", broken)
    else:

        def refuse(*args, **kwargs):
            raise aws_storage.PlacementRefused("simulated refusal")

        monkeypatch.setattr(aws_storage, "aws_download", refuse)
    with app.app_context():
        videos.download_task(key, os.path.basename(key), "r1", **kwargs)
        assert _pending(file_id) is None


def test_the_terminal_returns_of_the_download_go_through_the_clear():
    """Each return of a terminal status ends the pending restore (#610).

    DOWNLOAD_RESTORE_PENDING is the one status that keeps it."""

    from app import aws_storage

    terminal = {
        "DOWNLOAD_COMPLETE",
        "DOWNLOAD_OBJECT_MISSING",
        "DOWNLOAD_ACCESS_DENIED",
    }
    tree = ast.parse(inspect.getsource(aws_storage))
    functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name
        in (
            "aws_download",
            "_resolve_missing_object",
            "_refuse_access_fault",
            "_defer_to_restore",
        )
    }
    assert len(functions) == 4
    checked = 0
    for name, function in functions.items():
        for node in ast.walk(function):
            if not isinstance(node, ast.Return) or node.value is None:
                continue
            names = {
                child.id
                for child in ast.walk(node.value)
                if isinstance(child, ast.Name)
            }
            cleared = (
                isinstance(node.value, ast.Call)
                and getattr(node.value.func, "id", None) == "end_restore_request"
            )
            if names & terminal:
                checked += 1
                assert cleared, f"{name} line {node.lineno} returns without the clear"
            if "DOWNLOAD_RESTORE_PENDING" in names:
                assert (
                    not cleared
                ), f"{name} line {node.lineno} clears a pending restore"
    assert checked == 3
    give_up = functions["aws_download"].body[-1]
    assert isinstance(give_up, ast.Return)
    assert give_up.value.func.id == "end_restore_request"


def test_the_sweep_clears_a_state_older_than_14_days(app, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from app import db, maintenance
    from app.models import File

    monkeypatch.setattr(maintenance, "task_send_email", lambda *a, **k: None)
    with app.app_context():
        old_id, _, old_path = _pending_file(app, "Old Request", local=False)
        new_id, _, new_path = _pending_file(app, "New Request", local=False)
        now = datetime.now(timezone.utc)
        db.session.get(File, old_id).restore_requested_at = now - timedelta(days=15)
        db.session.get(File, new_id).restore_requested_at = now - timedelta(days=13)
        db.session.commit()
    maintenance.cleanup_orphaned_files()
    with app.app_context():
        assert _pending(old_id) is None
        assert _pending(new_id) is not None


def test_the_s3_sync_requests_a_restore_and_keeps_its_key(app, monkeypatch):
    """The missing best copy gets the state, and the prune skips its key."""

    from datetime import datetime, timedelta, timezone

    from app import aws_storage

    with app.app_context():
        file_id, key, path = _pending_file(
            app, "Synced Restore", requested=False, local=False
        )
    old = datetime.now(timezone.utc) - timedelta(days=30)
    objects = {"untouched/": [{"Key": key, "Size": 5, "LastModified": old}]}
    monkeypatch.setattr(
        aws_storage,
        "get_matching_s3_objects",
        lambda bucket, prefix="": objects.get(prefix, []),
    )
    deletes = []
    monkeypatch.setattr(aws_storage, "aws_delete", lambda key: deletes.append(key))
    monkeypatch.setattr("app.email.send_email", lambda *a, **k: None)

    class SyncS3:
        restores = []

        def head_object(self, **kwargs):
            return {"ContentLength": 5, "StorageClass": "DEEP_ARCHIVE"}

        def restore_object(self, Key=None, **kwargs):
            self.restores.append(Key)

        def put_object(self, **kwargs):
            pass

    monkeypatch.setattr(aws_storage, "aws_s3_client", lambda **kw: SyncS3())
    aws_storage.sync_aws_s3_storage_task()
    assert SyncS3.restores == [key]
    assert deletes == []
    with app.app_context():
        assert _pending(file_id) is not None


def test_an_old_sidecar_with_a_stat_line_still_resumes(app, monkeypatch):
    """A sidecar from before #610 has a third line. The ETag and the size still count."""

    from app import aws_storage

    file_id, key, path = _direct_row(app, "Old Sidecar")
    staged = os.path.join(app.config["IMPORT_DIR"], f".restore-{key.replace('/', '-')}")
    os.makedirs(os.path.dirname(staged), exist_ok=True)
    with open(staged, "wb") as handle:
        handle.write(b"s3!")
    with open(f"{staged}.etag", "w") as handle:
        handle.write('"v1"\n3\n[1, 2, 3]')
    s3 = S3()
    _wire(monkeypatch, s3, SQS())
    try:
        with app.app_context():
            assert (
                aws_storage.aws_download(
                    key, os.path.basename(key), "r1", place_at=path
                )
                == aws_storage.DOWNLOAD_COMPLETE
            )
        assert s3.downloads == []
        with open(path, "rb") as handle:
            assert handle.read() == b"s3!"
    finally:
        _remove(staged, f"{staged}.etag", path)
