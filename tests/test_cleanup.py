"""Test the weekly orphaned-file cleanup.

The cleanup deletes the hidden partial files that failed tasks leave
behind, after an age gate. It does not touch macOS metadata, a fresh
file, or a visible file.
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

    doomed = [
        plant(os.path.join(staging, ".Stranded (2020) - [DVD].mkv"), 8),
        plant(os.path.join(staging, ".Stranded (2020) - [DVD].mkv.convert.mkv"), 8),
        plant(
            os.path.join(movies, "Stranded (2020)", ".Stranded (2020) - [DVD].mkv"), 8
        ),
        plant(os.path.join(rejects, "exception", ".Bad (2020).mkv.partial"), 8),
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
        assert "4 orphaned partial file(s)" in sent[0]["body"]
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
        f"/tv/{series_id}/delete",
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


def test_deleting_a_special_returns_to_season_0(app, admin_client):
    """Season 0 holds the specials. The delete returns to its season page."""

    from app import db
    from tests.factories import make_tv_file, make_tv_series

    with app.app_context():
        series = make_tv_series("Special Show")
        file = make_tv_file(series, 0, 1, "DVD")
        db.session.commit()
        file_id, series_id = file.id, series.id
        file_path = file.file_path

    plant(os.path.join(app.config["LIBRARY_DIR"], file_path))
    token = _admin_csrf_token(admin_client, f"/file/{file_id}")
    response = admin_client.post(
        f"/file/{file_id}",
        data={"csrf_token": token, "delete_submit": "Delete File"},
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith(f"/tv/{series_id}/0")


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
        keeper.aws_key = shared
        goner.aws_key = shared
        db.session.commit()

        assert untouched_key_still_claimed(shared) is True

        # The row of the worse file goes away first, as in the purge.
        # The survivor still claims the key.

        db.session.delete(goner)
        db.session.commit()
        assert untouched_key_still_claimed(shared) is True

        # The purge can delete the key only when no active record holds
        # it.

        keeper.aws_date_deleted = datetime.now()
        db.session.commit()
        assert untouched_key_still_claimed(shared) is False


def test_cleanup_keeps_the_input_of_a_waiting_conversion(app, monkeypatch):
    """A conversion that waits over 7 days keeps its hidden input (#436)."""

    from datetime import timedelta

    monkeypatch.setattr(maintenance, "task_send_email", lambda *a, **k: None)
    staging = app.config["STAGING_DIR"]
    waiting = plant(os.path.join(staging, ".Waiting (2020) - [DVD].mkv"), 9)
    stray = plant(os.path.join(staging, ".Stray (2020) - [DVD].mkv"), 9)
    moving = plant(os.path.join(staging, ".Deferred (2020) - [DVD].mkv"), 9)
    app.transcode_queue.enqueue(
        "app.videos.convert_localized_file",
        "/import/Waiting (2020) - [Remux-2160p].mkv",
        {},
        None,
        waiting,
        {},
    )
    app.handoff_queue.enqueue_in(
        timedelta(minutes=30),
        "app.videos.move_localized_file",
        "/import/Deferred (2020) - [DVD].mkv",
        {},
        None,
        moving,
    )
    try:
        maintenance.cleanup_orphaned_files()
        assert os.path.exists(waiting)
        assert os.path.exists(moving)
        assert not os.path.exists(stray)
    finally:
        for path in (waiting, stray, moving):
            if os.path.exists(path):
                os.remove(path)
