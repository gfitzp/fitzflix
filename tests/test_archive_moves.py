"""Test the archive moves: the archive key follows the untouched name (#369).

A rename requests a move. The move job copies the S3 object on the
server side, verifies the copy, and only then writes the new key. A
Deep Archive object first gets a Bulk restore. Its notification brings
the move back. aws_key names a real object at every moment."""

import logging
import os
from datetime import datetime, timedelta, timezone

import pytest

from tests.factories import make_movie, make_movie_file, make_tv_file, make_tv_series
from tests.fakes import SQS, ArchiveS3

ETAG = '"0123456789abcdef0123456789abcdef-3"'
SIZE = 20 * 1024 * 1024


def readable(**extra):
    """Return the head of a readable multipart object."""

    return {
        "ContentLength": SIZE,
        "ETag": ETAG,
        "StorageClass": "STANDARD",
        "VersionId": "old-version",
        **extra,
    }


def frozen(**extra):
    """Return the head of a Deep Archive object with no restored copy."""

    return readable(StorageClass="DEEP_ARCHIVE", **extra)


@pytest.fixture
def s3(app, monkeypatch):
    """Install an ArchiveS3 and an SQS fake. Return a function that sets the objects."""

    from app import aws_storage

    holder = {}

    def install(objects=None, **kwargs):
        holder["s3"] = ArchiveS3(objects, **kwargs)
        return holder["s3"]

    install()
    holder["sqs"] = SQS()
    monkeypatch.setattr(aws_storage, "aws_s3_client", lambda **kw: holder["s3"])
    monkeypatch.setattr(aws_storage, "aws_sqs_client", lambda: holder["sqs"])
    install.sqs = holder["sqs"]
    return install


def archived_film(title, year, quality="Bluray-1080p", key_name=None, **kwargs):
    """Make a film file whose untouched/ archive has the key of its name."""

    from app import db

    movie = make_movie(title, year)
    file = make_movie_file(movie, quality, **kwargs)
    file.untouched_basename = file.basename
    file.aws_key = f"untouched/{key_name or file.basename}"
    file.aws_date_uploaded = datetime(2026, 1, 1)
    file.aws_filesize_bytes = SIZE
    db.session.commit()
    return file


def move_jobs(app):
    return [
        job
        for job in app.file_queue.jobs
        if job.func_name == "app.aws_storage.move_archive_object"
    ]


def committed_key(app, file_id, column="aws_key"):
    """Read a key column of a row through a second connection."""

    from app import db

    with db.engine.connect() as connection:
        return connection.execute(
            db.text(f"SELECT {column} FROM file WHERE id = :id"), {"id": file_id}
        ).scalar()


def test_a_readable_archive_moves_and_the_row_changes_last(app, s3):
    """Copy, verify, row update, then the delete of the old key."""

    from app import aws_storage, db
    from app.models import File

    with app.app_context():
        file = archived_film("Moving Picture", 1950, key_name="Old Name (1950).mkv")
        file_id = file.id
        old_key = file.aws_key
        new_key = "untouched/Moving Picture (1950) - [Bluray-1080p].mkv"

        seen = []
        at_delete = []
        fake = s3(
            {old_key: readable()},
            on_copy=lambda source, target: seen.append(committed_key(app, file_id)),
            on_delete=lambda key: at_delete.append((key, committed_key(app, file_id))),
        )

        assert aws_storage.request_archive_move(file) is True
        assert file.aws_key_target == new_key
        assert move_jobs(app) == []
        db.session.commit()
        assert [job.args for job in move_jobs(app)] == [(file_id,)]

        assert aws_storage.move_archive_object(file_id) is True

        # During the copy, the row still names the old object.
        assert seen == [old_key]
        assert fake.copies == [(old_key, new_key)]
        config = fake.configs[0]
        assert config.multipart_chunksize == fake.part_size
        assert fake.events.index(("head", new_key)) > fake.events.index(
            ("copy", old_key, new_key)
        )

        # The row names the new key before the old key goes. The delete
        # is a plain delete. The old version stays for 180 days.
        assert at_delete == [(old_key, new_key)]
        assert fake.deletes == [(old_key, None)]
        db.session.expire_all()
        moved = db.session.get(File, file_id)
        assert moved.aws_key == new_key
        assert moved.aws_key_target is None
        assert new_key in fake.objects and old_key not in fake.objects


def test_a_copy_that_differs_is_deleted_and_the_row_keeps_its_key(app, s3, caplog):
    from app import aws_storage, db
    from app.models import File

    with app.app_context():
        file = archived_film("Bad Copy", 1951, key_name="Bad (1951).mkv")
        file_id = file.id
        old_key = file.aws_key
        fake = s3({old_key: readable()}, copy_etag='"ffff-3"')
        aws_storage.request_archive_move(file)
        db.session.commit()

        # The job fails loudly, for the FailedJobRegistry. Only the own
        # copy goes, by its version id.
        with pytest.raises(RuntimeError, match="has the ETag"):
            aws_storage.move_archive_object(file_id)
        target = "untouched/Bad Copy (1951) - [Bluray-1080p].mkv"
        assert fake.deletes == [(target, "copy-1")]
        db.session.expire_all()
        assert db.session.get(File, file_id).aws_key == old_key
        assert db.session.get(File, file_id).aws_key_target == target

    # The backfill requests the stranded move again with --again. A
    # plain --apply skips a pending move (#384).
    for job in move_jobs(app):
        job.delete()
    from app.cli import register

    if "aws" not in app.cli.commands:
        register(app)
    runner = app.test_cli_runner()
    result = runner.invoke(args=["aws", "converge-keys", "--apply"])
    assert "Requested 0 archive move(s)" in result.output
    result = runner.invoke(args=["aws", "converge-keys", "--apply", "--again"])
    assert "Requested 1 archive move(s)" in result.output
    with app.app_context():
        assert [job.args for job in move_jobs(app)] == [(file_id,)]


