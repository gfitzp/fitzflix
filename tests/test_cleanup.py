"""Test the weekly orphaned-file cleanup.

The cleanup deletes the hidden partial files that failed tasks leave
behind, after an age gate. It does not touch macOS metadata, a fresh
file, or a visible file.
"""

import os
import time

import pytest

import app.maintenance as maintenance


def plant(path, age_days=0):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"stranded bytes")
    if age_days:
        old = time.time() - age_days * 86400
        os.utime(path, (old, old))
    return path


def test_cleanup_deletes_only_old_hidden_partials(app, monkeypatch):
    sent = []
    monkeypatch.setattr(
        maintenance,
        "task_send_email",
        lambda subject, sender, recipients, text_body, html_body: sent.append(
            {"subject": subject, "body": text_body}
        ),
    )

    staging = app.config["STAGING_DIR"]
    movies = app.config["MOVIE_LIBRARY"]
    rejects = app.config["REJECTS_DIR"]
    transcodes = app.config["TRANSCODES_DIR"]

    doomed = [
        plant(os.path.join(staging, ".Stranded (2020) - [DVD].mkv"), 8),
        plant(os.path.join(staging, ".Stranded (2020) - [DVD].mkv.convert.mkv"), 8),
        plant(
            os.path.join(movies, "Stranded (2020)", ".Stranded (2020) - [DVD].mkv"), 8
        ),
        plant(os.path.join(rejects, "exception", ".Bad (2020).mkv.partial"), 8),
        plant(os.path.join(transcodes, "Stranded (2020)", ".Stranded (2020).m4v"), 8),
    ]
    kept = [
        # This file is below the age gate. It can be a copy in progress.
        plant(os.path.join(staging, ".Copying (2021) - [DVD].mkv"), 1),
        # The cleanup never touches a visible file, at any age.
        plant(
            os.path.join(movies, "Stranded (2020)", "Stranded (2020) - [DVD].mkv"), 30
        ),
        # macOS metadata is not a pipeline leftover.
        plant(os.path.join(staging, ".DS_Store"), 30),
        plant(
            os.path.join(movies, "Stranded (2020)", "._Stranded (2020) - [DVD].mkv"), 30
        ),
    ]

    try:
        maintenance.cleanup_orphaned_files()

        for path in doomed:
            assert not os.path.exists(path), path
        for path in kept:
            assert os.path.exists(path), path

        assert len(sent) == 1
        assert "5 orphaned partial file(s)" in sent[0]["body"]
        assert "scratch database" not in sent[0]["body"]
    finally:
        for path in kept:
            if os.path.exists(path):
                os.remove(path)


