"""Test that a same-name re-rip never replaces a claimed untouched archive (#430).

A surviving record can claim untouched/<name> for the bytes of a good
rip. A re-rip with the same name and different bytes uploads to a
run-scoped key first. A failed import deletes that upload. A finished
import moves it to the final key after the records commit."""

import os
from datetime import datetime, timedelta, timezone

BASENAME = "Rerip Guard (2021) - [DVD].mkv"
KEY = f"untouched/{BASENAME}"


def _claimed_rip(app, monkeypatch, title="Rerip Guard"):
    """Make a record that claims KEY, and a fake S3 that holds its old object."""

    from app import aws_storage, db
    from tests.factories import make_movie, make_movie_file
    from tests.fakes import ArchiveS3

    fake = ArchiveS3(
        {
            KEY: {
                "ContentLength": 100,
                "ETag": '"old-rip"',
                "StorageClass": "DEEP_ARCHIVE",
                "VersionId": "v-old",
            }
        }
    )
    monkeypatch.setattr(aws_storage, "aws_s3_client", lambda **kwargs: fake)
    monkeypatch.setitem(app.config, "AWS_BUCKET", "test-bucket")
    monkeypatch.setitem(app.config, "ARCHIVE_ORIGINAL_MEDIA", True)
    with app.app_context():
        row = make_movie_file(
            make_movie(title, 2021),
            "DVD",
            aws_key=KEY,
            aws_date_uploaded=datetime.now(timezone.utc) - timedelta(days=30),
            aws_filesize_bytes=100,
            untouched_basename=BASENAME,
        )
        db.session.commit()
        return fake, row.id


def _incoming(sample_mkv, incoming_dir):
    source = os.path.join(incoming_dir, BASENAME)
    os.makedirs(incoming_dir, exist_ok=True)
    with open(sample_mkv, "rb") as f_in, open(source, "wb") as f_out:
        f_out.write(f_in.read())
    return source


def _pending_keys(fake):
    return [key for key in fake.uploads if key.startswith("pending-untouched/")]


def test_a_failed_rerip_leaves_the_claimed_archive_current(
    app, sample_mkv, incoming_dir, monkeypatch
):
    """The localization fails after the upload. The old rip stays the current object."""

    from app import db, importing
    from app.models import File

    fake, row_id = _claimed_rip(app, monkeypatch)
    source = _incoming(sample_mkv, incoming_dir)

    def broken_localize(*args, **kwargs):
        raise RuntimeError("mkvmerge failed")

    monkeypatch.setattr(importing, "_localize", broken_localize)

    with app.app_context():
        assert importing.localization_task(source) is True

        assert fake.objects[KEY]["ETag"] == '"old-rip"'
        assert fake.objects[KEY]["VersionId"] == "v-old"
        assert KEY not in fake.noncurrent
        pending = _pending_keys(fake)
        assert len(pending) == 1
        # Each version of the run-scoped upload is gone, not only hidden
        # behind a delete marker.
        assert pending[0] not in fake.objects
        assert not fake.noncurrent.get(pending[0])
        assert not fake.markers.get(pending[0])
        assert db.session.get(File, row_id).aws_key == KEY


def test_a_finished_rerip_moves_its_upload_to_the_final_key(
    app, sample_mkv, incoming_dir, monkeypatch
):
    """The records commit with the run-scoped key. Then the move takes the final key."""

    from app import aws_storage, db
    from app.models import File
    from tests.localization_helpers import run_full_chain

    fake, row_id = _claimed_rip(app, monkeypatch, title="Rerip Guard")
    source = _incoming(sample_mkv, incoming_dir)

    with app.app_context():
        run_full_chain(app, source)

        row = db.session.get(File, row_id)
        pending = _pending_keys(fake)
        assert len(pending) == 1
        assert row.aws_key == pending[0]
        assert row.aws_key_target == KEY
        # Nothing replaced the old rip before the commit.
        assert fake.objects[KEY]["ETag"] == '"old-rip"'
        # The target claims the old key. Thus, no delete of it is queued.
        assert not [
            job
            for job in app.handoff_queue.jobs
            if job.func_name == "app.videos.aws_delete" and job.args == (KEY,)
        ]

        # The upload stays in S3 Standard. The move needs no restore.
        assert fake.objects[pending[0]]["StorageClass"] == "STANDARD"
        new_etag = fake.objects[pending[0]]["ETag"]
        assert aws_storage.move_archive_object(row_id) is True
        assert fake.restores == []

        db.session.expire_all()
        row = db.session.get(File, row_id)
        assert row.aws_key == KEY
        assert row.aws_key_target is None
        assert fake.objects[KEY]["ETag"] == new_etag
        assert pending[0] not in fake.objects
        assert not fake.noncurrent.get(pending[0])
        # The old rip stays as a noncurrent version for the lifecycle window.
        assert [head["VersionId"] for head in fake.noncurrent[KEY]] == ["v-old"]


def test_an_unclaimed_key_with_other_bytes_is_replaced_in_place(
    app, monkeypatch, tmp_path
):
    """No record claims the key. Then the upload goes to the normal key, as before."""

    from app import aws_storage
    from tests.fakes import ArchiveS3

    fake = ArchiveS3(
        {KEY: {"ContentLength": 3, "ETag": '"stale"', "VersionId": "v-stale"}}
    )
    monkeypatch.setattr(aws_storage, "aws_s3_client", lambda **kwargs: fake)
    monkeypatch.setitem(app.config, "AWS_BUCKET", "test-bucket")
    path = tmp_path / BASENAME
    path.write_bytes(b"new rip")
    with app.app_context():
        key, _, size = aws_storage.upload_untouched(
            str(path), lambda claimed: "unused/" + claimed
        )
    assert key == KEY and size == 7
    assert fake.uploads == [KEY]