def test_a_deep_archive_object_gets_a_bulk_restore_and_the_row_stays(app, s3):
    from app import aws_storage, db
    from app.models import File

    with app.app_context():
        file = archived_film("Frozen Reel", 1952, key_name="Frozen (1952).mkv")
        file_id = file.id
        old_key = file.aws_key
        fake = s3({old_key: frozen()})
        aws_storage.request_archive_move(file)
        db.session.commit()

        assert aws_storage.move_archive_object(file_id) == aws_storage.MOVE_WAITING
        assert fake.restores == [(old_key, "Bulk")]
        assert fake.copies == [] and fake.deletes == []

        # record_request=False: a download must not think that a person
        # asked for this restore. The row gets no pending restore (#610).
        db.session.expire_all()
        row = db.session.get(File, file_id)
        assert row.restore_requested_at is None
        assert row.aws_key == old_key
        assert row.aws_key_target == "untouched/Frozen Reel (1952) - [Bluray-1080p].mkv"


def test_an_expired_restore_is_requested_again(app, s3):
    from app import aws_storage, db

    expired = 'ongoing-request="false", expiry-date="Fri, 01 Jan 2021 00:00:00 GMT"'
    with app.app_context():
        file = archived_film("Thawed Once", 1953, key_name="Thawed (1953).mkv")
        old_key = file.aws_key
        fake = s3({old_key: frozen(Restore=expired)})
        aws_storage.request_archive_move(file)
        db.session.commit()

        result = aws_storage.move_archive_object(file.id, sqs_receipt_handle="stale")
        assert result == aws_storage.MOVE_WAITING
        assert fake.restores == [(old_key, "Bulk")]
        assert fake.copies == []
        assert s3.sqs.deleted == ["stale"]


def test_a_missing_object_clears_the_target_with_a_warning(app, s3, caplog):
    from app import aws_storage, db
    from app.models import File

    with app.app_context():
        file = archived_film("Lost Reel", 1954, key_name="Lost (1954).mkv")
        file_id = file.id
        s3({})
        aws_storage.request_archive_move(file)
        db.session.commit()

        with caplog.at_level(logging.WARNING):
            assert aws_storage.move_archive_object(file_id) is False
        assert "is not in S3" in caplog.text
        db.session.expire_all()
        row = db.session.get(File, file_id)
        assert row.aws_key_target is None
        assert row.aws_key == "untouched/Lost (1954).mkv"


def test_a_second_rename_overwrites_the_target_and_the_job_takes_the_newest(app, s3):
    from app import aws_storage, db
    from app.models import File

    with app.app_context():
        file = archived_film("First Title", 1955)
        file_id = file.id
        old_key = file.aws_key
        fake = s3({old_key: readable()})

        file.untouched_basename = "Second Title (1955) - [Bluray-1080p].mkv"
        aws_storage.request_archive_move(file)
        db.session.commit()
        assert (
            file.aws_key_target == "untouched/Second Title (1955) - [Bluray-1080p].mkv"
        )

        file.untouched_basename = "Third Title (1955) - [Bluray-1080p].mkv"
        aws_storage.request_archive_move(file)
        db.session.commit()
        newest = "untouched/Third Title (1955) - [Bluray-1080p].mkv"
        assert file.aws_key_target == newest

        # A later rename that requests nothing still wins. The job
        # computes the key from the row.
        file.untouched_basename = "Fourth Title (1955) - [Bluray-1080p].mkv"
        db.session.commit()

        assert aws_storage.move_archive_object(file_id) is True
        fourth = "untouched/Fourth Title (1955) - [Bluray-1080p].mkv"
        assert fake.copies == [(old_key, fourth)]
        db.session.expire_all()
        assert db.session.get(File, file_id).aws_key == fourth


def test_a_rename_back_clears_the_target(app, s3):
    from app import aws_storage

    with app.app_context():
        file = archived_film("Round Trip", 1956)
        original = file.untouched_basename
        file.untouched_basename = "Away (1956) - [Bluray-1080p].mkv"
        assert aws_storage.request_archive_move(file) is True
        file.untouched_basename = original
        assert aws_storage.request_archive_move(file) is False
        assert file.aws_key_target is None


def test_the_poll_routes_a_restored_key_with_a_target_to_the_move(app, s3, monkeypatch):
    """A restore-ready notification of a pending move goes to the move job."""

    import json

    from app import aws_storage, db

    class Poll:
        def __init__(self, keys):
            self.messages = [
                {
                    "Body": json.dumps({"Records": [{"s3": {"object": {"key": key}}}]}),
                    "ReceiptHandle": f"handle-{n}",
                }
                for n, key in enumerate(keys)
            ]
            self.deleted = []

        def receive_message(self, **kwargs):
            return {"Messages": [self.messages.pop(0)]} if self.messages else {}

        def delete_message(self, QueueUrl=None, ReceiptHandle=None):
            self.deleted.append(ReceiptHandle)

        def change_message_visibility(self, **kwargs):
            pass

    with app.app_context():
        file = archived_film("Restored Move", 1957, key_name="Restored (1957).mkv")
        file_id = file.id
        old_key = file.aws_key
        aws_storage.request_archive_move(file)
        db.session.commit()
        for job in move_jobs(app):
            job.delete()

    poll = Poll([old_key, "untouched/Nobody Claims (1900).mkv"])
    monkeypatch.setattr(aws_storage, "aws_sqs_client", lambda: poll)
    assert aws_storage.sqs_retrieve_task() is True

    with app.app_context():
        downloads = [
            job
            for job in app.file_queue.jobs
            if job.func_name == "app.videos.download_task"
        ]
        assert downloads == []
        (job,) = move_jobs(app)
        assert job.args == (file_id,)
        assert job.kwargs == {"sqs_receipt_handle": "handle-0"}
        assert job.meta["archive_key"] == old_key

        # The key that no record claims: its message goes, with no job.
        assert poll.deleted == ["handle-1"]

        # The move deletes its message when it is done.
        fake = s3(
            {
                old_key: frozen(
                    Restore='ongoing-request="false", '
                    'expiry-date="Fri, 01 Jan 2100 00:00:00 GMT"'
                )
            }
        )
        assert aws_storage.move_archive_object(file_id, "handle-0") is True
        assert fake.copies == [
            (old_key, "untouched/Restored Move (1957) - [Bluray-1080p].mkv")
        ]
        assert poll.deleted == ["handle-1", "handle-0"]


