"""Test the series rename.

The tests cover the disk moves, the row rewrites, the resume semantics,
and the refusal guards. The rename never writes aws_key. It requests
the archive moves, and the move job writes the key (#369)."""

import contextlib
import os

from sqlalchemy import event

from app import db
from app.models import File
from app.series_rename import rename_tv_series_task

from tests.factories import make_tv_file, make_tv_series


def _materialize(app, file):
    path = os.path.join(app.config["LIBRARY_DIR"], file.file_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        handle.write("video")
    return path


@contextlib.contextmanager
def case_blind_titles(app):
    """Compare tv_series.title without case, as the MySQL general_ci
    collation does. SQLite compares it byte for byte."""

    def rewrite(conn, cursor, statement, parameters, context, executemany):
        return (
            statement.replace(
                "tv_series.title = ?", "tv_series.title = ? COLLATE NOCASE"
            ),
            parameters,
        )

    engine = db.engine
    event.listen(engine, "before_cursor_execute", rewrite, retval=True)
    try:
        yield
    finally:
        event.remove(engine, "before_cursor_execute", rewrite)


def test_rename_moves_files_and_rewrites_rows(app):
    with app.app_context():
        series = make_tv_series("Batman", tmdb_id=2287)
        one = make_tv_file(series, 1, 1, "Bluray-1080p")
        two = make_tv_file(series, 2, 3, "Bluray-1080p")
        one.aws_key = "untouched/Batman - S01E01 - [Bluray-1080p].mkv"
        db.session.commit()
        old_paths = [_materialize(app, f) for f in (one, two)]
        series_id = series.id

        assert rename_tv_series_task(series_id, "Batman (1966)") is True

        # The task commits on its own app-context session. Remove the
        # cached instances of this session before you read again
        db.session.expire_all()
        one = db.session.get(File, one.id)
        assert one.basename == "Batman (1966) - S01E01 - [Bluray-1080p].mkv"
        assert one.dirname == "TV Shows/Batman (1966)/Season 01"
        assert one.file_path == (
            "TV Shows/Batman (1966)/Season 01/"
            "Batman (1966) - S01E01 - [Bluray-1080p].mkv"
        )
        assert one.plex_title == "Batman (1966) - S01E01"

        # The task intentionally does not change the S3 key
        assert one.aws_key == ("untouched/Batman - S01E01 - [Bluray-1080p].mkv")

        for old in old_paths:
            assert not os.path.exists(old)
        for file in (one, db.session.get(File, two.id)):
            assert os.path.isfile(
                os.path.join(app.config["LIBRARY_DIR"], file.file_path)
            )
        assert db.session.get(type(series), series_id).title == "Batman (1966)"
        # The old series directory became empty, and the task removed it
        assert not os.path.isdir(
            os.path.join(app.config["LIBRARY_DIR"], "TV Shows/Batman")
        )


def test_rename_resumes_and_handles_archived_only_rows(app):
    with app.app_context():
        series = make_tv_series("Batman", tmdb_id=2287)
        moved = make_tv_file(series, 1, 1, "Bluray-1080p")
        archived_only = make_tv_file(series, 1, 2, "Bluray-1080p")
        db.session.commit()

        # Put the first file at its TARGET before the run. An earlier run
        # that crashed does the same. The second file has no local file
        target = os.path.join(
            app.config["LIBRARY_DIR"],
            "TV Shows/Batman (1966)/Season 01/"
            "Batman (1966) - S01E01 - [Bluray-1080p].mkv",
        )
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w") as handle:
            handle.write("video")

        assert rename_tv_series_task(series.id, "Batman (1966)") is True
        db.session.expire_all()
        assert db.session.get(File, moved.id).basename.startswith("Batman (1966)")
        assert db.session.get(File, archived_only.id).basename.startswith(
            "Batman (1966)"
        )


def test_rename_refuses_collisions_and_bad_names(app):
    with app.app_context():
        series = make_tv_series("Batman", tmdb_id=2287)
        make_tv_series("Batman (1966)")
        db.session.commit()

        assert rename_tv_series_task(series.id, "Batman (1966)") is False
        assert rename_tv_series_task(series.id, "Batman") is False
        assert rename_tv_series_task(series.id, "Batman: 1966?") is False
        assert db.session.get(type(series), series.id).title == "Batman"


def test_a_case_only_rename_is_not_a_collision_with_itself(app):
    """Rename a series when only the case of the title changes (#408).

    The title check compares without case. It must not find the series
    itself."""

    with app.app_context():
        series = make_tv_series("The office", tmdb_id=2316)
        file = make_tv_file(series, 1, 1, "Bluray-1080p")
        db.session.commit()
        _materialize(app, file)
        series_id = series.id

        with case_blind_titles(app):
            assert rename_tv_series_task(series_id, "The Office") is True

        db.session.expire_all()
        assert db.session.get(type(series), series_id).title == "The Office"


def test_an_accent_only_rename_moves_the_files(app):
    """Rename an accented title to its plain form (#408).

    A rename never writes accented letters. The reverse direction is
    the accent-only rename that the task can do."""

    with app.app_context():
        series = make_tv_series("Les Misérables", tmdb_id=2317)
        file = make_tv_file(series, 1, 1, "Bluray-1080p")
        db.session.commit()
        _materialize(app, file)
        series_id, file_id = series.id, file.id

        with case_blind_titles(app):
            assert rename_tv_series_task(series_id, "Les Miserables") is True

        db.session.expire_all()
        assert db.session.get(type(series), series_id).title == "Les Miserables"
        assert db.session.get(File, file_id).basename.startswith("Les Miserables")


def test_rename_keeps_the_id_tag_of_the_folder(app, monkeypatch):
    """A tagged folder is the title and a tag. It must match (#317)."""

    import app.series_rename as series_rename

    sonarr = []
    monkeypatch.setattr(
        series_rename,
        "_update_sonarr_path",
        lambda series, old, new: sonarr.append(new),
    )
    with app.app_context():
        series = make_tv_series("Tagged Show", tmdb_id=4242)
        file = make_tv_file(series, 1, 1, "WEBDL-1080p")
        file.dirname = "TV Shows/Tagged Show {tmdb-4242}/Season 01"
        file.file_path = f"{file.dirname}/{file.basename}"
        db.session.commit()
        _materialize(app, file)

        assert rename_tv_series_task(series.id, "Tagged Show (2001)") is True
        db.session.expire_all()
        file = db.session.get(File, file.id)
        assert file.dirname == "TV Shows/Tagged Show (2001) {tmdb-4242}/Season 01"
        assert os.path.isfile(os.path.join(app.config["LIBRARY_DIR"], file.file_path))
        assert sonarr == ["Tagged Show (2001) {tmdb-4242}"]


def test_rename_changes_nothing_when_a_file_does_not_match(app, monkeypatch):
    """A skipped file must not leave a new title and Sonarr path (#317)."""

    import app.series_rename as series_rename

    sonarr = []
    monkeypatch.setattr(
        series_rename,
        "_update_sonarr_path",
        lambda series, old, new: sonarr.append(new),
    )
    with app.app_context():
        series = make_tv_series("Odd Folder Show")
        good = make_tv_file(series, 1, 1, "WEBDL-1080p")
        odd = make_tv_file(series, 1, 2, "WEBDL-1080p")
        odd.dirname = "TV Shows/Elsewhere/Season 01"
        odd.file_path = f"{odd.dirname}/{odd.basename}"
        db.session.commit()
        good_path = _materialize(app, good)

        assert rename_tv_series_task(series.id, "Odd Folder Show (2002)") is False
        db.session.expire_all()
        assert db.session.get(type(series), series.id).title == "Odd Folder Show"
        assert (
            db.session.get(File, good.id).dirname
            == "TV Shows/Odd Folder Show/Season 01"
        )
        assert os.path.isfile(good_path)
        assert sonarr == []


def test_rename_gives_the_untouched_names_the_new_title(app):
    """A rename changes the untouched names and the keys that they want (#362).

    The stored key stays. It names the real archived object. The other
    parts of an untouched name stay. A series id tag gets the TMDB id."""

    from app.aws_storage import sanitize_s3_key

    old_key = "untouched/Batman - S01E01 - Fullscreen [Bluray-1080p].mkv"
    with app.app_context():
        series = make_tv_series("Batman", tmdb_id=2287)
        plain = make_tv_file(
            series,
            1,
            1,
            "Bluray-1080p",
            untouched_basename="Batman - S01E01 - Fullscreen [Bluray-1080p].mkv",
            aws_key=old_key,
        )
        tagged = make_tv_file(
            series,
            1,
            2,
            "Bluray-1080p",
            untouched_basename="Batman {tvdb-77871} - S01E02 - [Bluray-1080p].m2ts",
        )
        db.session.commit()
        _materialize(app, plain)

        assert rename_tv_series_task(series.id, "Batman (1966)") is True
        db.session.expire_all()
        plain = db.session.get(File, plain.id)
        assert plain.untouched_basename == (
            "Batman (1966) - S01E01 - Fullscreen [Bluray-1080p].mkv"
        )
        wanted = os.path.join(
            app.config["AWS_UNTOUCHED_PREFIX"],
            sanitize_s3_key(plain.untouched_basename),
        )
        assert (
            wanted == "untouched/Batman (1966) - S01E01 - Fullscreen [Bluray-1080p].mkv"
        )
        assert plain.aws_key == old_key
        assert db.session.get(File, tagged.id).untouched_basename == (
            "Batman (1966) {tmdb-2287} - S01E02 - [Bluray-1080p].m2ts"
        )


def test_rename_moves_the_trail_to_the_new_untouched_name(app):
    """The File Activity trail follows the renamed untouched name."""

    from app.pipeline import (
        index_trails,
        pipeline_trails,
        record_job_event,
        trail_entries_for,
    )

    old = "Batman - S01E03 - [Bluray-1080p].mkv"
    with app.app_context():
        series = make_tv_series("Batman", tmdb_id=2287)
        episode = make_tv_file(series, 1, 3, "Bluray-1080p", untouched_basename=old)
        db.session.commit()
        _materialize(app, episode)
        job = app.import_queue.enqueue(
            "app.videos.localization_task", args=(f"/import/{old}",)
        )
        record_job_event(app.redis, job, "started")
        # The import is done. A rename waits for an import of the series
        # that is still in a queue (#370).
        app.import_queue.remove(job)

        assert rename_tv_series_task(series.id, "Batman (1966)") is True
        db.session.expire_all()
        episode = db.session.get(File, episode.id)
        assert (
            episode.untouched_basename == "Batman (1966) - S01E03 - [Bluray-1080p].mkv"
        )
        index = index_trails(pipeline_trails(app.redis))
        entries = trail_entries_for(index, episode.basename, episode.untouched_basename)
        assert entries, "the renamed episode lost its trail"


def test_a_rename_stopped_in_the_loop_completes_on_the_retry(app, monkeypatch):
    """Rows of an earlier run carry the new title. The retry treats them as done (#374)."""

    import app.videos as videos

    from app.series_rename import fitzflix_folders

    real_rename = videos._rename_with_retries
    calls = []

    def stop_on_the_second_file(src, dst, *args, **kwargs):
        calls.append(src)
        if len(calls) == 2:
            raise OSError("the NFS share stopped")
        return real_rename(src, dst, *args, **kwargs)

    with app.app_context():
        series = make_tv_series("Batman", tmdb_id=2287)
        files = [make_tv_file(series, 1, n, "Bluray-1080p") for n in (1, 2, 3)]
        db.session.commit()
        for file in files:
            _materialize(app, file)
        series_id = series.id

        monkeypatch.setattr(videos, "_rename_with_retries", stop_on_the_second_file)
        try:
            rename_tv_series_task(series_id, "Batman (1966)")
        except OSError:
            pass
        db.session.expire_all()
        series = db.session.get(type(series), series_id)
        assert series.title == "Batman"
        assert len(fitzflix_folders(series)) == 2

        monkeypatch.setattr(videos, "_rename_with_retries", real_rename)
        assert rename_tv_series_task(series_id, "Batman (1966)") is True
        db.session.expire_all()
        series = db.session.get(type(series), series_id)
        assert series.title == "Batman (1966)"
        assert fitzflix_folders(series) == {"Batman (1966)"}
        for file in series.files:
            assert file.basename.startswith("Batman (1966) - S01E0")
            assert os.path.isfile(
                os.path.join(app.config["LIBRARY_DIR"], file.file_path)
            )


def test_the_rename_time_limit_grows_with_the_files(app, monkeypatch):
    """The job and the episode locks get 30 seconds for each file (#374)."""

    from rq.registry import ScheduledJobRegistry

    from app.series_rename import RENAME_SECONDS_PER_FILE, rename_timeout
    from app.tasks import requeue_job_id

    monkeypatch.setitem(app.config, "SQL_TASK_TIMEOUT", 600)
    with app.app_context():
        series = make_tv_series("Batman", tmdb_id=2287)
        make_tv_file(series, 1, 1, "Bluray-1080p")
        db.session.commit()
        assert rename_timeout(series) == 600
        for n in range(2, 41):
            make_tv_file(series, 1, n, "Bluray-1080p")
        db.session.commit()
        assert rename_timeout(series) == 40 * RENAME_SECONDS_PER_FILE

        # A retry of the rename keeps the long limit.
        app.import_queue.enqueue(
            "app.videos.localization_task",
            args=("/import/Batman - S01E41 - [WEBDL-1080p].mkv",),
        )
        assert rename_tv_series_task(series.id, "Batman (1966)") is False
        retry_id = requeue_job_id("app.series_rename.rename_tv_series_task", series.id)
        assert retry_id in ScheduledJobRegistry(queue=app.file_queue).get_job_ids()
        assert app.file_queue.fetch_job(retry_id).timeout == 1200


def _trail_of(app, *names):
    from app.pipeline import index_trails, pipeline_trails, trail_entries_for

    return trail_entries_for(index_trails(pipeline_trails(app.redis)), *names)


def _imported(app, source, library_name):
    """Record the trail of an import of source that the import named library_name."""

    from app.pipeline import migrate_trail, record_job_event

    job = app.import_queue.enqueue(
        "app.videos.localization_task", args=(f"/import/{source}",)
    )
    record_job_event(app.redis, job, "started")
    migrate_trail(app.redis, source, library_name)
    app.import_queue.remove(job)


def test_rename_moves_the_trail_of_a_remuxed_episode(app):
    """The import keyed the trail under the .mkv name. The untouched name is .mp4 (#375)."""

    mp4 = "Show - S01E01 - [WEBDL-1080p].mp4"
    with app.app_context():
        series = make_tv_series("Show", tmdb_id=4242)
        episode = make_tv_file(series, 1, 1, "WEBDL-1080p", untouched_basename=mp4)
        db.session.commit()
        _materialize(app, episode)
        _imported(app, mp4, episode.basename)
        assert _trail_of(app, episode.basename, episode.untouched_basename)

        assert rename_tv_series_task(series.id, "Show (2005)") is True
        db.session.expire_all()
        episode = db.session.get(File, episode.id)
        assert episode.basename == "Show (2005) - S01E01 - [WEBDL-1080p].mkv"
        assert episode.untouched_basename == "Show (2005) - S01E01 - [WEBDL-1080p].mp4"
        assert _trail_of(app, episode.basename, episode.untouched_basename)


def test_rename_back_to_the_import_name_keeps_the_trail(app):
    """The import made "Batman (1966)" canonical as "Batman". A rename goes back (#375)."""

    from app.pipeline import ALIAS_KEY, _digest

    source = "Batman (1966) - S01E01 - [Bluray-1080p].mkv"
    with app.app_context():
        series = make_tv_series("Batman", tmdb_id=2287)
        episode = make_tv_file(series, 1, 1, "Bluray-1080p", untouched_basename=source)
        db.session.commit()
        _materialize(app, episode)
        _imported(app, source, episode.basename)

        assert rename_tv_series_task(series.id, "Batman (1966)") is True
        db.session.expire_all()
        episode = db.session.get(File, episode.id)
        assert episode.basename == source
        assert _trail_of(app, episode.basename, episode.untouched_basename)
        # The new home of the trail has no redirect away from itself.
        assert not app.redis.exists(ALIAS_KEY.format(digest=_digest(source)))


def test_the_episode_locks_last_as_long_as_the_job(app, monkeypatch):
    """A file left the series after the enqueue. The TTL follows the job (#374 follow-up)."""

    import rq

    from app import series_rename

    class RunningJob:
        timeout = 5400

    taken = []
    real_lock = app.lock_manager.lock

    def recording_lock(key, ttl):
        taken.append(ttl)
        return real_lock(key, ttl)

    monkeypatch.setattr(rq, "get_current_job", lambda: RunningJob())
    monkeypatch.setattr(app.lock_manager, "lock", recording_lock)
    monkeypatch.setitem(app.config, "SQL_TASK_TIMEOUT", 600)
    with app.app_context():
        series = make_tv_series("Batman", tmdb_id=2287)
        make_tv_file(series, 1, 1, "Bluray-1080p")
        db.session.commit()
        locks = series_rename._take_series_locks(series, "Batman (1966)")
        for lock in locks:
            app.lock_manager.unlock(lock)
    assert taken == [5400 * 1000]
