"""Test the pending restore state of a File row (#610).

A restore request sets File.restore_requested_at. While it is set, the
forms of the file are off, and each writer refuses. The end of the
download clears it by the row id, unless a retry or a new restore
follows. The 14-day sweep clears it too.
"""

import importlib.util
import inspect
import os
import re
from types import SimpleNamespace

import botocore.exceptions
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
    assert migration.down_revision == "8e2cf89e1536"
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


def test_the_placement_copies_under_the_title_lock_and_then_clears_the_state(
    app, monkeypatch
):
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
    import app.videos as videos
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
    statuses = []
    real_download = aws_storage.aws_download

    def download(*args, **kwargs):
        statuses.append(real_download(*args, **kwargs))
        return statuses[-1]

    monkeypatch.setattr(aws_storage, "aws_download", download)
    with app.app_context():
        assert videos.download_task(key, os.path.basename(key), "r1") is bool(expected)
        assert _pending(file_id) is None
    assert statuses == [expected]
    assert not os.path.exists(path)


@pytest.mark.parametrize("deleted", [True, False])
def test_a_pending_restore_again_keeps_the_state(app, monkeypatch, deleted):
    """The restored copy expired. The request again is for the same row.

    A failed delete of the stale message does not end the state. The
    restore is on its way."""

    import app.videos as videos
    from app import aws_storage
    from tests.test_aws_download import _client_error

    file_id, key, path = _direct_row(app, f"Restore Again {deleted}")
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
    if not deleted:
        monkeypatch.setattr(
            aws_storage, "delete_download_message", lambda *args, **kwargs: False
        )
    with app.app_context():
        assert (
            aws_storage.aws_download(key, os.path.basename(key), "r1", place_at=path)
            == aws_storage.DOWNLOAD_RESTORE_PENDING
        )
        assert videos.download_task(key, os.path.basename(key), "r1") is True
        assert _pending(file_id) is not None
    assert restored == [(key, {"record_request": False})] * 2


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


def test_a_move_during_the_download_still_ends_the_pending_restore(app, monkeypatch):
    """An archive move repoints the row during the download (#563 follow-up).

    The clear goes by the row id. A clear by the old key found no row,
    and the forms stayed off until the 14-day sweep. A gone key refuses
    the placement (#621). The person requests the restore again."""

    import botocore.exceptions

    import app.videos as videos
    from app import db
    from app.models import File

    file_id, key, path = _direct_row(app, "Moved Pending")
    moved_key = "modified/Moved Pending Later.mkv"
    gone = botocore.exceptions.ClientError(
        {"Error": {"Code": "404", "Message": "gone"}}, "HeadObject"
    )

    def head(call):
        if call == 1:
            db.session.execute(
                db.update(File).where(File.id == file_id).values(aws_key=moved_key)
            )
            db.session.commit()
        elif call == 2:
            raise gone
        return {"ContentLength": 3, "ETag": '"v1"'}

    _wire(monkeypatch, S3(head=head))
    try:
        with app.app_context():
            assert videos.download_task(key, os.path.basename(key)) is True
            assert _pending(file_id) is None
            assert db.session.get(File, file_id).aws_key == moved_key
        assert not os.path.exists(path)
    finally:
        _remove(path)


def test_the_retry_after_a_busy_lock_places_the_kept_copy(app, monkeypatch, held_lock):
    """The retry places the kept download with no second download, then clears."""

    import app.videos as videos
    from app import db
    from app.models import File
    from tests.test_scheduling import scheduled_jobs

    file_id, key, path = _direct_row(app, "Busy Then Free")
    with app.app_context():
        resource = db.session.get(File, file_id).file_identifier()
    lock = held_lock(resource)
    s3 = S3()
    _wire(monkeypatch, s3, SQS())
    try:
        with app.app_context():
            assert videos.download_task(key, os.path.basename(key), "r1") is False
            assert _pending(file_id) is not None
        app.lock_manager.unlock(lock)
        (retry,) = [
            job
            for job in scheduled_jobs(app.file_queue)
            if job.func_name == "app.videos.download_task"
        ]
        with app.app_context():
            assert videos.download_task(*retry.args, **retry.kwargs) is True
            assert _pending(file_id) is None
        assert s3.downloads == [key]
        with open(path, "rb") as handle:
            assert handle.read() == b"s3!"
    finally:
        _remove(path)


