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
