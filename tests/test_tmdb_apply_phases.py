"""Test the 2 phases of a TMDB refresh (app/tmdb_refresh.py).

refresh_tmdb_info fetches. apply_tmdb_refresh applies the payload on
the sql queue. Both phases find the record of the refresh the same
way. The apply holds the title locks of the merge, and it saves a
payload whose apply raises.
"""

import json
import os
import zlib

from app import db
from tests.factories import make_movie, make_movie_file, make_tv_series
from tests.fakes import Response
from tests.test_scheduling import assert_binds, requeue_id, scheduled_jobs


def test_tmdb_refresh_hands_off_to_sql_queue(app):
    """Test that the TMDB refresh hands off to the sql queue.

    The fetch phase runs on the multi-worker request queue. Each
    database write occurs in apply_tmdb_refresh on the single-worker
    sql queue. Thus, concurrent fetches cannot cause concurrent writes."""

    from app import db
    from app.videos import refresh_tmdb_info
    from tests.factories import make_movie

    with app.app_context():
        movie = make_movie("Handoff Film", 2003)
        db.session.commit()
        movie_id = movie.id

        # With no TMDB_API_KEY, the fetch finds nothing. The result says
        # so. But the task must still enqueue the apply job. That job also
        # rewrites file paths

        from app.tmdb_refresh import TMDB_NO_MATCH

        assert refresh_tmdb_info("Movies", movie_id) == TMDB_NO_MATCH

        jobs = [
            job
            for job in app.sql_queue.jobs
            if job.func_name == "app.videos.apply_tmdb_refresh"
            and job.kwargs.get("id") == movie_id
        ]
        assert len(jobs) == 1
        assert jobs[0].kwargs["library"] == "Movies"
        assert jobs[0].kwargs["tmdb_payload"] is None
        assert_binds(jobs[0])


def test_apply_tmdb_refresh_round_trips_payload(app):
    """Test that the payload survives the round trip.

    The fetch phase builds the compressed payload. The database phase
    decompresses and applies it."""

    import zlib

    from app import db
    from app.models import TVSeries
    from app.videos import apply_tmdb_refresh
    from tests.factories import make_tv_series

    with app.app_context():
        tv = make_tv_series("Payload Show")
        db.session.commit()
        tv_id = tv.id

        payload = zlib.compress(
            json.dumps({"id": 999, "name": "Payload Show Canonical"}).encode("utf-8")
        )
        assert apply_tmdb_refresh("TV Shows", tv_id, tmdb_payload=payload) is True

        db.session.expire_all()
        refreshed = TVSeries.query.filter_by(id=tv_id).first()
        assert refreshed.tmdb_id == 999
        assert refreshed.tmdb_name == "Payload Show Canonical"


def test_tmdb_apply_requests_the_archive_move_after_the_commit(app, monkeypatch):
    """Test that a movie rename requests the archive move, and queues it at the commit.

    The move job reads the target from its own session. On 2026-09-05 a
    job dequeued 11 ms after the disk rename, before the commit. Thus,
    the job goes to the queue only when the target is committed (#369).
    A Deep Archive object moves too: the move does not need the
    library file."""

    from datetime import date

    from app import db
    from app.models import File
    from app.videos import apply_tmdb_refresh
    from tests.factories import make_movie, make_movie_file

    with app.app_context():
        movie = make_movie("Desert Tune", 1943)
        file = make_movie_file(movie, "HDTV-720p")
        file.untouched_basename = file.basename
        file.aws_key = f"untouched/{file.basename}"
        file.aws_date_uploaded = date(2026, 1, 1)
        movie.tmdb_title = "Desert Tune"
        movie.tmdb_release_date = date(1944, 1, 1)
        db.session.commit()
        movie_id = movie.id
        file_id = file.id

        old_path = os.path.join(app.config["LIBRARY_DIR"], file.file_path)
        os.makedirs(os.path.dirname(old_path), exist_ok=True)
        with open(old_path, "wb") as handle:
            handle.write(b"payload")

        seen = []
        real_enqueue = app.file_queue.enqueue

        def spy(name, *args, **kwargs):
            if name == "app.aws_storage.move_archive_object":
                # Record what the job sees from another connection.
                with db.engine.connect() as connection:
                    committed = connection.execute(
                        db.text("SELECT aws_key_target FROM file WHERE id = :id"),
                        {"id": file_id},
                    ).scalar()
                seen.append((kwargs["args"], committed))
            return real_enqueue(name, *args, **kwargs)

        monkeypatch.setattr(app.file_queue, "enqueue", spy)

        assert apply_tmdb_refresh("Movies", movie_id) is True

        new_key = "untouched/Desert Tune (1944) - [HDTV-720p].mkv"
        assert seen == [((file_id,), new_key)]
        db.session.expire_all()
        moved = db.session.get(File, file_id)
        assert moved.aws_key == "untouched/Desert Tune (1943) - [HDTV-720p].mkv"
        assert moved.aws_key_target == new_key
        assert not os.path.exists(old_path)