def test_a_held_archive_never_replaces_a_claimed_rip(app, monkeypatch, tmp_path):
    """The "Archive only" choice uploads a same-name HDR rip to a key of its own."""

    import json

    from app import db
    from app.models import ArchivedOriginal, HeldImport
    from app.sdr_conversion import archive_held_task

    fake, _ = _claimed_rip(app, monkeypatch, title="Rerip Guard Held")
    held_path = tmp_path / BASENAME
    held_path.write_bytes(b"hdr rip bytes")
    with app.app_context():
        held = HeldImport(
            held_path=str(held_path),
            untouched_basename=BASENAME,
            file_details=json.dumps(
                {
                    "media_library": "Movies",
                    "title": "Rerip Guard Held",
                    "year": 2021,
                    "quality_title": "DVD",
                }
            ),
            source_format="HDR10",
            filesize_bytes=13,
        )
        db.session.add(held)
        db.session.commit()
        held_id = held.id

        assert archive_held_task(held_id) is True

        own = f"untouched/Rerip Guard (2021) - [DVD] (held {held_id}).mkv"
        assert fake.objects[KEY]["ETag"] == '"old-rip"'
        assert own in fake.objects
        assert ArchivedOriginal.query.filter_by(aws_key=own).first() is not None


def test_the_sync_warns_when_a_claimed_object_changed_after_its_upload(app, caplog):
    """A new size with a later object date keeps the recorded size and warns."""

    from app.aws_storage import _replaced_after_upload

    class Row:
        aws_key = KEY
        basename = BASENAME
        aws_filesize_bytes = 100
        aws_date_uploaded = datetime(2026, 9, 1, 12, 0)

    with app.app_context():
        later = datetime(2026, 10, 1, tzinfo=timezone.utc)
        earlier = datetime(2026, 8, 31, tzinfo=timezone.utc)
        assert _replaced_after_upload(Row(), 55, later) is True
        assert "changed after the upload" in caplog.text
        assert _replaced_after_upload(Row(), 55, earlier) is False


def test_the_pending_uploads_live_outside_untouched(app, monkeypatch):
    """The run-scoped key is outside untouched/ and its Deep Archive rule (follow-up #430).

    The weekly sync lists the prefix. Thus, it prunes a leftover upload
    with every version, and a claim on it does not read as dangling."""

    from app import aws_storage, s3_layout

    with app.app_context():
        key = aws_storage.pending_archive_key(KEY, "run1")
        assert key == f"pending-untouched/run1/{BASENAME}"
        assert not key.startswith("untouched/")
        entry = s3_layout.prefix_of(key)
        assert entry.name == "pending-untouched"
        assert entry.lifecycle == "scratch" and entry.delete == "versions"
        assert entry.current_days == 60
        assert aws_storage._archive_prefix_of(key) == "untouched"

        listed = []
        monkeypatch.setattr(
            aws_storage,
            "get_matching_s3_objects",
            lambda bucket, prefix="": listed.append(prefix) or [],
        )
        aws_storage._list_archives()
        assert "pending-untouched/" in listed


def _drop_pending_before_the_follow_up(monkeypatch, fake):
    """Delete the run-scoped upload just before the archive key step."""

    from app import importing

    real = importing._request_final_archive_key

    def dropped(file, source_path=None):
        fake.objects.pop(file.aws_key, None)
        return real(file, source_path)

    monkeypatch.setattr(importing, "_request_final_archive_key", dropped)


def test_a_missing_pending_upload_goes_up_again_before_the_source_goes(
    app, sample_mkv, incoming_dir, monkeypatch
):
    """The pending object expired. The source uploads again first (#494)."""

    from app import db
    from app.models import File
    from tests.localization_helpers import run_full_chain

    fake, row_id = _claimed_rip(app, monkeypatch)
    source = _incoming(sample_mkv, incoming_dir)
    _drop_pending_before_the_follow_up(monkeypatch, fake)

    with app.app_context():
        run_full_chain(app, source)

        row = db.session.get(File, row_id)
        pending = _pending_keys(fake)
        assert len(pending) == 2 and pending[0] == pending[1]
        assert row.aws_key == pending[0]
        assert pending[0] in fake.objects
        assert not os.path.exists(source)


def test_a_failed_new_upload_keeps_the_source_in_rejects(
    app, sample_mkv, incoming_dir, monkeypatch
):
    """The pending object is gone and the new upload fails. The rip stays."""

    from tests.localization_helpers import run_full_chain

    fake, _ = _claimed_rip(app, monkeypatch)
    source = _incoming(sample_mkv, incoming_dir)
    _drop_pending_before_the_follow_up(monkeypatch, fake)
    real_upload = fake.upload_file

    def upload_once(Filename, Bucket, Key, **kwargs):
        if _pending_keys(fake):
            raise OSError("network down")
        return real_upload(Filename, Bucket, Key, **kwargs)

    monkeypatch.setattr(fake, "upload_file", upload_once)

    with app.app_context():
        run_full_chain(app, source)

        kept = os.path.join(app.config["REJECTS_DIR"], "unarchived", BASENAME)
        assert not os.path.exists(source)
        assert os.path.exists(kept)