def test_cleanup_removes_week_old_leftover_directories(app, monkeypatch):
    """Make sure the cleanup removes week-old leftover directories.

    This is the leftover-directory pass. A directory with no changes for
    a week falls when it is empty OR holds only junk. Junk is an @eaDir
    tree, macOS metadata, or some aged stray images (an orphaned custom
    poster). A fresh directory, a real file, or a picture collection
    survives."""

    sent = []
    monkeypatch.setattr(
        maintenance,
        "task_send_email",
        lambda subject, sender, recipients, text_body, html_body: sent.append(
            {"subject": subject, "body": text_body}
        ),
    )
    movies = app.config["MOVIE_LIBRARY"]
    old = time.time() - 8 * 86400

    def make_dir(path, aged=False):
        os.makedirs(path, exist_ok=True)
        if aged:
            os.utime(path, (old, old))
        return path

    def age(path):
        os.utime(path, (old, old))

    # This is an aged empty tree. The child and the parent both go in
    # ONE pass. The pass captures the mtimes before the removals start.

    nested_child = make_dir(os.path.join(movies, "Gone (1999)", "Extras"), aged=True)
    nested_parent = os.path.join(movies, "Gone (1999)")
    age(nested_parent)

    # All leftovers that hold only junk fall: a metadata-only folder, an
    # orphaned poster, and a Synology @eaDir with fresh contents. The
    # NAS rewrites the @eaDir contents on its own schedule. That must
    # not keep a dead folder alive forever.

    finder_touched = make_dir(os.path.join(movies, "Browsed (2002)"))
    plant(os.path.join(finder_touched, ".DS_Store"), age_days=30)
    age(finder_touched)

    postered = make_dir(os.path.join(movies, "Moved Away (2003)"))
    plant(os.path.join(postered, "poster.jpg"), age_days=30)
    plant(os.path.join(postered, ".DS_Store"), age_days=30)
    age(postered)

    synology = make_dir(os.path.join(movies, "NAS Leftover (2004)"))
    plant(os.path.join(synology, "@eaDir", "SYNOPHOTO_THUMB_XL.jpg"))  # fresh
    plant(os.path.join(synology, "folder.jpg"), age_days=30)
    age(os.path.join(synology, "@eaDir"))
    age(synology)

    # The survivors: a folder emptied recently, a real file, a FRESH
    # poster (a recent human action), and an image collection above the
    # cap.

    fresh = make_dir(os.path.join(movies, "Fresh (2000)"))
    occupied = make_dir(os.path.join(movies, "Occupied (2001)"))
    resident = plant(os.path.join(occupied, "Occupied (2001) - [DVD].mkv"), age_days=30)
    age(occupied)
    fresh_poster_dir = make_dir(os.path.join(movies, "Repostered (2005)"))
    fresh_poster = plant(os.path.join(fresh_poster_dir, "poster.jpg"))
    age(fresh_poster_dir)
    trove = make_dir(os.path.join(movies, "Photo Trove (2006)"))
    trove_images = [
        plant(os.path.join(trove, f"scan-{n:03d}.jpg"), age_days=30) for n in range(30)
    ]
    age(trove)

    try:
        maintenance.cleanup_orphaned_files()

        for gone in (nested_child, nested_parent, finder_touched, postered, synology):
            assert not os.path.exists(gone), gone
        assert os.path.exists(fresh)
        assert os.path.exists(occupied) and os.path.exists(resident)
        assert os.path.exists(fresh_poster_dir) and os.path.exists(fresh_poster)
        assert os.path.exists(trove) and all(os.path.exists(i) for i in trove_images)
        assert os.path.isdir(movies)  # the root is never a candidate

        assert len(sent) == 1
        assert "5 leftover directories" in sent[0]["body"]
        assert "(cleared 2 leftover file(s))" in sent[0]["body"]
        assert "orphaned partial file" not in sent[0]["body"]
    finally:
        for leftover in (resident, fresh_poster, *trove_images):
            if os.path.exists(leftover):
                os.remove(leftover)
        for leftover_dir in (fresh, occupied, fresh_poster_dir, trove):
            if os.path.isdir(leftover_dir):
                os.rmdir(leftover_dir)


def test_clear_leftover_directory_is_junk_aware_and_climbs(app):
    """Make sure clear_leftover_directory knows junk and climbs.

    This function is the delete-time and rename-time relative of the
    directory pass of the weekly sweep. It has no age gate. A fresh
    poster-only folder is already known as a husk. The climb clears the
    parent that holds only a poster too. Real media keeps everything
    alive. The roots never fall. The function refuses a path outside
    the roots."""

    with app.app_context():
        tv = app.config["TV_LIBRARY"]

        season = os.path.join(tv, "Husk Show", "Season 2023")
        os.makedirs(season)
        plant(os.path.join(season, "poster.png"))  # fresh, not aged
        plant(os.path.join(tv, "Husk Show", "poster.png"))
        removed = maintenance.clear_leftover_directory(season)
        assert len(removed) == 2
        assert not os.path.exists(os.path.join(tv, "Husk Show"))
        assert os.path.isdir(tv)

        kept = os.path.join(tv, "Kept Show", "Season 01")
        os.makedirs(kept)
        plant(os.path.join(kept, "Kept Show - S01E01 - [DVD].mkv"))
        assert maintenance.clear_leftover_directory(kept) == []
        assert os.path.isdir(kept)

        assert maintenance.clear_leftover_directory("/etc") == []
        assert maintenance.clear_leftover_directory(tv) == []
        assert os.path.isdir(tv)


def _admin_csrf_token(admin_client, path):
    import re

    page = admin_client.get(path).get_data(as_text=True)
    return re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page).group(1)