def test_the_poll_extends_the_restore_of_a_queued_move(app, s3, monkeypatch):
    from app import aws_storage, db

    class Poll:
        def __init__(self):
            self.visibility = []

        def receive_message(self, **kwargs):
            return {}

        def change_message_visibility(self, ReceiptHandle=None, **kwargs):
            self.visibility.append(ReceiptHandle)

    with app.app_context():
        file = archived_film("Waiting Move", 1958, key_name="Waiting (1958).mkv")
        aws_storage.request_archive_move(file)
        db.session.commit()
        for job in move_jobs(app):
            job.delete()
        app.file_queue.enqueue(
            "app.aws_storage.move_archive_object",
            args=(file.id,),
            kwargs={"sqs_receipt_handle": "held"},
        )
        old_key = file.aws_key

        # The poll recorded the handle under the key at the receive (#617).
        aws_storage.record_receipt_handle(old_key, "held")

    poll = Poll()
    fake = s3({old_key: frozen()})
    monkeypatch.setattr(aws_storage, "aws_sqs_client", lambda: poll)
    assert aws_storage.sqs_retrieve_task() is True
    assert poll.visibility == ["held"]
    assert fake.restores == [(old_key, "Standard")]


def test_a_move_waits_for_a_download_of_the_old_key(app, s3):
    from app import aws_storage, db

    with app.app_context():
        file = archived_film("Busy Reel", 1959, key_name="Busy (1959).mkv")
        aws_storage.request_archive_move(file)
        db.session.commit()
        app.file_queue.enqueue(
            "app.videos.download_task", args=(file.aws_key, "Busy (1959).mkv")
        )
        fake = s3({file.aws_key: readable()})

        assert aws_storage.move_archive_object(file.id) == aws_storage.MOVE_WAITING
        assert fake.copies == []


def test_a_move_that_waits_books_a_random_retry_delay(app, monkeypatch):
    """Test that the retry delay of a waiting move is not a fixed period (#404).

    A fixed period can stay aligned with the 5-minute retry of the sync."""

    from datetime import timedelta

    from app import aws_storage, db

    delays = []
    monkeypatch.setattr(
        aws_storage, "requeue", lambda *args, **kwargs: delays.append(kwargs["delay"])
    )
    with app.app_context():
        file = archived_film("Random Reel", 1960, key_name="Random (1960).mkv")
        db.session.commit()
        for _ in range(20):
            result = aws_storage._wait_move(file, file.aws_key, None, "the S3 sync")
            assert result == aws_storage.MOVE_WAITING

    assert all(timedelta(minutes=25) <= d <= timedelta(minutes=35) for d in delays)
    assert len(set(delays)) > 1


def test_a_move_goes_on_when_the_sync_stops_after_the_pause(app, s3, monkeypatch):
    """Test that a sync that stops at once does not hold the move (#404)."""

    from app import aws_storage, db

    answers = iter([True, False])
    pauses = []
    monkeypatch.setattr(aws_storage, "_sync_is_running", lambda: next(answers))
    monkeypatch.setattr(aws_storage.time, "sleep", pauses.append)
    with app.app_context():
        file = archived_film("Quick Sync", 1961, key_name="Quick (1961).mkv")
        aws_storage.request_archive_move(file)
        db.session.commit()
        fake = s3({file.aws_key: readable()})

        assert aws_storage.move_archive_object(file.id) != aws_storage.MOVE_WAITING
        assert fake.copies

    assert pauses == [aws_storage.SYNC_RECHECK_SECONDS]


def test_a_move_waits_while_the_sync_still_runs_after_the_pause(app, s3, monkeypatch):
    """The move pauses and checks again before it books its wait (#404)."""

    from app import aws_storage, db

    events = []

    def sync_runs():
        events.append("check")
        return True

    monkeypatch.setattr(aws_storage, "_sync_is_running", sync_runs)
    monkeypatch.setattr(
        aws_storage.time, "sleep", lambda seconds: events.append(("sleep", seconds))
    )
    real_wait = aws_storage._wait_move

    def recorded_wait(*args, **kwargs):
        events.append("wait")
        return real_wait(*args, **kwargs)

    monkeypatch.setattr(aws_storage, "_wait_move", recorded_wait)
    with app.app_context():
        file = archived_film("Slow Sync", 1962, key_name="Slow (1962).mkv")
        aws_storage.request_archive_move(file)
        db.session.commit()
        fake = s3({file.aws_key: readable()})

        assert aws_storage.move_archive_object(file.id) == aws_storage.MOVE_WAITING
        assert fake.copies == []

    assert events == [
        "check",
        ("sleep", aws_storage.SYNC_RECHECK_SECONDS),
        "check",
        "wait",
    ]


def test_an_edit_during_an_untouched_move_goes_to_modified_under_the_new_name(
    app, monkeypatch
):
    from app import aws_storage, db

    uploads = []

    def fake_upload(path, key_prefix="", key_name=None, **kwargs):
        uploads.append((key_prefix, key_name))
        return os.path.join(key_prefix, key_name), datetime.utcnow(), 7

    monkeypatch.setattr(aws_storage, "aws_upload", fake_upload)
    with app.app_context():
        file = archived_film("Edited Rip", 1960, key_name="Edited (1960).mkv")
        old_key = file.aws_key
        aws_storage.request_archive_move(file)
        db.session.commit()

        aws_storage.archive_library_file(file, "/library/file.mkv")
        assert uploads == [("modified", "Edited Rip (1960) - [Bluray-1080p].mkv")]
        assert file.aws_key == "modified/Edited Rip (1960) - [Bluray-1080p].mkv"
        assert file.aws_key_target is None
        assert [
            job.args[0]
            for job in app.handoff_queue.jobs
            if job.func_name == "app.videos.aws_delete"
        ] == [old_key]