def test_tmdb_apply_defers_while_title_is_locked(app, held_lock):
    """Test that the TMDB apply defers when the title is locked.

    The TMDB apply of a movie rewrites file paths and can merge records.
    Thus, it must wait for an import chain that holds a lock of the
    title."""

    from app import db
    from app.videos import apply_tmdb_refresh
    from tests.factories import make_movie, make_movie_file

    with app.app_context():
        movie = make_movie("Locked Film", 1999)
        file = make_movie_file(movie, "DVD")
        db.session.commit()
        movie_id = movie.id

        held_lock(file.file_identifier())

        assert apply_tmdb_refresh("Movies", movie_id) is False

        retries = [
            job
            for job in scheduled_jobs(app.sql_queue)
            if job.id.startswith("retry_apply_tmdb_refresh")
        ]
        assert [job.id for job in retries] == [
            requeue_id("app.videos.apply_tmdb_refresh", f"Movies:{movie_id}")
        ]
        job = retries[0]
        assert job.kwargs["library"] == "Movies"
        assert job.kwargs["id"] == movie_id
        assert_binds(job)


def test_tmdb_apply_locks_the_merge_target_too(app, held_lock):
    """Test that the TMDB apply also locks the merge target.

    A TMDB id can point at an existing movie. Then the apply merges the
    records. Thus, a lock on the files of the *target* movie defers the
    apply."""

    from app import db
    from app.videos import apply_tmdb_refresh
    from tests.factories import make_movie, make_movie_file

    with app.app_context():
        source = make_movie("Duplicate Entry", 2001)
        make_movie_file(source, "DVD")
        target = make_movie("Canonical Entry", 2001, tmdb_id=4242)
        target_file = make_movie_file(target, "Bluray-1080p")
        db.session.commit()
        source_id = source.id

        held_lock(target_file.file_identifier())

        assert apply_tmdb_refresh("Movies", source_id, tmdb_id=4242) is False
        assert any(
            job.id == requeue_id("app.videos.apply_tmdb_refresh", f"Movies:{source_id}")
            for job in scheduled_jobs(app.sql_queue)
        )


def test_tmdb_apply_releases_locks_when_done(app):
    from app import db
    from app.videos import apply_tmdb_refresh
    from tests.factories import make_movie, make_movie_file

    with app.app_context():
        movie = make_movie("Free Film", 2002)
        file = make_movie_file(
            movie, "DVD", untouched_basename="Free Film (2002) - [DVD].mkv"
        )
        db.session.commit()
        movie_id = movie.id
        identifier = file.file_identifier()

        assert apply_tmdb_refresh("Movies", movie_id) is True

        lock = app.lock_manager.lock(identifier, 1000)
        assert lock, "apply did not release the title lock"
        app.lock_manager.unlock(lock)


def test_apply_tmdb_refresh_saves_the_payload_when_apply_raises(app, monkeypatch):
    """Test that apply_tmdb_refresh saves the payload when the apply raises.

    The task writes the payload next to the log as gzipped JSON named
    for the record. Thus, you can examine a transient upstream glitch
    (the malformed aggregate credits of 2026-08-22) after TMDB serves
    clean data again."""

    import glob
    import gzip
    import zlib

    from app import db
    from app.models import TVSeries
    from app.videos import apply_tmdb_refresh
    from tests.factories import make_tv_series

    with app.app_context():
        tv = make_tv_series("Glitch Show")
        db.session.commit()
        tv_id = tv.id

        def explode(self, tmdb_info):
            raise AttributeError("'list' object has no attribute 'get'")

        monkeypatch.setattr(TVSeries, "tmdb_tv_apply", explode)

        info = {"id": 4242, "aggregate_credits": {"cast": [{"roles": [["bad"]]}]}}
        payload = zlib.compress(json.dumps(info).encode("utf-8"))
        assert apply_tmdb_refresh("TV Shows", tv_id, tmdb_payload=payload) is False

        pattern = f"{app.config['LOG_FILE']}.tmdb-payload.tv-{tv_id}.*.json.gz"
        dumps = glob.glob(pattern)
        assert len(dumps) == 1
        with gzip.open(dumps[0], "rb") as saved:
            assert json.loads(saved.read()) == info