def test_series_delete_clears_poster_husks(app, admin_client):
    """Make sure a series delete clears the poster husks.

    A series delete removes the poster-only season and series folders
    immediately. It does not leave husks for the weekly sweep (the
    Legacy on Ice leftovers, 2026-08)."""

    from app import db
    from tests.factories import make_tv_file, make_tv_series

    with app.app_context():
        series = make_tv_series("Husk Special")
        file = make_tv_file(series, 2023, 1, "HDTV-720p")
        db.session.commit()
        series_id = series.id
        file_path = file.file_path

    lib = app.config["LIBRARY_DIR"]
    plant(os.path.join(lib, file_path))
    plant(os.path.join(os.path.dirname(os.path.join(lib, file_path)), "poster.png"))
    plant(os.path.join(lib, "TV Shows", "Husk Special", "poster.png"))

    token = _admin_csrf_token(admin_client, f"/tv/{series_id}")
    response = admin_client.post(
        f"/tv/{series_id}",
        data={"csrf_token": token, "delete_submit": "Delete Series"},
    )
    assert response.status_code == 302
    assert not os.path.exists(os.path.join(lib, "TV Shows", "Husk Special"))
    assert os.path.isdir(os.path.join(lib, "TV Shows"))


def test_file_delete_clears_a_movie_poster_husk(app, admin_client):
    """Make sure a delete of the last movie file clears a poster husk.

    Without this, the planted poster.jpg would keep the folder alive."""

    from app import db
    from tests.factories import make_movie, make_movie_file

    with app.app_context():
        movie = make_movie("Husk Film", 2001)
        file = make_movie_file(movie, "DVD")
        db.session.commit()
        file_id = file.id
        file_path = file.file_path

    lib = app.config["LIBRARY_DIR"]
    movie_dir = os.path.dirname(os.path.join(lib, file_path))
    plant(os.path.join(lib, file_path))
    plant(os.path.join(movie_dir, "poster.jpg"))

    token = _admin_csrf_token(admin_client, f"/file/{file_id}")
    response = admin_client.post(
        f"/file/{file_id}",
        data={"csrf_token": token, "delete_submit": "Delete File"},
    )
    assert response.status_code == 302
    assert not os.path.exists(movie_dir)


def test_cleanup_stays_quiet_when_nothing_is_stranded(app, monkeypatch):
    sent = []
    monkeypatch.setattr(
        maintenance, "task_send_email", lambda *args, **kwargs: sent.append(1)
    )

    maintenance.cleanup_orphaned_files()

    assert sent == []


def test_scratch_db_drop_is_skipped_off_mysql(app):
    """Make sure the scratch database drop does nothing off MySQL.

    The test suite runs on SQLite. SQLite has no scratch database to
    drop. The helper must detect this and do nothing."""

    with app.app_context():
        assert maintenance._drop_leftover_restore_database() is False


def test_shared_untouched_key_is_never_deleted(app):
    """Make sure the purge never deletes a shared untouched key.

    Two file records can claim the same untouched S3 key. Causes: a key
    that points to a new object after a rename, or a re-import that
    arrives at the same basename (the Bambi II incident). The purge
    guard reports the key as claimed until no surviving record holds
    it."""

    from datetime import datetime

    from app import db
    from app.videos import untouched_key_still_claimed
    from tests.factories import make_movie, make_movie_file

    with app.app_context():
        movie = make_movie("Shared Key Film", 2006)
        keeper = make_movie_file(movie, "Bluray-1080p")
        goner = make_movie_file(movie, "DVD")
        shared = "untouched/Shared Key Film (2006) - [WEBDL-1080p].mkv"
        keeper.aws_untouched_key = shared
        goner.aws_untouched_key = shared
        db.session.commit()

        assert untouched_key_still_claimed(shared) is True

        # The row of the worse file goes away first, as in the purge.
        # The survivor still claims the key.

        db.session.delete(goner)
        db.session.commit()
        assert untouched_key_still_claimed(shared) is True

        # The purge can delete the key only when no active record holds
        # it.

        keeper.aws_untouched_date_deleted = datetime.now()
        db.session.commit()
        assert untouched_key_still_claimed(shared) is False


