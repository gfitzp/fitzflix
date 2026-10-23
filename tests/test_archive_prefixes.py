"""Test the 3 archive prefixes: untouched/, modified/, and transcoded/ (#299)."""

import os
from datetime import datetime

import pytest

import app.aws_storage as aws_storage
from app import db
from app.models import File

from tests.factories import make_movie, make_movie_file


@pytest.fixture
def uploads(monkeypatch):
    """Fake aws_upload. Record (path, key prefix, key name) for each call."""

    calls = []

    def fake_upload(path, key_prefix="", key_name=None, **kwargs):
        calls.append((path, key_prefix, key_name))
        return os.path.join(key_prefix, key_name), datetime.utcnow(), 7

    monkeypatch.setattr(aws_storage, "aws_upload", fake_upload)
    return calls


def _deletes(app):
    return [
        job.args[0]
        for job in app.handoff_queue.jobs
        if job.func_name == "app.videos.aws_delete"
    ]


def test_the_first_change_moves_the_archive_to_modified(app, uploads):
    with app.app_context():
        file = make_movie_file(
            make_movie("Moved Archive", 1999),
            "Bluray-1080p",
            aws_key="untouched/Moved Archive (1999) - [Bluray-1080p].mkv",
        )
        db.session.commit()

        aws_storage.archive_library_file(file, "/library/file.mkv")
        assert file.aws_key == ("modified/Moved Archive (1999) - [Bluray-1080p].mkv")
        assert _deletes(app) == ["untouched/Moved Archive (1999) - [Bluray-1080p].mkv"]

        # A later change overwrites the modified/ object. Nothing is deleted.

        aws_storage.archive_library_file(file, "/library/file.mkv")
        assert uploads[-1][1:] == (
            "modified",
            "Moved Archive (1999) - [Bluray-1080p].mkv",
        )
        assert file.aws_key == ("modified/Moved Archive (1999) - [Bluray-1080p].mkv")
        assert len(_deletes(app)) == 1


def test_an_extra_keeps_the_title_of_its_film_in_modified(app, uploads):
    """The basename of an extra is not unique. Its modified/ key keeps the
    untouched/ name, with the title of the film."""

    with app.app_context():
        movie = make_movie("Extra Archive", 1955)
        first = make_movie_file(
            movie,
            "Bluray-1080p",
            "Trailers",
            plex_title="Trailer",
            aws_key="untouched/Extra Archive (1955) - Trailers - Trailer [Bluray-1080p].mkv",
        )
        second = make_movie_file(
            make_movie("Other Archive", 1956),
            "DVD",
            "Trailers",
            plex_title="Trailer",
            untouched_basename="Other Archive (1956) - Trailers - Trailer [DVD].mkv",
        )
        db.session.commit()
        assert first.basename == second.basename == "Trailer.mkv"

        aws_storage.archive_library_file(first, "/library/first.mkv")
        aws_storage.archive_library_file(second, "/library/second.mkv")
        assert first.aws_key == (
            "modified/Extra Archive (1955) - Trailers - Trailer [Bluray-1080p].mkv"
        )
        assert second.aws_key == (
            "modified/Other Archive (1956) - Trailers - Trailer [DVD].mkv"
        )


def test_a_claimed_modified_key_gets_the_record_id(app, uploads):
    with app.app_context():
        name = "Shared Archive (1957) - [DVD].mkv"
        movie = make_movie("Shared Archive", 1957)
        make_movie_file(movie, "DVD", aws_key=f"modified/{name}")
        other = make_movie_file(
            movie,
            "DVD",
            plex_title="Shared Archive (1957) - copy",
            aws_key=f"untouched/{name}",
        )
        db.session.commit()

        aws_storage.archive_library_file(other, "/library/other.mkv", False)
        assert (
            other.aws_key == f"modified/Shared Archive (1957) - [DVD] ({other.id}).mkv"
        )


def test_a_change_of_a_conversion_goes_to_transcoded(app, uploads):
    with app.app_context():
        file = make_movie_file(
            make_movie("Converted Archive", 2001),
            "Bluray-1080p",
            converted_from_quality="Bluray-2160p",
            aws_key="untouched/Converted Archive (2001) - [Bluray-2160p].mkv",
        )
        db.session.commit()

        aws_storage.archive_library_file(file, "/library/file.mkv")
        assert file.aws_key == (
            "untouched/Converted Archive (2001) - [Bluray-2160p].mkv"
        )
        assert file.aws_transcoded_key == (
            "transcoded/Converted Archive (2001) - [Bluray-1080p].mkv"
        )
        assert file.restore_key == file.aws_transcoded_key
        assert _deletes(app) == []