def test_the_placement_after_a_writer_that_began_first_ends_consistent(
    app, monkeypatch, held_lock
):
    """A remux held the lock at the request. It writes while the placement waits.

    Its archive is refused. The placement does not replace the newer
    file, the pending state ends, and the stale mark of the write stays
    for the sync (#615)."""

    import app.videos as videos
    from app import aws_storage, db
    from app.models import File
    from app.tracks import flag_archive_before_rewrite
    from tests.test_scheduling import scheduled_jobs

    monkeypatch.setitem(app.config, "ARCHIVE_ORIGINAL_MEDIA", True)
    file_id, key, path = _direct_row(app, "Writer First", requested=False)
    with app.app_context():
        resource = db.session.get(File, file_id).file_identifier()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(b"old local")
    s3 = S3()
    _wire(monkeypatch, s3, SQS())
    writer_lock = held_lock(resource)
    staged = os.path.join(app.config["IMPORT_DIR"], f".restore-{key.replace('/', '-')}")
    try:
        with app.app_context():
            aws_storage.mark_restore_pending(key)
            assert videos.download_task(key, os.path.basename(key), "r1") is False
            flag_archive_before_rewrite(file_id)
            with open(path, "wb") as handle:
                handle.write(b"remuxed local")
            assert (
                aws_storage.archive_library_file(db.session.get(File, file_id), path)
                is None
            )
        app.lock_manager.unlock(writer_lock)
        (retry,) = [
            job
            for job in scheduled_jobs(app.file_queue)
            if job.func_name == "app.videos.download_task"
        ]
        with app.app_context():
            videos.download_task(*retry.args, **retry.kwargs)
            assert _pending(file_id) is None
            assert db.session.get(File, file_id).aws_stale is True
        with open(path, "rb") as handle:
            assert handle.read() == b"remuxed local"
        assert not os.path.exists(staged)
        assert s3.downloads == [key]
    finally:
        _remove(path, staged, f"{staged}.etag")


def test_the_import_of_the_restored_rip_clears_the_stale_mark(app):
    """A refused archive of a stale untouched/ row keeps the mark. The rip then imports.

    The new library file is a fresh localization of the rip. A stale mark
    that stayed made the next sync upload the file to modified/ and
    delete the untouched/ rip. A refusal clears no mark (#615)."""

    from datetime import datetime

    from app import aws_storage, db, importing
    from app.models import File

    with app.app_context():
        file_id, key, path = _pending_file(app, "Refused Untouched")
        file = db.session.get(File, file_id)
        file.untouched_basename = file.basename
        file.aws_stale = True
        db.session.commit()
        assert aws_storage.archive_library_file(file, path) is None
        assert db.session.get(File, file_id).aws_stale is True
        aws_storage.end_restore_request(file_id)
        file = db.session.get(File, file_id)
        details = {
            "file_path": file.file_path,
            "basename": file.basename,
            "media_library": "Movies",
            "title": "Refused Untouched",
            "year": 1953,
            "quality_title": "Bluray-1080p",
            "untouched_basename": file.basename,
            "aws_key": key,
            "aws_date_uploaded": datetime(2026, 10, 1),
            "aws_filesize_bytes": 5,
        }
        inspection = {
            "video": {},
            "audio_tracks": [],
            "subtitle_tracks": [],
            "filesize_bytes": 5,
        }
        try:
            importing._build_records(details, inspection, file.basename)
            db.session.commit()
            db.session.expire_all()
            row = db.session.get(File, file_id)
            assert row.aws_key == key
            assert row.aws_stale is False
        finally:
            _remove(path)