def test_rename_untouched_object_moves_or_reuploads(app, monkeypatch):
    """Make sure the rename moves the object or uploads it again.

    The archive key changes only when a real object is at the new key.
    A STANDARD object goes through copy, verify, and delete. Fitzflix
    cannot copy a Deep Archive object or a missing object. Thus, the
    LOCAL library file force-uploads under the new key (decision by
    Glenn: close the invariant now, and do not hope that a later
    re-upload heals it)."""

    from app import aws_storage

    from app import db
    from app import videos
    from tests.factories import make_movie, make_movie_file

    class FakeS3:
        def __init__(self, storage_class="STANDARD", exists=True):
            self.storage_class = storage_class
            self.exists = exists
            self.copied = None
            self.deleted = None

        def head_object(self, Bucket, Key):
            if not self.exists and self.copied is None:
                raise RuntimeError("404")
            return {"ContentLength": 123, "StorageClass": self.storage_class}

        def copy(self, source, bucket, key):
            self.copied = (source["Key"], key)

        def delete_object(self, Bucket, Key):
            self.deleted = Key

    with app.app_context():
        file = make_movie_file(make_movie("Rename Subject", 2020), "DVD")
        file.aws_untouched_key = "untouched/old.mkv"
        db.session.commit()

        fake = FakeS3()
        monkeypatch.setattr(aws_storage, "aws_s3_client", lambda **kw: fake)
        assert videos.rename_untouched_object(file, "untouched/new.mkv") is True
        assert file.aws_untouched_key == "untouched/new.mkv"
        assert fake.copied == ("untouched/old.mkv", "untouched/new.mkv")
        assert fake.deleted == "untouched/old.mkv"

        # Deep Archive: a server-side copy is not possible. The library
        # copy force-uploads under the new key. The old object goes.

        from datetime import datetime

        uploads = []

        def fake_upload(path, prefix, key_name=None, **kw):
            uploads.append((path, prefix, key_name))
            return (f"{prefix}/{key_name}", datetime(2026, 8, 18), 999)

        file.aws_untouched_key = "untouched/frozen.mkv"
        fake = FakeS3(storage_class="DEEP_ARCHIVE")
        monkeypatch.setattr(aws_storage, "aws_s3_client", lambda **kw: fake)
        monkeypatch.setattr(aws_storage, "aws_upload", fake_upload)
        assert videos.rename_untouched_object(file, "untouched/thawed.mkv") is True
        assert file.aws_untouched_key == "untouched/thawed.mkv"
        assert file.aws_untouched_filesize_bytes == 999
        assert uploads[-1][2] == "thawed.mkv"
        assert fake.copied is None
        assert fake.deleted == "untouched/frozen.mkv"

        # Missing object: there is nothing to copy or delete. The upload
        # heals it.

        file.aws_untouched_key = "untouched/gone.mkv"
        fake = FakeS3(exists=False)
        monkeypatch.setattr(aws_storage, "aws_s3_client", lambda **kw: fake)
        assert videos.rename_untouched_object(file, "untouched/found.mkv") is True
        assert file.aws_untouched_key == "untouched/found.mkv"
        assert fake.deleted is None