def _payload(details):
    return zlib.compress(json.dumps(details).encode("utf-8"))


def test_both_phases_decline_a_record_that_is_gone(app):
    from app.tmdb_refresh import apply_tmdb_refresh, refresh_tmdb_info

    for library in ("Movies", "TV Shows"):
        assert refresh_tmdb_info(library, 999999) is False
        assert apply_tmdb_refresh(library, 999999, tmdb_payload=_payload({})) is False
    assert refresh_tmdb_info("Music", 1) is False


def test_a_series_refresh_works_on_the_record_that_owns_the_tmdb_id(app, monkeypatch):
    """A series that shares its TMDB id with an older record refreshes that record."""

    from app.tmdb_refresh import apply_tmdb_refresh, refresh_tmdb_info

    searched = []

    def fake_get(url, params=None, **kwargs):
        searched.append(params.get("query"))
        if "/search/tv" in url:
            return Response({"results": [{"id": 555}]})
        return Response({"id": 555, "name": "Canonical Show"})

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "key")
    monkeypatch.setattr("app.tmdb_apply.tmdb_get", fake_get)
    with app.app_context():
        canonical = make_tv_series("Canonical Show", tmdb_id=555)
        duplicate = make_tv_series("Duplicate Show", tmdb_id=555)
        db.session.commit()
        canonical_id, duplicate_id = canonical.id, duplicate.id

    assert refresh_tmdb_info("TV Shows", duplicate_id) is True
    assert searched[0] == "Canonical Show"
    jobs = [
        job
        for job in app.sql_queue.jobs
        if job.func_name == "app.videos.apply_tmdb_refresh"
    ]
    assert [job.description for job in jobs] == [
        "Updating 'Canonical Show' with TMDB data"
    ]

    payload = _payload({"id": 555, "name": "Canonical Show (TMDB)"})
    assert apply_tmdb_refresh("TV Shows", duplicate_id, tmdb_payload=payload) is True
    with app.app_context():
        from app.models import TVSeries

        assert db.session.get(TVSeries, canonical_id).tmdb_name == (
            "Canonical Show (TMDB)"
        )
        assert db.session.get(TVSeries, duplicate_id).tmdb_name is None


def test_a_merge_moves_the_files_and_every_review(app):
    """The apply merges a duplicate into the record that owns the TMDB id."""

    from app.models import File, Movie, User, UserMovieReview
    from app.tmdb_refresh import apply_tmdb_refresh

    with app.app_context():
        user = User.query.first()
        source = make_movie("Merge Source", 2001)
        file = make_movie_file(source, "DVD")
        file.untouched_basename = file.basename
        target = make_movie("Merge Target", 2001, tmdb_id=4243)
        for text in ("first", "second"):
            db.session.add(
                UserMovieReview(user_id=user.id, movie_id=source.id, review=text)
            )
        db.session.commit()
        source_id, target_id, file_id = source.id, target.id, file.id

    payload = _payload(
        {"id": 4243, "title": "Merge Target", "release_date": "2001-05-01"}
    )
    assert (
        apply_tmdb_refresh("Movies", source_id, tmdb_id=4243, tmdb_payload=payload)
        is True
    )
    with app.app_context():
        assert db.session.get(Movie, source_id) is None
        assert db.session.get(File, file_id).movie_id == target_id
        assert UserMovieReview.query.filter_by(movie_id=target_id).count() == 2