def test_an_edit_during_a_modified_move_uploads_under_the_target(app, monkeypatch):
    from app import aws_storage, db

    uploads = []

    def fake_upload(path, key_prefix="", key_name=None, **kwargs):
        uploads.append((key_prefix, key_name))
        return os.path.join(key_prefix, key_name), datetime.utcnow(), 7

    monkeypatch.setattr(aws_storage, "aws_upload", fake_upload)
    with app.app_context():
        file = archived_film("Edited Twice", 1961)
        file.aws_key = "modified/Old Edited Name (1961).mkv"
        db.session.commit()
        aws_storage.request_archive_move(file)
        db.session.commit()
        target = "modified/Edited Twice (1961) - [Bluray-1080p].mkv"
        assert file.aws_key_target == target

        aws_storage.archive_library_file(file, "/library/file.mkv")
        assert uploads == [("modified", os.path.basename(target))]
        assert file.aws_key == target
        assert file.aws_key_target is None
        assert [
            job.args[0]
            for job in app.handoff_queue.jobs
            if job.func_name == "app.videos.aws_delete"
        ] == ["modified/Old Edited Name (1961).mkv"]

        # Without a pending move, a later edit overwrites the same key.
        aws_storage.archive_library_file(file, "/library/file.mkv")
        assert uploads[-1] == ("modified", os.path.basename(target))


def test_the_claims_include_the_pending_targets(app):
    from app import aws_storage, db

    with app.app_context():
        file = archived_film("Claimed Target", 1962, key_name="Claimed (1962).mkv")
        aws_storage.request_archive_move(file)
        db.session.commit()
        target = file.aws_key_target

        assert target in aws_storage._claimed_keys()
        assert aws_storage.untouched_key_still_claimed(target) is True
        assert aws_storage.untouched_key_still_claimed("untouched/Nobody.mkv") is False

        # The rule gives another record with the same name the record id.
        other = make_movie_file(
            make_movie("Claimed Target", 1963),
            "Bluray-1080p",
            untouched_basename=file.untouched_basename,
        )
        db.session.commit()
        assert aws_storage.archive_key_for(other, "untouched") == (
            f"untouched/Claimed Target (1962) - [Bluray-1080p] ({other.id}).mkv"
        )


def test_the_series_rename_requests_a_move_for_each_tv_file(app, s3):
    from app import aws_storage, db
    from app.models import File
    from app.series_rename import rename_tv_series_task

    with app.app_context():
        series = make_tv_series("Batman", tmdb_id=2287)
        files = [make_tv_file(series, 1, n, "Bluray-1080p") for n in (1, 2)]
        for file in files:
            file.untouched_basename = file.basename
            file.aws_key = f"untouched/{file.basename}"
            file.aws_date_uploaded = datetime(2026, 1, 1)
        db.session.commit()
        ids = [file.id for file in files]
        old_keys = [file.aws_key for file in files]

        assert rename_tv_series_task(series.id, "Batman (1966)") is True
        assert sorted(job.args[0] for job in move_jobs(app)) == sorted(ids)
        db.session.expire_all()
        for file_id, old_key in zip(ids, old_keys):
            row = db.session.get(File, file_id)
            assert row.aws_key == old_key
            assert row.aws_key_target == f"untouched/{row.untouched_basename}"
            assert row.aws_key_target.startswith("untouched/Batman (1966) - S01E0")

        # The moves complete the rule.
        fake = s3({key: readable() for key in old_keys})
        for file_id in ids:
            assert aws_storage.move_archive_object(file_id) is True
        db.session.expire_all()
        for file_id in ids:
            row = db.session.get(File, file_id)
            assert row.aws_key == aws_storage.archive_key_for(row, "untouched")
        assert len(fake.copies) == 2


def test_the_series_rename_guard_requests_no_move(app, monkeypatch):
    """A file without the series title stops the rename before any S3 work (#317)."""

    import app.series_rename as series_rename
    from app import db
    from app.models import File

    monkeypatch.setattr(series_rename, "_update_sonarr_path", lambda *a: None)
    with app.app_context():
        series = make_tv_series("Odd Archive Show")
        good = make_tv_file(series, 1, 1, "WEBDL-1080p")
        good.untouched_basename = good.basename
        good.aws_key = f"untouched/{good.basename}"
        good.aws_date_uploaded = datetime(2026, 1, 1)
        odd = make_tv_file(series, 1, 2, "WEBDL-1080p")
        odd.dirname = "TV Shows/Elsewhere/Season 01"
        odd.file_path = f"{odd.dirname}/{odd.basename}"
        db.session.commit()

        assert (
            series_rename.rename_tv_series_task(series.id, "Odd Archive Show (2002)")
            is False
        )
        assert move_jobs(app) == []
        db.session.expire_all()
        assert db.session.get(File, good.id).aws_key_target is None


def test_a_conversion_rename_moves_the_original_and_then_the_transcoded_copy(app, s3):
    from app import aws_storage, db
    from app.models import File

    with app.app_context():
        file = make_movie_file(
            make_movie("Converted Move", 2001),
            "Bluray-1080p",
            converted_from_quality="Bluray-2160p",
        )
        file.untouched_basename = "Converted Move (2001) - [Bluray-2160p].mkv"
        file.aws_key = "untouched/Converted Move (2001) - [Bluray-2160p].mkv"
        file.aws_date_uploaded = datetime(2026, 1, 1)
        file.aws_transcoded_key = (
            "transcoded/Converted Move (2001) - [Bluray-1080p].mkv"
        )
        file.aws_transcoded_date_uploaded = datetime(2026, 1, 2)
        db.session.commit()
        file_id = file.id
        assert aws_storage.archive_drift(file) is None

        # The rename changes the untouched name. The original moves first.
        file.untouched_basename = "Converted Film (2001) - [Bluray-2160p].mkv"
        aws_storage.request_archive_move(file)
        db.session.commit()
        assert (
            file.aws_key_target
            == "untouched/Converted Film (2001) - [Bluray-2160p].mkv"
        )

        fake = s3(
            {
                file.aws_key: readable(),
                file.aws_transcoded_key: readable(VersionId="transcoded-version"),
            }
        )
        assert aws_storage.move_archive_object(file_id) is True
        db.session.expire_all()
        row = db.session.get(File, file_id)
        assert row.aws_key == "untouched/Converted Film (2001) - [Bluray-2160p].mkv"

        # Then the transcoded/ copy, with the quality of its library file.
        retagged = "transcoded/Converted Film (2001) - [Bluray-1080p].mkv"
        assert row.aws_key_target == retagged
        assert [job.args for job in move_jobs(app)][-1] == (file_id,)
        assert aws_storage.move_archive_object(file_id) is True
        db.session.expire_all()
        row = db.session.get(File, file_id)
        assert row.aws_transcoded_key == retagged
        assert row.aws_key_target is None
        assert fake.copies[-1] == (
            "transcoded/Converted Move (2001) - [Bluray-1080p].mkv",
            retagged,
        )