def test_rename_untouched_object_defers_the_upload(app, monkeypatch):
    """Make sure rename_untouched_object can defer the upload.

    A caller on a queue with a short budget passes defer_upload. An
    object that Fitzflix cannot copy hands its multi-gigabyte re-upload
    to the file queue. The key does not change until that job
    completes. This is #231: the 10-minute limit of the sql queue killed
    a 43.9 GB upload at 84%."""

    from app import aws_storage

    from app import db
    from app import videos
    from tests.factories import make_movie, make_movie_file

    class FrozenS3:
        def __init__(self):
            self.deleted = None

        def head_object(self, Bucket, Key):
            return {"ContentLength": 123, "StorageClass": "DEEP_ARCHIVE"}

        def delete_object(self, Bucket, Key):
            self.deleted = Key

    with app.app_context():
        file = make_movie_file(make_movie("Deferred Subject", 2021), "Bluray-1080p")
        file.aws_untouched_key = "untouched/frozen.mkv"
        db.session.commit()

        fake = FrozenS3()
        monkeypatch.setattr(aws_storage, "aws_s3_client", lambda **kw: fake)
        monkeypatch.setattr(
            aws_storage,
            "aws_upload",
            lambda *a, **kw: pytest.fail("the upload should not run inline"),
        )

        assert (
            videos.rename_untouched_object(
                file, "untouched/thawed.mkv", defer_upload=True
            )
            is False
        )

        # Nothing changed yet. The key still names the real object. The
        # old object is still there. The deferred job deletes it.

        assert file.aws_untouched_key == "untouched/frozen.mkv"
        assert fake.deleted is None

        jobs = [
            job
            for job in app.file_queue.jobs
            if job.func_name == "app.videos.rearchive_untouched_object"
        ]
        assert len(jobs) == 1
        assert jobs[0].args == (file.id, "untouched/thawed.mkv")
        assert jobs[0].timeout == app.config["UPLOAD_TASK_TIMEOUT"]


def test_rearchive_untouched_object_uploads_or_skips(app, monkeypatch):
    """Make sure the re-archive uploads the object or skips it.

    The deferred half runs the force-upload in the 6-hour budget of the
    file queue. It skips a key that the record no longer wants. It does
    not spend tens of gigabytes on a stale name."""

    from datetime import datetime

    from app import aws_storage

    from app import db
    from app import videos
    from app.models import File
    from tests.factories import make_movie, make_movie_file

    class FrozenS3:
        def __init__(self):
            self.deleted = None

        def head_object(self, Bucket, Key):
            return {"ContentLength": 123, "StorageClass": "DEEP_ARCHIVE"}

        def delete_object(self, Bucket, Key):
            self.deleted = Key

    with app.app_context():
        file = make_movie_file(make_movie("Deferred Subject II", 2022), "Bluray-1080p")
        file.aws_untouched_key = "untouched/frozen.mkv"
        file.untouched_basename = "thawed.mkv"
        db.session.commit()
        file_id = file.id

        local_path = os.path.join(app.config["LIBRARY_DIR"], file.file_path)
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        with open(local_path, "wb") as handle:
            handle.write(b"payload")

        new_key = os.path.join(app.config["AWS_UNTOUCHED_PREFIX"], "thawed.mkv")

        uploads = []

        def fake_upload(path, prefix, key_name=None, **kw):
            uploads.append((path, prefix, key_name))
            return (os.path.join(prefix, key_name), datetime(2026, 8, 25), 999)

        fake = FrozenS3()
        monkeypatch.setattr(aws_storage, "aws_s3_client", lambda **kw: fake)
        monkeypatch.setattr(aws_storage, "aws_upload", fake_upload)

        assert videos.rearchive_untouched_object(file_id, new_key) is True

        # The task commits in the session of its own app context.

        db.session.expire_all()
        file = db.session.get(File, file_id)
        assert file.aws_untouched_key == new_key
        assert file.aws_untouched_filesize_bytes == 999
        assert uploads[-1][0] == local_path
        assert fake.deleted == "untouched/frozen.mkv"

        # This is a key that the record no longer wants. A later refresh
        # renamed it again, or the refresh that queued this job rolled
        # back.

        file.aws_untouched_key = "untouched/current.mkv"
        file.untouched_basename = "current.mkv"
        db.session.commit()

        uploads.clear()
        stale_key = os.path.join(app.config["AWS_UNTOUCHED_PREFIX"], "stale.mkv")
        assert videos.rearchive_untouched_object(file_id, stale_key) is False
        db.session.expire_all()
        assert file.aws_untouched_key == "untouched/current.mkv"
        assert uploads == []

        # A record with a local file that the user deleted on purpose has
        # nothing to upload again. It keeps the old key that still names
        # an object.

        os.remove(local_path)
        file.untouched_basename = "thawed.mkv"
        db.session.commit()
        assert (
            videos.rearchive_untouched_object(
                file_id, new_key, path_retries=aws_storage.MAX_REARCHIVE_PATH_RETRIES
            )
            is False
        )
        db.session.expire_all()
        assert file.aws_untouched_key == "untouched/current.mkv"
        assert uploads == []