def test_a_rename_onto_a_claimed_path_is_refused(app, monkeypatch):
    """A sibling row on the new path stops the rename and emails the admin."""

    import app.tmdb_refresh as tmdb_refresh
    from app.tmdb_refresh import apply_tmdb_refresh

    sent = []
    monkeypatch.setattr(
        tmdb_refresh, "notify_admin_text", lambda subject, *a: sent.append(subject)
    )
    with app.app_context():
        movie = make_movie("Old Name", 1999)
        file = make_movie_file(movie, "DVD")
        file.untouched_basename = file.basename
        other = make_movie("Claimant", 1998)
        sibling = make_movie_file(other, "DVD", plex_title="New Name (1999)")
        sibling.file_path = "Movies/New Name (1999)/New Name (1999) - [DVD].mkv"
        db.session.commit()
        movie_id, file_id = movie.id, file.id
        old_path = file.file_path

    payload = _payload({"id": 777, "title": "New Name", "release_date": "1999-02-02"})
    assert apply_tmdb_refresh("Movies", movie_id, tmdb_payload=payload) is True
    with app.app_context():
        from app.models import File

        assert db.session.get(File, file_id).file_path == old_path
    assert sent == ["Rename collision needs triage"]


def _failing_commits(monkeypatch, failures):
    """Make the next failures commits of the session raise. Return the call list."""

    real_commit = db.session.commit
    calls = []

    def commit():
        calls.append(1)
        if len(calls) <= failures:
            raise RuntimeError("Lock wait timeout exceeded")
        return real_commit()

    monkeypatch.setattr(db.session, "commit", commit)
    return calls


def test_a_rename_whose_commit_fails_moves_the_file_back(app, monkeypatch):
    """The row rolls back. The file must not stay at the new path (#380)."""

    from app.tmdb_refresh import _rename_movie_file

    with app.app_context():
        movie = make_movie("Desert Tune", 1943)
        file = make_movie_file(movie, "HDTV-720p")
        file.untouched_basename = "Desert Tune (1944) - [HDTV-720p].mkv"
        db.session.commit()
        old_path = os.path.join(app.config["LIBRARY_DIR"], file.file_path)
        os.makedirs(os.path.dirname(old_path), exist_ok=True)
        with open(old_path, "wb") as handle:
            handle.write(b"payload")
        new_path = os.path.join(
            app.config["LIBRARY_DIR"],
            "Movies/Desert Tune (1944)/Desert Tune (1944) - [HDTV-720p].mkv",
        )

        # The SQLite driver of the tests releases a savepoint as a commit.
        # Thus, the test checks the disk. MariaDB rolls the row back.

        calls = _failing_commits(monkeypatch, 1)
        assert _rename_movie_file(file, None) is False
        assert calls
        assert os.path.isfile(old_path)
        assert not os.path.exists(new_path)


def test_an_apply_whose_commit_fails_stops(app, monkeypatch):
    """The new details rolled back. The names must not come from the old ones (#380)."""

    from app.tmdb_refresh import _apply_movie_refresh

    from app.models import File
    from app.queues import roster_queues

    with app.app_context():
        movie = make_movie("Old Name", 1999)
        file = make_movie_file(movie, "DVD")
        db.session.commit()
        file_id, old_path = file.id, file.file_path

        _failing_commits(monkeypatch, 1)
        locks = []
        try:
            result = _apply_movie_refresh(
                movie,
                "Refreshing 'Old Name (1999)'",
                None,
                {"id": 778, "title": "New Name", "release_date": "1999-02-02"},
                None,
                False,
                locks,
            )
        finally:
            for lock in locks:
                app.lock_manager.unlock(lock)
        assert result is False

        # The run stopped. The record keeps its path, and no follow-up job
        # is queued with the old details.
        monkeypatch.undo()
        db.session.rollback()
        db.session.expire_all()
        assert db.session.get(File, file_id).file_path == old_path
        assert [job for queue in roster_queues(app) for job in queue.jobs] == []


def test_a_rename_waits_for_a_dead_library_volume(app, monkeypatch):
    """A dropped share hides both paths (#551).

    The rename must not make the folder on the boot disk, and the row
    must not move with no file."""

    import app.videos as videos
    from app.models import File
    from app.tmdb_refresh import VOLUME_DOWN, _rename_movie_file

    monkeypatch.setattr(videos, "_dead_volumes", lambda paths: ["/Volumes/Movies"])
    with app.app_context():
        movie = make_movie("Dropped Tune", 1943)
        file = make_movie_file(movie, "HDTV-720p")
        file.untouched_basename = "Dropped Tune (1944) - [HDTV-720p].mkv"
        db.session.commit()
        file_id, old_relative = file.id, file.file_path
        new_folder = os.path.join(
            app.config["LIBRARY_DIR"], "Movies/Dropped Tune (1944)"
        )

        assert _rename_movie_file(file, None) == VOLUME_DOWN
        assert not os.path.exists(new_folder)
        db.session.expire_all()
        assert db.session.get(File, file_id).file_path == old_relative