def test_the_backfill_lists_the_drifted_rows_and_apply_requests_them(app):
    from app import db
    from app.cli import register
    from app.models import File

    if "aws" not in app.cli.commands:
        register(app)
    with app.app_context():
        good = archived_film("Good Key", 1964)
        drifted = archived_film("Drifted Key", 1965, key_name="Old Drift (1965).mkv")
        tv = make_tv_file(make_tv_series("Drift Show"), 1, 1, "WEBDL-1080p")
        tv.untouched_basename = tv.basename
        tv.aws_key = "untouched/Drift Show - S01E01.mkv"
        tv.aws_date_uploaded = datetime(2026, 1, 1)
        tv.aws_filesize_bytes = 5
        db.session.commit()
        ids = (good.id, drifted.id, tv.id)

    runner = app.test_cli_runner()
    result = runner.invoke(args=["aws", "converge-keys"])
    assert result.exit_code == 0, result.output
    assert "2 archive key(s) differ" in result.output
    assert f"{SIZE + 5} bytes" in result.output
    assert (
        "untouched/Old Drift (1965).mkv -> untouched/Drifted Key (1965)"
        in result.output
    )
    assert "Dry run" in result.output
    with app.app_context():
        assert move_jobs(app) == []
        assert all(db.session.get(File, i).aws_key_target is None for i in ids)

    # A pending move is not requested again. Thus, --limit takes the
    # next row, and a repeat adds no second job (#384).

    result = runner.invoke(args=["aws", "converge-keys", "--apply", "--limit", "1"])
    assert result.exit_code == 0, result.output
    assert "Requested 1 archive move(s)" in result.output
    with app.app_context():
        first = {job.args[0] for job in move_jobs(app)}
    result = runner.invoke(args=["aws", "converge-keys", "--apply", "--limit", "1"])
    assert "1 of them are pending already" in result.output
    assert "Requested 1 archive move(s)" in result.output
    result = runner.invoke(args=["aws", "converge-keys", "--apply"])
    assert "Requested 0 archive move(s)" in result.output
    assert len(move_jobs(app)) == 2
    assert first < {job.args[0] for job in move_jobs(app)}
    with app.app_context():
        db.session.expire_all()
        assert db.session.get(File, ids[0]).aws_key_target is None
        assert db.session.get(File, ids[1]).aws_key_target == (
            "untouched/Drifted Key (1965) - [Bluray-1080p].mkv"
        )
        assert db.session.get(File, ids[2]).aws_key_target == (
            f"untouched/{db.session.get(File, ids[2]).untouched_basename}"
        )
        assert {job.args[0] for job in move_jobs(app)} == {ids[1], ids[2]}


def test_the_system_page_shows_the_pending_moves(app, admin_client):
    from app import aws_storage, db

    with app.app_context():
        file = archived_film("Pending Page", 1966, key_name="Pending (1966).mkv")
        aws_storage.request_archive_move(file)
        db.session.commit()
        file.archive_move_requested_at = datetime.now(timezone.utc) - timedelta(hours=3)
        db.session.commit()
        assert aws_storage.pending_archive_moves()["count"] == 1

    page = admin_client.get("/system").get_data(as_text=True)
    assert "Archive moves pending: 1, oldest 3 hours ago" in page


def test_the_old_rearchive_task_requests_a_move(app):
    """A queued job of the old task name still works. It requests the move."""

    from app import db, videos

    with app.app_context():
        file = archived_film("Old Job", 1967, key_name="Old Job Key (1967).mkv")
        file_id = file.id
        db.session.commit()

    assert videos.rearchive_untouched_object(file_id, "untouched/ignored.mkv") is True
    with app.app_context():
        assert [job.args for job in move_jobs(app)] == [(file_id,)]


def test_each_producer_leaves_the_key_of_the_untouched_name(
    app, s3, monkeypatch, tmp_path
):
    """Import, edit, conversion and rename each leave the key of the rule."""

    from app import aws_storage, db
    from app.models import File
    from app.series_rename import rename_tv_series_task

    class UploadS3:
        def head_object(self, **kwargs):
            from tests.fakes import missing_object

            raise missing_object()

        def upload_file(self, *args, **kwargs):
            return None

    monkeypatch.setattr(aws_storage, "aws_s3_client", lambda **kw: UploadS3())
    monkeypatch.setitem(app.config, "AWS_BUCKET", "test-bucket")
    staging = str(tmp_path)

    def upload_rip(file):
        """Upload a rip the way an import does: by its untouched name."""

        path = os.path.join(staging, file.untouched_basename)
        with open(path, "wb") as handle:
            handle.write(b"rip")
        file.aws_key, file.aws_date_uploaded, file.aws_filesize_bytes = (
            aws_storage.aws_upload(path, app.config["AWS_UNTOUCHED_PREFIX"])
        )
        db.session.commit()
        os.remove(path)
        return path

    with app.app_context():
        # Import: a name with characters that the key sanitizes.
        film = make_movie_file(make_movie("Rock & Roll: Live", 1968), "DVD")
        film.untouched_basename = "Rock & Roll: Live (1968) - [DVD].mkv"
        upload_rip(film)
        assert film.aws_key == aws_storage.archive_key_for(film, "untouched")

        # Edit: the first change goes to modified/.
        library = os.path.join(staging, "library.mkv")
        with open(library, "wb") as handle:
            handle.write(b"edit")
        aws_storage.archive_library_file(film, library)
        assert film.aws_key == aws_storage.archive_key_for(film, "modified")

        # Conversion: the original in untouched/, the copy in transcoded/.
        converted = make_movie_file(
            make_movie("Converted Producer", 1969),
            "Bluray-1080p",
            converted_from_quality="Bluray-2160p",
        )
        converted.untouched_basename = "Converted Producer (1969) - [Bluray-2160p].mkv"
        upload_rip(converted)
        aws_storage.archive_library_file(converted, library)
        assert converted.aws_key == aws_storage.archive_key_for(converted, "untouched")
        assert converted.aws_transcoded_key == aws_storage.archive_key_for(
            converted, "transcoded"
        )
        assert "[Bluray-1080p]" in converted.aws_transcoded_key

        # Rename: the series rename and its moves.
        series = make_tv_series("Producer Show")
        episode = make_tv_file(series, 1, 1, "WEBDL-1080p")
        episode.untouched_basename = episode.basename
        upload_rip(episode)
        series_id, episode_id, old_key = series.id, episode.id, episode.aws_key
        others = (film.id, converted.id)
        os.remove(library)

    assert rename_tv_series_task(series_id, "Producer Show (2020)") is True
    fake = s3({old_key: readable()})
    monkeypatch.setattr(aws_storage, "aws_s3_client", lambda **kw: fake)
    assert aws_storage.move_archive_object(episode_id) is True
    with app.app_context():
        row = db.session.get(File, episode_id)
        assert row.aws_key == aws_storage.archive_key_for(row, "untouched")
        assert "Producer Show (2020)" in row.aws_key
        for file_id in others:
            assert aws_storage.archive_drift(db.session.get(File, file_id)) is None


