"""The weekly orphaned-file cleanup: age-gated deletion of the hidden
partials that failed tasks strand, with macOS metadata and anything fresh
or visible left alone.
"""

import os
import time

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
        # Under the age gate: might still be an in-flight copy
        plant(os.path.join(staging, ".Copying (2021) - [DVD].mkv"), 1),
        # Visible files are never touched, however old
        plant(
            os.path.join(movies, "Stranded (2020)", "Stranded (2020) - [DVD].mkv"), 30
        ),
        # macOS metadata isn't a pipeline strand
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
    """The leftover-directory pass (#66 follow-up): week-undisturbed
    directories fall when empty OR holding only junk — @eaDir trees,
    macOS metadata, a few aged stray images (an orphaned custom
    poster) — while anything fresh, real, or picture-collection-sized
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

    # An aged empty tree: child and parent both go in ONE pass (mtimes
    # are captured before removals begin)

    nested_child = make_dir(os.path.join(movies, "Gone (1999)", "Extras"), aged=True)
    nested_parent = os.path.join(movies, "Gone (1999)")
    age(nested_parent)

    # Junk-anchored leftovers all fall: metadata-only, an orphaned
    # poster, and a Synology @eaDir whose contents stay fresh (the NAS
    # rewrites them on its own schedule — that must not immortalize a
    # dead folder)

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

    # Survivors: freshly emptied, a real file, a FRESH poster (recent
    # human action), and an image trove past the cap

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
        assert os.path.isdir(movies)  # the root itself is never a candidate

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


def test_cleanup_stays_quiet_when_nothing_is_stranded(app, monkeypatch):
    sent = []
    monkeypatch.setattr(
        maintenance, "task_send_email", lambda *args, **kwargs: sent.append(1)
    )

    maintenance.cleanup_orphaned_files()

    assert sent == []


def test_scratch_db_drop_is_skipped_off_mysql(app):
    """The test suite runs on SQLite, where there is no scratch database
    to drop — the helper must notice and do nothing."""

    with app.app_context():
        assert maintenance._drop_leftover_restore_database() is False


def test_shared_untouched_key_is_never_deleted(app):
    """Two file records can claim the same untouched S3 key — a
    repointed key after a rename, or a re-import landing on the same
    basename (#64, the Bambi II incident). The purge guard reports a
    key as claimed until no surviving record holds it."""

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

        # The worse file's row goes away first (as the purge does);
        # the survivor still claims the key

        db.session.delete(goner)
        db.session.commit()
        assert untouched_key_still_claimed(shared) is True

        # Only when no active record holds it may the key be deleted

        keeper.aws_untouched_date_deleted = datetime.now()
        db.session.commit()
        assert untouched_key_still_claimed(shared) is False


def test_rename_untouched_object_moves_or_reuploads(app, monkeypatch):
    """The archive key only ever changes when a real object sits at the
    new key (#64): STANDARD objects copy-verify-delete; Deep Archive
    and missing objects can't be copied, so the LOCAL library file
    force-uploads under the new key instead (Glenn's call — close the
    invariant now rather than hope a future re-upload heals it)."""

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
        monkeypatch.setattr(videos, "aws_s3_client", lambda **kw: fake)
        assert videos.rename_untouched_object(file, "untouched/new.mkv") is True
        assert file.aws_untouched_key == "untouched/new.mkv"
        assert fake.copied == ("untouched/old.mkv", "untouched/new.mkv")
        assert fake.deleted == "untouched/old.mkv"

        # Deep Archive: no server-side copy possible — the library
        # copy force-uploads under the new key and the old object goes

        from datetime import datetime

        uploads = []

        def fake_upload(path, prefix, key_name=None, **kw):
            uploads.append((path, prefix, key_name))
            return (f"{prefix}/{key_name}", datetime(2026, 8, 18), 999)

        file.aws_untouched_key = "untouched/frozen.mkv"
        fake = FakeS3(storage_class="DEEP_ARCHIVE")
        monkeypatch.setattr(videos, "aws_s3_client", lambda **kw: fake)
        monkeypatch.setattr(videos, "aws_upload", fake_upload)
        assert videos.rename_untouched_object(file, "untouched/thawed.mkv") is True
        assert file.aws_untouched_key == "untouched/thawed.mkv"
        assert file.aws_untouched_filesize_bytes == 999
        assert uploads[-1][2] == "thawed.mkv"
        assert fake.copied is None
        assert fake.deleted == "untouched/frozen.mkv"

        # Missing object: nothing to copy or delete — heal by upload

        file.aws_untouched_key = "untouched/gone.mkv"
        fake = FakeS3(exists=False)
        monkeypatch.setattr(videos, "aws_s3_client", lambda **kw: fake)
        assert videos.rename_untouched_object(file, "untouched/found.mkv") is True
        assert file.aws_untouched_key == "untouched/found.mkv"
        assert fake.deleted is None