def test_a_restore_from_modified_goes_straight_into_the_library(app, tmp_path):
    with app.app_context():
        library = os.path.join(app.config["MOVIE_LIBRARY"], "Placed (2002)")
        os.makedirs(library, exist_ok=True)
        target = os.path.join(library, "Placed (2002) - [DVD].mkv")
        with open(target, "wb") as f:
            f.write(b"old")
        downloaded = tmp_path / ".Placed (2002) - [DVD].mkv"
        downloaded.write_bytes(b"restored file")

        aws_storage.place_in_library(str(downloaded), target, 13, "Placed")
        assert open(target, "rb").read() == b"restored file"
        assert not downloaded.exists()
        assert os.listdir(library) == ["Placed (2002) - [DVD].mkv"]

        # A size that does not match the S3 object changes nothing.

        downloaded.write_bytes(b"short")
        with pytest.raises(OSError):
            aws_storage.place_in_library(str(downloaded), target, 13, "Placed")
        assert open(target, "rb").read() == b"restored file"
        os.remove(target)


@pytest.mark.parametrize(
    "key,direct",
    [
        ("modified/Routed (2003) - [DVD].mkv", True),
        ("untouched/Routed (2003) - [DVD].mkv", False),
    ],
)
def test_download_task_routes_by_prefix(app, monkeypatch, key, direct):
    calls = []
    monkeypatch.setattr(
        aws_storage,
        "aws_download",
        lambda key, basename, receipt=None, place_at=None: calls.append(place_at)
        or aws_storage.DOWNLOAD_COMPLETE,
    )
    with app.app_context():
        file = make_movie_file(make_movie("Routed", 2003), "DVD", aws_key=key)
        db.session.commit()
        expected = os.path.join(app.config["LIBRARY_DIR"], file.file_path)

    assert aws_storage.download_task(key, os.path.basename(key)) is True
    assert calls == [expected if direct else None]


def test_the_sync_restores_the_transcoded_copy_and_prunes_all_prefixes(
    app, monkeypatch
):
    """A missing conversion restores its transcoded/ copy, not the 4K original.

    The sync claims the keys of the 3 prefixes and prunes the others."""

    objects = {
        "untouched/": [
            {"Key": "untouched/Synced (2004) - [Bluray-2160p].mkv", "Size": 90},
            {"Key": "untouched/Stray (2004) - [DVD].mkv", "Size": 1},
        ],
        "modified/": [{"Key": "modified/Old Copy (2004) - [DVD].mkv", "Size": 2}],
        "transcoded/": [
            {"Key": "transcoded/Synced (2004) - [Bluray-1080p].mkv", "Size": 30}
        ],
    }
    monkeypatch.setattr(
        aws_storage,
        "get_matching_s3_objects",
        lambda bucket, prefix="": objects.get(prefix, []),
    )
    restores, deletes = [], []
    monkeypatch.setattr(
        aws_storage, "aws_restore", lambda key, **kw: restores.append(key)
    )
    monkeypatch.setattr(aws_storage, "aws_delete", lambda key: deletes.append(key))
    monkeypatch.setattr(aws_storage, "send_email", lambda *a, **k: None)

    class FakeClient:
        def put_object(self, **kwargs):
            pass

    monkeypatch.setattr(aws_storage, "aws_s3_client", lambda **kw: FakeClient())

    with app.app_context():
        make_movie_file(
            make_movie("Synced", 2004),
            "Bluray-1080p",
            converted_from_quality="Bluray-2160p",
            aws_key="untouched/Synced (2004) - [Bluray-2160p].mkv",
            aws_date_uploaded=datetime.utcnow(),
            aws_transcoded_key="transcoded/Synced (2004) - [Bluray-1080p].mkv",
            aws_transcoded_date_uploaded=datetime.utcnow(),
        )
        db.session.commit()

    aws_storage.sync_aws_s3_storage_task()

    assert restores == ["transcoded/Synced (2004) - [Bluray-1080p].mkv"]
    assert sorted(deletes) == [
        "modified/Old Copy (2004) - [DVD].mkv",
        "untouched/Stray (2004) - [DVD].mkv",
    ]
    with app.app_context():
        file = File.query.filter_by(plex_title="Synced (2004)").one()
        assert file.aws_transcoded_filesize_bytes == 30


