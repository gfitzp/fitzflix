"""Test the series rename.

The tests cover the disk moves, the row rewrites, the resume semantics,
and the refusal guards. The S3 keys must never change."""

import os

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

        assert rename_tv_series_task(series.id, "Batman (1966)") is True
        db.session.expire_all()
        episode = db.session.get(File, episode.id)
        assert (
            episode.untouched_basename == "Batman (1966) - S01E03 - [Bluray-1080p].mkv"
        )
        index = index_trails(pipeline_trails(app.redis))
        entries = trail_entries_for(index, episode.basename, episode.untouched_basename)
        assert entries, "the renamed episode lost its trail"
