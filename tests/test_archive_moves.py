"""Test the archive moves: the archive key follows the untouched name (#369).

A rename requests a move. The move job copies the S3 object on the
server side, verifies the copy, and only then writes the new key. A
Deep Archive object first gets a Bulk restore. Its notification brings
the move back. aws_key names a real object at every moment."""

import logging
import os
from datetime import datetime

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
        fake = s3(
            {old_key: readable()},
            on_copy=lambda source, target: seen.append(committed_key(app, file_id)),
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

        # Equal ETags prove the bytes. The old version goes by its id.
        assert fake.deletes == [(old_key, "old-version")]
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

        assert aws_storage.move_archive_object(file_id) is False
        target = "untouched/Bad Copy (1951) - [Bluray-1080p].mkv"
        assert fake.deletes == [(target, "copy-1")]
        db.session.expire_all()
        assert db.session.get(File, file_id).aws_key == old_key
        assert db.session.get(File, file_id).aws_key_target == target
        assert "has the ETag" in caplog.text


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
        # asked for this restore.
        assert aws_storage.restore_requested_at(app.redis, old_key) is None
        db.session.expire_all()
        row = db.session.get(File, file_id)
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
        def list_objects(self, **kwargs):
            return {}

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