def test_a_movie_rename_on_a_dead_volume_books_the_apply_again(app, monkeypatch):
    """The apply comes back for the file that the volume kept (follow-up #551).

    Before, the title and the untouched name committed. The folder kept
    the old title until TMDB changed the film again."""

    import app.videos as videos
    from app.videos import apply_tmdb_refresh

    monkeypatch.setattr(videos, "_dead_volumes", lambda paths: ["/Volumes/Movies"])
    with app.app_context():
        movie = make_movie("Volume Tune", 1943)
        file = make_movie_file(movie, "HDTV-720p")
        file.untouched_basename = file.basename
        movie.year = 1944
        db.session.commit()
        movie_id, old_path = movie.id, file.file_path

        assert apply_tmdb_refresh("Movies", movie_id) is True
        retries = [
            job
            for queue in (app.sql_queue, app.request_queue, app.maintenance_queue)
            for job in scheduled_jobs(queue)
            if job.func_name == "app.videos.apply_tmdb_refresh"
        ]
        assert [job.kwargs["volume_waits"] for job in retries] == [1]
        assert retries[0].kwargs["id"] == movie_id
        from app.models import File

        db.session.expire_all()
        assert db.session.get(File, file.id).file_path == old_path
        for job in retries:
            job.delete()


def test_a_movie_apply_probes_the_volume_once_before_any_stat(app, monkeypatch):
    """The collision stat ran before the dead-volume probe (#575).

    A hung share blocks that stat. Each file also probed on its own
    while the title locks were held. Now 1 probe runs first."""

    import app.tmdb_refresh as tmdb_refresh
    import app.videos as videos

    probes = []
    monkeypatch.setattr(
        videos, "_dead_volumes", lambda paths: probes.append(paths) or ["/Volumes/M"]
    )
    stats = []
    real_exists = os.path.exists
    monkeypatch.setattr(
        tmdb_refresh.os.path,
        "exists",
        lambda path: stats.append(path) or real_exists(path),
    )
    renames = []
    monkeypatch.setattr(
        tmdb_refresh,
        "_rename_movie_file",
        lambda f, tmdb_id, probe=True: renames.append(f.id),
    )
    monkeypatch.setattr(tmdb_refresh, "_requeue_for_the_volume", lambda *a: None)
    with app.app_context():
        movie = make_movie("Probe Once", 1960)
        for quality in ("DVD", "Bluray-1080p"):
            file = make_movie_file(movie, quality)
            file.untouched_basename = file.basename
        db.session.commit()
        locks = []
        try:
            tmdb_refresh._apply_movie_refresh(
                movie,
                "Refreshing 'Probe Once (1960)'",
                None,
                {"id": 779, "title": "Probe Once", "release_date": "1960-02-02"},
                None,
                False,
                locks,
            )
        finally:
            for lock in locks:
                app.lock_manager.unlock(lock)
    assert len(probes) == 1
    assert renames == []


def test_a_dead_volume_skips_the_collision_stat_of_a_rename(app, monkeypatch):
    """The probe of _rename_movie_file comes before os.path.exists (#575)."""

    import app.tmdb_refresh as tmdb_refresh
    import app.videos as videos
    from app.tmdb_refresh import VOLUME_DOWN, _rename_movie_file

    monkeypatch.setattr(videos, "_dead_volumes", lambda paths: ["/Volumes/Movies"])
    with app.app_context():
        movie = make_movie("Hung Tune", 1943)
        file = make_movie_file(movie, "HDTV-720p")
        file.untouched_basename = "Hung Tune (1944) - [HDTV-720p].mkv"
        db.session.commit()

        library = app.config["LIBRARY_DIR"]
        real_exists = os.path.exists
        stats = []

        def hung(path):
            if str(path).startswith(library):
                stats.append(path)
            return real_exists(path)

        monkeypatch.setattr(tmdb_refresh.os.path, "exists", hung)
        outcome = _rename_movie_file(file, None)
        monkeypatch.undo()
        assert outcome == VOLUME_DOWN
        assert stats == []
