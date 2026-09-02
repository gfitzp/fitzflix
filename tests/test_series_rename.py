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
        one.aws_untouched_key = "untouched/Batman - S01E01 - [Bluray-1080p].mkv"
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
        assert one.aws_untouched_key == (
            "untouched/Batman - S01E01 - [Bluray-1080p].mkv"
        )

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