@pytest.mark.parametrize("stage", ["start", "placement"])
def test_a_new_localization_ends_the_pending_restore(app, monkeypatch, stage):
    """An import that replaces the file ends the restore of its row (#620).

    The download then drops. It never puts the old archive over the new
    file, at its start or at its placement."""

    from datetime import datetime

    import app.videos as videos
    from app import db, importing
    from app.models import File

    file_id, key, path = _direct_row(app, f"Replaced {stage}")

    class ImportDuringTheDownload(S3):
        def download_file(self, *args, **kwargs):
            super().download_file(*args, **kwargs)
            if stage == "placement":
                import_now()

    s3 = ImportDuringTheDownload()
    sqs = SQS()
    _wire(monkeypatch, s3, sqs)

    def import_now():
        file = db.session.get(File, file_id)
        details = {
            "file_path": file.file_path,
            "basename": file.basename,
            "media_library": "Movies",
            "title": f"Replaced {stage}",
            "year": 1953,
            "quality_title": "Bluray-1080p",
            "untouched_basename": file.basename,
            "aws_key": key,
            "aws_date_uploaded": datetime(2026, 10, 1),
            "aws_filesize_bytes": 5,
        }
        inspection = {
            "video": {},
            "audio_tracks": [],
            "subtitle_tracks": [],
            "filesize_bytes": 5,
        }
        importing._build_records(details, inspection, file.basename)
        db.session.commit()

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(b"new import")
    try:
        with app.app_context():
            if stage == "start":
                import_now()
                assert _pending(file_id) is None
            assert videos.download_task(key, os.path.basename(key), "r1") is True
            assert _pending(file_id) is None
        with open(path, "rb") as handle:
            assert handle.read() == b"new import"
        assert sqs.deleted == ["r1"]
        assert s3.downloads == ([] if stage == "start" else [key])
    finally:
        _remove(path)


@pytest.mark.parametrize("on_demand", [False, True])
def test_the_scan_does_not_rewrite_the_file_of_a_pending_restore(
    app, monkeypatch, on_demand
):
    """The scan removes empty subtitle tracks. A pending restore keeps the file.

    The read part of the scan still runs. A rewrite marked the archive
    stale, and the placement then refused the restore. The library scan,
    the rescan after a failed edit and the page scan all reach it."""

    from app import db, tracks
    from app.models import File

    with app.app_context():
        file_id, _, path = _pending_file(app, f"Scan Writer {on_demand}")
        db.session.get(File, file_id).container = "Matroska"
        db.session.commit()

    class Info:
        tracks = [SimpleNamespace(track_type="General", format="Matroska")]

    rewrites = []
    saved = []
    monkeypatch.setattr(tracks.MediaInfo, "parse", staticmethod(lambda path: Info()))
    monkeypatch.setattr(
        tracks,
        "remove_empty_subtitle_tracks",
        lambda path, before_replace=None: rewrites.append(path) or True,
    )
    monkeypatch.setattr(tracks, "_extract_media_details", lambda path: {})
    monkeypatch.setattr(
        tracks,
        "enqueue_task",
        lambda name, file_id, details, lock, archive, **kwargs: saved.append(
            (file_id, archive)
        ),
    )

    def save(file_id, details, lock=None, archive=False):
        saved.append((file_id, archive))
        app.lock_manager.unlock(lock)
        return True

    monkeypatch.setattr(tracks, "save_track_metadata", save)
    try:
        with app.app_context():
            scan = (
                tracks.track_metadata_scan
                if on_demand
                else tracks.track_metadata_scan_task
            )
            assert scan(file_id) is True
            assert db.session.get(File, file_id).aws_stale is False
        assert rewrites == []
        assert saved == [(file_id, False)]
    finally:
        _remove(path)


def test_the_sweep_reads_a_naive_stamp_as_utc(app):
    """MariaDB returns naive datetimes. A naive stamp of 3 days ago stays."""

    from datetime import datetime, timedelta, timezone

    from app import db
    from app.aws_storage import expire_restore_requests
    from app.models import File

    with app.app_context():
        file_id, _, _ = _pending_file(app, "Naive Stamp", local=False)
        stamp = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=3)
        db.session.get(File, file_id).restore_requested_at = stamp
        db.session.commit()
        expire_restore_requests(14)
        assert _pending(file_id) is not None