class FakeArchiveS3:
    """Fake S3 for the backfill. Hold the ETag of each key."""

    def __init__(self, old_size, old_etag, new_etag):
        self.objects = {}
        self.old_size, self.old_etag, self.new_etag = old_size, old_etag, new_etag
        self.deleted = []

    def head_object(self, Bucket, Key):
        if Key.startswith("untouched/"):
            return {
                "ContentLength": self.old_size,
                "ETag": self.old_etag,
                "VersionId": "v-old",
            }
        return {
            "ContentLength": self.old_size,
            "ETag": self.new_etag,
            "VersionId": "v-new",
        }

    def delete_object(self, Bucket, Key, VersionId=None):
        self.deleted.append((Key, VersionId))


@pytest.fixture
def backfill_file(app, monkeypatch):
    """A replaced untouched/ archive with a 12-byte library file."""

    import app.videos as videos

    monkeypatch.setattr(videos, "acquire_lock_or_defer", lambda *a, **k: "lock")
    monkeypatch.setattr(app.lock_manager, "unlock", lambda lock: None)
    with app.app_context():
        file = make_movie_file(
            make_movie("Backfilled", 2005),
            "DVD",
            aws_key="untouched/Backfilled (2005) - [DVD].mkv",
            aws_filesize_bytes=12,
        )
        db.session.commit()
        path = os.path.join(app.config["LIBRARY_DIR"], file.file_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(b"library file")
        yield file.id
    os.remove(path)


def _run_backfill(app, monkeypatch, file_id, fake):
    monkeypatch.setattr(aws_storage, "aws_s3_client", lambda **kw: fake)
    result = aws_storage.move_archive_to_modified_task(file_id)
    with app.app_context():
        return result, db.session.get(File, file_id).aws_key


def test_the_backfill_moves_a_copy_of_the_library_file(
    app, uploads, monkeypatch, backfill_file
):
    fake = FakeArchiveS3(12, '"same"', '"same"')
    result, key = _run_backfill(app, monkeypatch, backfill_file, fake)
    assert result is True
    assert key == "modified/Backfilled (2005) - [DVD].mkv"
    # The old version is deleted outright. No delete marker job is queued.
    assert fake.deleted == [("untouched/Backfilled (2005) - [DVD].mkv", "v-old")]
    assert _deletes(app) == []
    # A second run finds the archive already in modified/.
    assert aws_storage.move_archive_to_modified_task(backfill_file) is True
    assert len(uploads) == 1


def test_the_backfill_keeps_an_archive_of_a_different_size(
    app, uploads, monkeypatch, backfill_file
):
    fake = FakeArchiveS3(99, '"rip"', '"library"')
    result, key = _run_backfill(app, monkeypatch, backfill_file, fake)
    assert result is False
    assert key == "untouched/Backfilled (2005) - [DVD].mkv"
    assert uploads == [] and fake.deleted == []


def test_the_backfill_keeps_an_archive_with_different_bytes(
    app, uploads, monkeypatch, backfill_file
):
    """The sizes are equal, but the ETags are not. The task deletes its
    new upload and keeps the old archive."""

    fake = FakeArchiveS3(12, '"rip"', '"library"')
    result, key = _run_backfill(app, monkeypatch, backfill_file, fake)
    assert result is False
    assert key == "untouched/Backfilled (2005) - [DVD].mkv"
    assert len(uploads) == 1
    assert fake.deleted == [("modified/Backfilled (2005) - [DVD].mkv", "v-new")]


def test_a_scan_that_rewrites_the_file_queues_its_archive(app, monkeypatch):
    import app.tracks as tracks

    monkeypatch.setattr(tracks, "remove_empty_subtitle_tracks", lambda path: True)
    monkeypatch.setattr(
        tracks,
        "_extract_media_details",
        lambda path: {
            "video": {},
            "audio_tracks": [],
            "subtitle_tracks": [],
            "filesize_bytes": 1,
        },
    )
    monkeypatch.setattr(
        tracks, "get_setting", lambda name, *a, **k: name == "ARCHIVE_ORIGINAL_MEDIA"
    )

    class Matroska:
        tracks = [type("T", (), {"track_type": "General", "format": "Matroska"})()]

    monkeypatch.setattr(tracks.MediaInfo, "parse", lambda path: Matroska())
    with app.app_context():
        file = make_movie_file(make_movie("Scanned", 2006), "DVD")
        db.session.commit()
        path = os.path.join(app.config["LIBRARY_DIR"], file.file_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "wb").close()
        file_id = file.id

    tracks.track_metadata_scan_task(file_id)
    with app.app_context():
        jobs = [
            job.args
            for job in app.file_queue.jobs
            if job.func_name == "app.videos.upload_task"
        ]
        assert jobs == [(file_id,)]
    os.remove(path)