def test_a_second_rename_during_an_upload_keeps_the_newest_target(app, s3, monkeypatch):
    """An upload takes no title lock. A rename during it must survive its commit."""

    from app import aws_storage, db
    from app.models import File

    with app.app_context():
        file = archived_film("Upload Race", 1970)
        file_id = file.id
        file.aws_key = "modified/Upload Race Old (1970).mkv"
        file.untouched_basename = "Upload Race One (1970) - [Bluray-1080p].mkv"
        aws_storage.request_archive_move(file)
        db.session.commit()
        first = "modified/Upload Race One (1970) - [Bluray-1080p].mkv"
        second = "modified/Upload Race Two (1970) - [Bluray-1080p].mkv"
        assert file.aws_key_target == first

        def slow_upload(path, key_prefix="", key_name=None, **kwargs):
            # The second rename commits from another session during the upload.
            with db.engine.begin() as connection:
                connection.execute(
                    db.text(
                        "UPDATE file SET untouched_basename = :name, "
                        "aws_key_target = :target WHERE id = :id"
                    ),
                    {
                        "name": os.path.basename(second),
                        "target": second,
                        "id": file_id,
                    },
                )
            return os.path.join(key_prefix, key_name), datetime.utcnow(), 7

        monkeypatch.setattr(aws_storage, "aws_upload", slow_upload)
        for job in move_jobs(app):
            job.delete()
        aws_storage.archive_library_file(file, "/library/file.mkv")

        assert file.aws_key == first
        assert file.aws_key_target == second
        assert [job.args for job in move_jobs(app)] == [(file_id,)]

    s3({first: readable()})
    assert aws_storage.move_archive_object(file_id) is True
    with app.app_context():
        row = db.session.get(File, file_id)
        assert (row.aws_key, row.aws_key_target) == (second, None)


def test_a_move_with_a_lost_target_computes_it_again(app, s3):
    """A queued move whose target a race cleared still moves the archive."""

    from app import aws_storage, db
    from app.models import File

    with app.app_context():
        file = archived_film("Lost Target", 1971, key_name="Lost Target Old.mkv")
        file_id = file.id
        old_key = file.aws_key
        assert file.aws_key_target is None
        fake = s3({old_key: readable()})

        assert aws_storage.move_archive_object(file_id) is True
        assert fake.copies == [
            (old_key, "untouched/Lost Target (1971) - [Bluray-1080p].mkv")
        ]
        db.session.expire_all()
        assert db.session.get(File, file_id).aws_key_target is None


def test_the_sync_waits_for_a_scheduled_move(app):
    from app import aws_storage

    with app.app_context():
        assert aws_storage._sync_deferred(None) is False
        app.file_queue.enqueue_in(
            timedelta(minutes=10), "app.aws_storage.move_archive_object", 1
        )
        assert aws_storage._sync_deferred(None) is True


def test_the_sync_does_not_upload_over_a_key_that_moved_after_the_listing(
    app, s3, monkeypatch
):
    from app import aws_storage, db

    moved = "untouched/Moved Late (1972) - [DVD].mkv"
    monkeypatch.setattr(
        aws_storage, "get_matching_s3_objects", lambda bucket, prefix="": []
    )
    monkeypatch.setitem(app.config, "AWS_BUCKET", "test-bucket")
    s3({moved: readable()})
    with app.app_context():
        file = archived_film("Moved Late", 1972, quality="DVD")
        file.aws_key = moved
        db.session.commit()
        path = os.path.join(app.config["LIBRARY_DIR"], file.file_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "wb").close()

        listing = aws_storage._list_archives()
        aws_storage._reconcile_files(listing, None)
        uploads = [
            job
            for job in app.file_queue.jobs
            if job.func_name == "app.videos.upload_task"
        ]
        os.remove(path)
        assert uploads == []


def test_a_download_wait_keeps_the_receipt_handle(app, s3):
    from rq.registry import ScheduledJobRegistry

    from app import aws_storage, db

    with app.app_context():
        file = archived_film("Handle Kept", 1973, key_name="Handle Kept Old.mkv")
        aws_storage.request_archive_move(file)
        db.session.commit()
        app.file_queue.enqueue(
            "app.videos.download_task", args=(file.aws_key, "Handle Kept Old.mkv")
        )
        s3({file.aws_key: readable()})

        result = aws_storage.move_archive_object(file.id, sqs_receipt_handle="kept")
        assert result == aws_storage.MOVE_WAITING
        (retry,) = [
            app.file_queue.fetch_job(job_id)
            for job_id in ScheduledJobRegistry(queue=app.file_queue).get_job_ids()
        ]
        assert retry.func_name == "app.aws_storage.move_archive_object"
        assert retry.kwargs == {"sqs_receipt_handle": "kept"}
        assert retry.meta["archive_key"] == file.aws_key
        assert s3.sqs.deleted == []