def test_a_fault_at_the_lock_release_still_ends_a_placed_restore(app, monkeypatch):
    """A Redis fault at the release of the title lock does not fail the restore.

    The copy is in place, and the state ends. The lock expires by itself."""

    import app.videos as videos

    file_id, key, path = _direct_row(app, "Unlock Fault")
    _wire(monkeypatch, S3(), SQS())
    real_unlock = app.lock_manager.unlock
    faults = []

    def unlock(lock):
        real_unlock(lock)
        if not faults:
            faults.append(lock)
            raise RuntimeError("simulated Redis fault at the unlock")

    monkeypatch.setattr(app.lock_manager, "unlock", unlock)
    try:
        with app.app_context():
            assert videos.download_task(key, os.path.basename(key), "r1") is True
            assert _pending(file_id) is None
        assert len(faults) == 1
        with open(path, "rb") as handle:
            assert handle.read() == b"s3!"
    finally:
        _remove(path)


def test_the_page_stamps_the_row_before_the_redirect(app, admin_client, monkeypatch):
    """The forms are disabled on the page the restore request redirects to (#613)."""

    from app import aws_storage
    from tests.conftest import ADMIN_PASSWORD, csrf_token_from

    queued = []
    monkeypatch.setattr(
        aws_storage, "enqueue_task", lambda name, *args, **kwargs: queued.append(name)
    )
    with app.app_context():
        file_id, _, path = _pending_file(app, "Stamped At The Click", requested=False)
    try:
        page = admin_client.get(f"/file/{file_id}").get_data(as_text=True)
        assert "disabled" not in _fieldset_of(page, "s3_upload_submit")
        response = admin_client.post(
            f"/file/{file_id}",
            data={
                "csrf_token": csrf_token_from(page),
                "password": ADMIN_PASSWORD,
                "s3_download_submit": "Restore",
            },
            follow_redirects=True,
        )
        page = response.get_data(as_text=True)
        assert "to be restored from AWS" in page
        assert "are disabled until the restore ends" in page
        for field in ("s3_upload_submit", "s3_download_submit"):
            assert "disabled" in _fieldset_of(page, field)
        # The worker has not run. The page stamped the row itself.
        assert queued == ["app.videos.aws_restore"]
        with app.app_context():
            assert _pending(file_id) is not None
    finally:
        _remove(path)


class _RefusingS3:
    """An S3 client whose restore request cannot be made."""

    def __init__(self, fault):
        self.fault = fault

    def head_object(self, **kwargs):
        if self.fault == "missing":
            raise botocore.exceptions.ClientError(
                {"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
                "HeadObject",
            )
        return {"StorageClass": "DEEP_ARCHIVE"}

    def restore_object(self, **kwargs):
        if self.fault == "refused":
            raise botocore.exceptions.ClientError(
                {"Error": {"Code": "AccessDenied"}}, "RestoreObject"
            )
        if self.fault == "in-progress":
            raise botocore.exceptions.ClientError(
                {"Error": {"Code": "RestoreAlreadyInProgress"}}, "RestoreObject"
            )
        raise OSError("endpoint unreachable")


@pytest.mark.parametrize("fault", ["missing", "refused", "unreachable", "in-progress"])
def test_a_request_that_cannot_be_made_clears_the_state(app, monkeypatch, fault):
    """A stamp from the page ends when S3 cannot take the request (#613)."""

    from app import aws_storage

    monkeypatch.setattr(
        aws_storage, "aws_s3_client", lambda **kwargs: _RefusingS3(fault)
    )
    with app.app_context():
        file_id, key, path = _pending_file(app, f"Refused Request {fault}", local=False)
        try:
            if fault in ("refused", "unreachable"):
                with pytest.raises(Exception):
                    aws_storage.aws_restore(key)
            else:
                aws_storage.aws_restore(key)
            # A request that is already in progress keeps the stamp.
            expected_pending = fault == "in-progress"
            assert (_pending(file_id) is not None) is expected_pending
        finally:
            _remove(path)


def test_a_move_request_that_cannot_be_made_keeps_the_rows(app, monkeypatch):
    """record_request=False never touches the state (#613)."""

    from app import aws_storage

    monkeypatch.setattr(
        aws_storage, "aws_s3_client", lambda **kwargs: _RefusingS3("missing")
    )
    with app.app_context():
        file_id, key, path = _pending_file(app, "Move Request Missing", local=False)
        try:
            aws_storage.aws_restore(key, record_request=False)
            assert _pending(file_id) is not None
        finally:
            _remove(path)