def test_rearchive_waits_for_the_path_commit(app, monkeypatch):
    """Make sure the re-archive waits when the record path has no file.

    The refresh that queues the job renames the file on disk inside a
    transaction. On 2026-09-05 the job dequeued 11 ms after that rename,
    and before the commit. It read the old path, found no file, and gave
    up. The archive of the film kept the old name. Now the job returns
    to the queue and reads the record again after 5 minutes."""

    from app import aws_storage, db, retry_job_id, videos
    from app.models import File
    from tests.factories import make_movie, make_movie_file
    from tests.test_scheduling import scheduled_jobs

    with app.app_context():
        file = make_movie_file(make_movie("Deferred Subject III", 1943), "HDTV-720p")
        file.aws_untouched_key = "untouched/old.mkv"
        file.untouched_basename = "new.mkv"
        db.session.commit()
        file_id = file.id
        basename = file.basename
        new_key = os.path.join(app.config["AWS_UNTOUCHED_PREFIX"], "new.mkv")

        uploads = []
        monkeypatch.setattr(
            aws_storage, "aws_upload", lambda *a, **kw: uploads.append(a)
        )

        assert videos.rearchive_untouched_object(file_id, new_key) is False
        assert uploads == []

        retries = [
            job
            for job in scheduled_jobs(app.file_queue)
            if job.func_name == "app.videos.rearchive_untouched_object"
            and job.args[0] == file_id
        ]
        assert [job.id for job in retries] == [
            retry_job_id("rearchive_untouched_object", f"'{basename}'", 1)
        ]
        assert retries[0].args[1] == new_key
        assert retries[0].kwargs == {"path_retries": 1}
        assert retries[0].timeout == app.config["UPLOAD_TASK_TIMEOUT"]

        # The last attempt gives up. The record keeps the old key.

        assert (
            videos.rearchive_untouched_object(
                file_id, new_key, path_retries=aws_storage.MAX_REARCHIVE_PATH_RETRIES
            )
            is False
        )
        db.session.expire_all()
        assert db.session.get(File, file_id).aws_untouched_key == "untouched/old.mkv"
        assert not any(
            job.args[0] == file_id and job.kwargs.get("path_retries", 0) > 1
            for job in scheduled_jobs(app.file_queue)
            if job.func_name == "app.videos.rearchive_untouched_object"
        )


def test_rearchive_keeps_webdl_scaffold_keys(app, monkeypatch):
    """Make sure the re-archive keeps a WEBDL scaffold key.

    This is the WEBDL-rebuild scaffold (#158). A WEBRip row keeps its
    WEBDL-named archive key on purpose until a real WEB-DL replaces it.
    The deferred re-archive refuses that trade. It does not upload
    gigabytes again and retire the scaffold key. The genre backfill of
    2026-08-29 started to do exactly that."""

    from app import aws_storage

    from app import db
    from app import videos
    from app.models import File
    from tests.factories import make_movie, make_movie_file

    with app.app_context():
        file = make_movie_file(make_movie("Scaffold Subject", 1964), "WEBRip-1080p")
        file.aws_untouched_key = "untouched/Scaffold Subject (1964) - [WEBDL-1080p].mkv"
        file.untouched_basename = "Scaffold Subject (1964) - [WEBRip-1080p].mkv"
        db.session.commit()
        file_id = file.id

        new_key = os.path.join(
            app.config["AWS_UNTOUCHED_PREFIX"],
            "Scaffold Subject (1964) - [WEBRip-1080p].mkv",
        )
        monkeypatch.setattr(
            aws_storage,
            "aws_upload",
            lambda *a, **kw: pytest.fail("the scaffold re-upload must not run"),
        )
        monkeypatch.setattr(
            aws_storage,
            "aws_s3_client",
            lambda **kw: pytest.fail("no S3 calls for a scaffold key"),
        )

        assert videos.rearchive_untouched_object(file_id, new_key) is False

        db.session.expire_all()
        file = db.session.get(File, file_id)
        assert (
            file.aws_untouched_key
            == "untouched/Scaffold Subject (1964) - [WEBDL-1080p].mkv"
        )