def test_the_request_time_is_set_and_cleared_with_the_target(app):
    """One column holds the move state. Nothing outside the row records a move (#618).

    The first request keeps its time. A purged row counts no more."""

    from app import aws_storage, db
    from app.models import File

    with app.app_context():
        file = archived_film("Timed Move", 1975, key_name="Timed Old.mkv")
        assert aws_storage.pending_archive_moves() == {"count": 0, "oldest": None}
        aws_storage.request_archive_move(file)
        db.session.commit()
        first = file.archive_move_requested_at
        assert first is not None
        aws_storage.request_archive_move(file)
        db.session.commit()
        assert file.archive_move_requested_at == first
        assert aws_storage.pending_archive_moves() == {"count": 1, "oldest": first}
        assert not app.redis.keys("fitzflix:archive-move:*")

        aws_storage.clear_archive_target(file)
        db.session.commit()
        assert file.archive_move_requested_at is None

        aws_storage.request_archive_move(file)
        db.session.commit()
        db.session.delete(file)
        db.session.commit()
        assert aws_storage.pending_archive_moves() == {"count": 0, "oldest": None}
        assert (
            File.query.filter(File.archive_move_requested_at.isnot(None)).count() == 0
        )


def test_the_migration_adds_the_move_request_time():
    import importlib.util
    import inspect

    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "migrations",
        "versions",
        "e5d2a8c47f13_file_archive_move_requested_at.py",
    )
    spec = importlib.util.spec_from_file_location("move_time_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.revision == "e5d2a8c47f13"
    assert migration.down_revision == "b3a9d1e7c4f2"
    source = inspect.getsource(migration.upgrade)
    assert "archive_move_requested_at" in source and "add_column" in source
    assert "drop_column" not in source


def test_the_move_has_a_trail_stage(app):
    from app.pipeline import STAGES

    assert STAGES["app.aws_storage.move_archive_object"][0] == "Moving the S3 archive"


def test_a_move_waits_while_the_s3_sync_runs(app, s3, monkeypatch):
    """A move that starts after the idle check of the sync waits for it.

    The pause of SYNC_RECHECK_SECONDS is patched out (#447). A real sleep
    of 10 seconds let another user of the test Redis change the
    registries during the pause."""

    import time

    from rq.registry import ScheduledJobRegistry, StartedJobRegistry

    from app import aws_storage, db
    from app.models import File

    pauses = []
    monkeypatch.setattr(aws_storage.time, "sleep", pauses.append)
    with app.app_context():
        file = archived_film("Sync Wait", 1974, key_name="Sync Wait Old.mkv")
        aws_storage.request_archive_move(file)
        db.session.commit()
        old_key, target = file.aws_key, file.aws_key_target
        fake = s3({file.aws_key: readable()})
        sync = app.maintenance_queue.enqueue(
            "app.videos.sync_aws_s3_storage_task",
            description="Syncing files with AWS S3 storage",
        )
        # StartedJobRegistry.add raises NotImplementedError in rq 2. Thus,
        # the test writes the sorted set of the registry directly
        registry = StartedJobRegistry(queue=app.maintenance_queue)
        app.redis.zadd(registry.key, {sync.id: int(time.time()) + 300})

        result = aws_storage.move_archive_object(file.id, sqs_receipt_handle="held")
        assert result == aws_storage.MOVE_WAITING
        assert pauses == [aws_storage.SYNC_RECHECK_SECONDS]
        assert fake.copies == []
        db.session.expire_all()
        file = db.session.get(File, file.id)
        assert (file.aws_key, file.aws_key_target) == (old_key, target)
        (retry,) = [
            app.file_queue.fetch_job(job_id)
            for job_id in ScheduledJobRegistry(queue=app.file_queue).get_job_ids()
        ]
        assert retry.func_name == "app.aws_storage.move_archive_object"
        assert retry.kwargs == {"sqs_receipt_handle": "held"}
        assert retry.meta["archive_key"] == old_key
        assert s3.sqs.deleted == []


def test_a_second_request_of_a_waiting_move_adds_no_job(app):
    """A queued move reads the target when it runs (#384)."""

    from app import aws_storage, db

    with app.app_context():
        file = archived_film("Twice Asked", 1967, key_name="Twice (1967).mkv")
        assert aws_storage.request_archive_move(file)
        db.session.commit()
        assert aws_storage.request_archive_move(file)
        db.session.commit()
        (job,) = move_jobs(app)
        assert job.id.startswith(f"archive-move-{file.id}-")

        # A running move does not count. It can hold an older target.
        app.file_queue.remove(job)
        assert aws_storage.request_archive_move(file)
        db.session.commit()
        assert len(move_jobs(app)) == 1


def test_a_restore_of_a_shared_key_goes_to_the_row_that_moves_it(app, s3):
    """Rows A and B share key K. Only B has a pending move of K (#385)."""

    from app import aws_storage, db

    with app.app_context():
        first = archived_film("Shared Stay", 1958, key_name="Shared (1958).mkv")
        mover = archived_film("Shared Move", 1959, key_name="Shared (1958).mkv")
        key = first.aws_key
        assert mover.aws_key == key and first.id < mover.id
        assert aws_storage.request_archive_move(mover)
        db.session.commit()
        for job in move_jobs(app):
            job.delete()

        downloads, moves = {}, {}
        aws_storage._route_restore(s3.sqs, key, "handle-9", downloads, moves)
        assert downloads == {}
        assert moves[key].args == (mover.id,)
        assert [
            job
            for job in app.file_queue.jobs
            if job.func_name == "app.videos.download_task"
        ] == []


def test_a_requested_download_of_a_shared_key_runs_beside_the_move(app, s3):
    """A user asked for the download of key K, and a move of K waits (#385 follow-up)."""

    from app import aws_storage, db

    with app.app_context():
        first = archived_film("Shared Ask", 1960, key_name="Asked (1960).mkv")
        mover = archived_film("Shared Moving", 1961, key_name="Asked (1960).mkv")
        key = first.aws_key
        assert aws_storage.request_archive_move(mover)
        db.session.commit()
        for job in move_jobs(app):
            job.delete()
        aws_storage.mark_restore_pending(key)

        downloads, moves = {}, {}
        aws_storage._route_restore(s3.sqs, key, "handle-7", downloads, moves)
        assert moves[key].args == (mover.id,)
        assert moves[key].kwargs == {"sqs_receipt_handle": "handle-7"}
        (download,) = [
            job
            for job in app.file_queue.jobs
            if job.func_name == "app.videos.download_task"
        ]
        assert download.args == (key, os.path.basename(key), None)
        assert downloads[key].id == download.id

        # The move waits while the download is in the queue.
        assert aws_storage.move_archive_object(mover.id, "handle-7") == (
            aws_storage.MOVE_WAITING
        )


@pytest.mark.parametrize("deleted", [True, False])
def test_an_early_stop_deletes_a_message_that_arrived_late(app, monkeypatch, deleted):
    """A move queued with no handle can get one from the poll (#587).

    The early returns tested the argument, not the newest handle. The
    message came back, and a rename back turned it into a download. The
    handle is under the key of the message (#617)."""

    from types import SimpleNamespace

    from app import aws_storage, db

    key = "untouched/Late Handle Move (1960) - [DVD].mkv"
    sqs = SQS()
    monkeypatch.setattr(aws_storage, "aws_sqs_client", lambda: sqs)
    monkeypatch.setattr(
        aws_storage,
        "get_current_job",
        lambda: SimpleNamespace(id="late-move", meta={"archive_key": key}),
    )
    app.redis.set(aws_storage.SQS_HANDLE_KEY.format(key=key), "late")
    with app.app_context():
        file = make_movie_file(make_movie("Late Handle Move", 1960), "DVD")
        db.session.commit()
        file_id = file.id
        if deleted:
            db.session.delete(file)
            db.session.commit()
        result = aws_storage.move_archive_object(file_id)
    assert result is (not deleted)
    assert sqs.deleted == ["late"]
    assert app.redis.get(aws_storage.SQS_HANDLE_KEY.format(key=key)) is None


def _case_renamed_archive(library, key_name):
    """Make a film or an episode whose archive key is key_name(rule name). Return it."""

    from app import db

    if library == "film":
        file = archived_film("None but the Brave", 1965)
    else:
        file = make_tv_file(make_tv_series("Case Show"), 1, 2, "WEBDL-1080p")
        file.untouched_basename = file.basename
        file.aws_date_uploaded = datetime(2026, 1, 1)
        file.aws_filesize_bytes = SIZE
    file.aws_key = f"untouched/{key_name(file.untouched_basename)}"
    db.session.commit()
    return file


def _dry_run(app):
    """Run the dry run of flask aws converge-keys. Return its output."""

    from app.cli import register

    if "aws" not in app.cli.commands:
        register(app)
    result = app.test_cli_runner().invoke(args=["aws", "converge-keys"])
    assert result.exit_code == 0, result.output
    return result.output


@pytest.mark.parametrize("library", ["film", "episode"])
def test_a_case_only_rename_keeps_the_archive_key(app, library):
    """A rename that changes only letter case keeps the key and queues no move (#612).

    A pending target from an older case-only request goes. The dry run of
    converge-keys uses the same rule and lists no drift."""

    from app import aws_storage, db

    with app.app_context():
        file = _case_renamed_archive(library, str.swapcase)
        file_id, old_key = file.id, file.aws_key
        rule_key = f"untouched/{file.untouched_basename}"
        assert old_key != rule_key and old_key.lower() == rule_key.lower()
        file.aws_key_target = rule_key
        db.session.commit()

        assert aws_storage.archive_drift(file) is None
        assert aws_storage.request_archive_move(file) is False
        db.session.commit()
        assert committed_key(app, file_id) == old_key
        assert committed_key(app, file_id, "aws_key_target") is None
        assert move_jobs(app) == []
    assert "0 archive key(s) differ" in _dry_run(app)


@pytest.mark.parametrize("library", ["film", "episode"])
def test_a_real_rename_still_moves_the_archive(app, library):
    """A rename that changes more than letter case still requests a move (#369)."""

    from app import aws_storage, db

    with app.app_context():
        file = _case_renamed_archive(library, lambda name: f"Old {name}")
        file_id, old_key = file.id, file.aws_key
        rule_key = f"untouched/{file.untouched_basename}"

        assert aws_storage.request_archive_move(file) is True
        db.session.commit()
        assert committed_key(app, file_id) == old_key
        assert committed_key(app, file_id, "aws_key_target") == rule_key
        assert [job.args[0] for job in move_jobs(app)] == [file_id]
    output = _dry_run(app)
    assert "1 archive key(s) differ" in output
    assert f"{old_key} -> {rule_key}" in output


def _scheduled_moves(app):
    from rq.registry import ScheduledJobRegistry

    return [
        job
        for job in (
            app.file_queue.fetch_job(job_id)
            for job_id in ScheduledJobRegistry(queue=app.file_queue).get_job_ids()
        )
        if job is not None and job.func_name == "app.aws_storage.move_archive_object"
    ]


@pytest.mark.parametrize("wait", ["download", "lock"])
def test_a_move_wait_keeps_the_handle_record_of_its_key(app, s3, monkeypatch, wait):
    """The message stays in SQS while a move waits. So does its record (#617).

    The record lives under the source key. Only a delete of the message
    removes it."""

    from app import aws_storage, db

    with app.app_context():
        file = archived_film(f"Wait Handle {wait}", 1974, key_name=f"Wait {wait}.mkv")
        aws_storage.request_archive_move(file)
        db.session.commit()
        old_key = file.aws_key
        if wait == "download":
            app.file_queue.enqueue(
                "app.videos.download_task", args=(old_key, "Wait Handle Old.mkv")
            )
        s3({old_key: readable()})
        aws_storage.record_receipt_handle(old_key, "handle-2")
        held = (
            app.lock_manager.lock(file.file_identifier(), 60000)
            if wait == "lock"
            else None
        )
        try:
            result = aws_storage.move_archive_object(file.id, sqs_receipt_handle="h1")
            assert result == (aws_storage.MOVE_WAITING if wait == "download" else False)
            (retry,) = _scheduled_moves(app)
            assert retry.kwargs == {"sqs_receipt_handle": "h1"}
            assert app.redis.get(aws_storage.SQS_HANDLE_KEY.format(key=old_key)) == (
                b"handle-2"
            )
        finally:
            if held:
                app.lock_manager.unlock(held)
