"""Test the Sonarr path check, and the rename of a series that Sonarr moved.

A person renames a series in Fitzflix. The nightly check compares the
folder of each Sonarr series with the Fitzflix folder of the series. It
repoints Sonarr when only the Fitzflix folder exists. Each other
difference is a conflict: the check changes nothing, marks the series,
and mails the admin. The admin then gives the series the Sonarr title
with the rename task, which accepts the files that Sonarr moved."""

import base64
import os

from datetime import datetime

import pytest

from tests.conftest import ADMIN_API_KEY, ADMIN_EMAIL
from tests.factories import make_tv_file, make_tv_series
from tests.fakes import Sonarr

TVDB = 77871


@pytest.fixture
def sonarr(app, monkeypatch):
    """Install a fake Sonarr. Return a function that sets its series list."""

    import app.arr.sonarr_push as sonarr_push

    holder = {}

    def install(*series, down=False):
        holder["fake"] = Sonarr(series, down=down)
        monkeypatch.setattr(sonarr_push, "_sonarr", holder["fake"])
        return holder["fake"]

    install()
    return install


def sonarr_series(folder, series_id=5, tvdb_id=TVDB, root=None, **extra):
    """Return a Sonarr series dict at a folder of the Sonarr root.

    The root is the TV library unless the test gives one."""

    from app import get_app

    root = root or get_app().config["TV_LIBRARY"]
    return {
        "id": series_id,
        "tvdbId": tvdb_id,
        "title": folder,
        "path": os.path.join(root, folder),
        **extra,
    }


def place(app, relative):
    """Put a file at a path under the library directory. Return the path."""

    path = os.path.join(app.config["LIBRARY_DIR"], relative)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        handle.write("video")
    return path


def tv_folder(app, name):
    """Make an empty show folder in the TV library."""

    os.makedirs(os.path.join(app.config["TV_LIBRARY"], name), exist_ok=True)


def batman(app, on_disk=True, **kwargs):
    """Make the series Batman with one episode. Return the series id and file id."""

    from app import db

    series = make_tv_series("Batman", tvdb_id=TVDB, tmdb_id=2287, **kwargs)
    file = make_tv_file(series, 1, 1, "Bluray-1080p")
    file.untouched_basename = file.basename
    db.session.commit()
    if on_disk:
        place(app, file.file_path)
    return series.id, file.id


def rename_jobs(app):
    return [
        job
        for job in app.file_queue.jobs
        if job.func_name == "app.media.series_rename.rename_tv_series_task"
    ]


NEW_FOLDER = "TV Shows/Batman (1966)/Season 01"
MOVED = f"{NEW_FOLDER}/Batman (1966) - S01E01 - [Bluray-1080p].mkv"


# The nightly check, branch by branch.


def test_equal_folders_change_nothing(app, sonarr):
    from app.media.series_rename import reconcile_sonarr_paths

    with app.app_context():
        batman(app)
    fake = sonarr(sonarr_series("Batman"))

    assert reconcile_sonarr_paths() == {"checked": 1, "repointed": 0, "conflicts": 0}
    assert fake.puts == [] and fake.commands == []
    assert rename_jobs(app) == []


def test_a_folder_that_sonarr_moved_is_a_conflict_with_one_mail(
    app, sonarr, caplog, monkeypatch
):
    """Sonarr moved the folder. Fitzflix does not follow.

    The check changes nothing. It marks the series, and the admin gets
    1 mail while the conflict lasts, not 1 for each night."""

    from app.media.series_rename import SONARR_CONFLICT_KEY, reconcile_sonarr_paths

    sent = admin_mails(monkeypatch)
    with app.app_context():
        series_id, _file_id = batman(app, on_disk=False)
    place(app, MOVED)
    fake = sonarr(sonarr_series("Batman (1966)"))

    for _night in range(2):
        summary = reconcile_sonarr_paths()
        assert (summary["conflicts"], summary["repointed"]) == (1, 0)
    assert rename_jobs(app) == [] and fake.puts == []
    assert "'Batman' is at 'Batman (1966)' in Sonarr and at 'Batman' in Fitzflix" in (
        caplog.text
    )
    marker = SONARR_CONFLICT_KEY.format(series_id=series_id)
    assert 86400 < app.redis.ttl(marker) <= 2 * 86400
    ((subject, text),) = sent
    assert subject == "Sonarr folder needs triage"
    assert "Only the Sonarr folder is on the disk" in text
    assert "Rename Series form" in text


def test_a_series_with_a_rename_on_the_queue_gets_no_mail(app, sonarr, monkeypatch):
    """A rename that waits can have its files in 2 folders. That is no conflict."""

    from app.media.series_rename import SONARR_CONFLICT_KEY, reconcile_sonarr_paths
    from app.tasks import enqueue_task

    sent = admin_mails(monkeypatch)
    with app.app_context():
        series_id, _file_id = batman(app, on_disk=False)
        enqueue_task(
            "app.media.series_rename.rename_tv_series_task", series_id, "Batman (1966)"
        )
    place(app, MOVED)
    sonarr(sonarr_series("Batman (1966)"))

    summary = reconcile_sonarr_paths()
    assert (summary["checked"], summary["conflicts"]) == (1, 0)
    assert sent == []
    assert app.redis.exists(SONARR_CONFLICT_KEY.format(series_id=series_id))


def test_a_lost_push_repoints_sonarr(app, sonarr):
    """Only the Fitzflix folder exists. Sonarr goes back to it (#266 case)."""

    from app.media.series_rename import reconcile_sonarr_paths

    with app.app_context():
        batman(app)
    fake = sonarr(sonarr_series("Batman (Old)"))

    assert reconcile_sonarr_paths()["repointed"] == 1
    (put,) = fake.puts
    assert put["path"] == os.path.join(app.config["TV_LIBRARY"], "Batman")
    assert fake.commands == [{"name": "RescanSeries", "seriesId": 5}]
    assert rename_jobs(app) == []


@pytest.mark.parametrize("both", [True, False], ids=["both exist", "neither exists"])
def test_two_or_no_folders_change_nothing(app, sonarr, caplog, both):
    from app.media.series_rename import reconcile_sonarr_paths

    with app.app_context():
        batman(app, on_disk=both)
    if both:
        tv_folder(app, "Batman (1966)")
    fake = sonarr(sonarr_series("Batman (1966)"))

    summary = reconcile_sonarr_paths()
    assert (summary["conflicts"], summary["repointed"]) == (1, 0)
    assert fake.puts == [] and rename_jobs(app) == []
    state = "The 2 folders are" if both else "No folder of the 2 is"
    assert f"{state} on the disk. Not changed" in caplog.text


def test_a_series_that_sonarr_does_not_manage_is_untouched(app, sonarr):
    from app.media.series_rename import reconcile_sonarr_paths

    with app.app_context():
        batman(app)
    fake = sonarr(sonarr_series("Other Show", tvdb_id=1))

    assert reconcile_sonarr_paths()["checked"] == 0
    assert fake.puts == [] and rename_jobs(app) == []


def test_a_series_without_files_is_not_checked(app, sonarr):
    from app import db
    from app.media.series_rename import reconcile_sonarr_paths

    with app.app_context():
        make_tv_series("Batman", tvdb_id=TVDB)
        db.session.commit()
    fake = sonarr(sonarr_series("Batman (1966)"))

    assert reconcile_sonarr_paths()["checked"] == 0
    assert fake.puts == [] and rename_jobs(app) == []


def test_the_tmdb_id_matches_a_series_without_a_tvdb_id(app, sonarr):
    from app import db
    from app.media.series_rename import SONARR_CONFLICT_KEY, reconcile_sonarr_paths

    with app.app_context():
        series = make_tv_series("Batman", tmdb_id=2287)
        make_tv_file(series, 1, 1, "Bluray-1080p")
        db.session.commit()
        series_id = series.id
    place(app, MOVED)
    sonarr(sonarr_series("Batman (1966)", tvdb_id=999, tmdbId=2287))

    summary = reconcile_sonarr_paths()
    assert (summary["checked"], summary["conflicts"]) == (1, 1)
    assert app.redis.exists(SONARR_CONFLICT_KEY.format(series_id=series_id))


def test_the_check_changes_nothing_over_the_limit(app, sonarr, caplog, monkeypatch):
    import app.media.series_rename as series_rename

    monkeypatch.setattr(series_rename, "MAX_SONARR_FIXES", 0)
    with app.app_context():
        batman(app)
    fake = sonarr(sonarr_series("Batman (Old)"))

    summary = series_rename.reconcile_sonarr_paths()
    assert summary["over_limit"] == 1 and summary["repointed"] == 0
    assert fake.puts == []
    assert "more than 0. Nothing changed" in caplog.text


def test_the_check_survives_a_sonarr_outage(app, sonarr):
    from app.media.series_rename import reconcile_sonarr_paths

    sonarr(down=True)
    assert reconcile_sonarr_paths()["error"] == "Sonarr unreachable"


def test_the_check_changes_nothing_without_the_tv_library(
    app, sonarr, caplog, monkeypatch, tmp_path
):
    """A dropped mount hides both folders. One warning, no 'neither' per series."""

    from app.media.series_rename import reconcile_sonarr_paths

    with app.app_context():
        batman(app, on_disk=False)
    monkeypatch.setitem(app.config, "TV_LIBRARY", str(tmp_path / "unmounted"))
    fake = sonarr(sonarr_series("Batman (1966)"))

    assert reconcile_sonarr_paths()["error"] == "TV library not mounted"
    assert fake.gets == []
    assert "on the disk" not in caplog.text


def test_the_rename_task_completes_a_series_that_sonarr_moved(app, sonarr):
    """The admin follows a Sonarr rename: the files are at the new paths already.

    The task completes the rows, rewrites the untouched names, and puts
    the same path back to Sonarr. The archives keep their keys (#668)."""

    from app import db
    from app.models import File, TVSeries
    from app.media.series_rename import reconcile_sonarr_paths, rename_tv_series_task

    with app.app_context():
        series = make_tv_series("Batman", tvdb_id=TVDB, tmdb_id=2287)
        files = [make_tv_file(series, 1, n, "Bluray-1080p") for n in (1, 2)]
        for file in files:
            file.untouched_basename = file.basename
            file.aws_key = f"untouched/{file.basename}"
            file.aws_date_uploaded = datetime(2026, 1, 1)
        db.session.commit()
        ids = [file.id for file in files]
        old_keys = {file.id: file.aws_key for file in files}
        series_id = series.id
    for n in (1, 2):
        place(
            app,
            f"TV Shows/Batman (1966)/Season 01/"
            f"Batman (1966) - S01E0{n} - [Bluray-1080p].mkv",
        )
    fake = sonarr(sonarr_series("Batman (1966)"))

    assert reconcile_sonarr_paths()["conflicts"] == 1
    assert rename_tv_series_task(series_id, "Batman (1966)") is True

    moves = [
        job.args[0]
        for job in app.file_queue.jobs
        if job.func_name == "app.archive.moves.move_archive_object"
    ]
    assert moves == []
    with app.app_context():
        assert db.session.get(TVSeries, series_id).title == "Batman (1966)"
        for file_id in ids:
            row = db.session.get(File, file_id)
            assert row.dirname == "TV Shows/Batman (1966)/Season 01"
            assert row.basename.startswith("Batman (1966) - S01E0")
            assert row.untouched_basename == row.basename
            assert row.aws_key == old_keys[file_id]
            assert row.aws_key_target is None
            assert os.path.isfile(
                os.path.join(app.config["LIBRARY_DIR"], row.file_path)
            )
    assert [put["path"] for put in fake.puts] == [
        os.path.join(app.config["TV_LIBRARY"], "Batman (1966)")
    ]

    # The next check finds the folders equal.
    assert reconcile_sonarr_paths()["conflicts"] == 0


def two_batman_episodes(app):
    """Make Batman with E01 and E02 in the old folder. Return the series and file ids."""

    from app import db

    with app.app_context():
        series = make_tv_series("Batman", tvdb_id=TVDB, tmdb_id=2287)
        files = [make_tv_file(series, 1, n, "Bluray-1080p") for n in (1, 2)]
        for file in files:
            file.untouched_basename = file.basename
        db.session.commit()
        return series.id, [file.id for file in files]


SECOND = "TV Shows/Batman (1966)/Season 01/Batman (1966) - S01E02 - [Bluray-1080p].mkv"


def test_a_file_under_its_old_name_in_the_sonarr_folder_gets_the_new_name(app, sonarr):
    """Sonarr moved the folder and did not rename an untracked file (#758).

    The file keeps its old name in the new folder. The task renames it to
    the planned name, with its thumb. Both records name their files."""

    from app import db
    from app.models import File, TVSeries
    from app.media.series_rename import rename_tv_series_task

    series_id, ids = two_batman_episodes(app)
    place(app, MOVED)
    old_name = place(app, f"{NEW_FOLDER}/Batman - S01E02 - [Bluray-1080p].mkv")
    old_thumb = place(app, f"{NEW_FOLDER}/Batman - S01E02 - [Bluray-1080p].jpg")
    sonarr(sonarr_series("Batman (1966)"))

    assert rename_tv_series_task(series_id, "Batman (1966)") is True

    library = app.config["LIBRARY_DIR"]
    with app.app_context():
        assert db.session.get(TVSeries, series_id).title == "Batman (1966)"
        paths = [db.session.get(File, file_id).file_path for file_id in ids]
    assert paths == [MOVED, SECOND]
    assert all(os.path.isfile(os.path.join(library, path)) for path in paths)
    assert not os.path.exists(old_name) and not os.path.exists(old_thumb)
    assert os.path.isfile(os.path.join(library, SECOND[: -len(".mkv")] + ".jpg"))


def admin_mails(monkeypatch):
    """Record the subject and the text of each admin mail."""

    sent = []
    monkeypatch.setattr(
        "app.email.notify_admin_text",
        lambda subject, text, html: sent.append((subject, text)),
    )
    return sent


def test_a_file_that_no_record_accounts_for_stops_the_rename(
    app, sonarr, caplog, monkeypatch
):
    """Sonarr gave a file an other quality name (#758).

    The file is at neither name of its record. The task refuses the
    rename with 1 warning, and no record changes. A video in a hidden
    directory does not count. The admin gets 1 mail for the life of the
    refusal, not 1 for each try."""

    from app import db
    from app.models import File, TVSeries
    from app.media.series_rename import rename_tv_series_task

    sent = admin_mails(monkeypatch)
    series_id, ids = two_batman_episodes(app)
    with app.app_context():
        before = [db.session.get(File, file_id).file_path for file_id in ids]
    place(app, MOVED)
    other = f"{NEW_FOLDER}/Batman (1966) - S01E02 - [HDTV-1080p].mkv"
    place(app, other)
    place(app, f"{NEW_FOLDER}/Batman (1966) - S01E02 - [HDTV-1080p].en.srt")
    place(app, f"{NEW_FOLDER}/.Batman - S01E03 - [Bluray-1080p].mkv")
    place(app, "TV Shows/Batman (1966)/@eaDir/Batman - S01E04 - [DVD].mkv")
    place(app, "TV Shows/Batman (1966)/.grab/Batman - S01E05 - [DVD].mkv")
    place(app, "TV Shows/Batman (1966)/poster.png")
    sonarr(sonarr_series("Batman (1966)"))

    for _try in range(2):
        caplog.clear()
        assert rename_tv_series_task(series_id, "Batman (1966)") is False
        assert caplog.text.count("belong to no record of the series") == 1
        assert f"so nothing changed: [{other!r}]" in caplog.text
    ((subject, text),) = sent
    assert subject == "Series rename needs triage"
    assert "'Batman'" in text and "'Batman (1966)'" in text
    assert "1 video file(s)" in text and other in text
    assert "with the form of its page" in text
    with app.app_context():
        assert db.session.get(TVSeries, series_id).title == "Batman"
        assert [db.session.get(File, file_id).file_path for file_id in ids] == before


def test_a_stray_file_in_an_other_season_folder_stops_the_rename(app, sonarr, caplog):
    """The count reads each season folder of the show, not only the planned ones (#758)."""

    from app.media.series_rename import rename_tv_series_task

    series_id, _ids = two_batman_episodes(app)
    place(app, MOVED)
    place(app, SECOND)
    stray = "TV Shows/Batman (1966)/Season 02/Batman (1966) - S02E01 - [HDTV-1080p].mkv"
    place(app, stray)
    sonarr(sonarr_series("Batman (1966)"))

    assert rename_tv_series_task(series_id, "Batman (1966)") is False
    assert f"so nothing changed: [{stray!r}]" in caplog.text


def test_the_mail_of_a_stopped_rename_quotes_the_title_for_the_shell(app, monkeypatch):
    """A title with an apostrophe must give a command that the shell reads (#758)."""

    import shlex

    from app.media.series_rename import _tell_admin_of_stray_files
    from tests.factories import make_tv_series

    sent = admin_mails(monkeypatch)
    with app.app_context():
        series = make_tv_series("Bob's Burgers")
        _tell_admin_of_stray_files(
            series, "Bob's Burgers (2011)", {"TV Shows/x.mkv"}, resumed=True
        )
    ((_subject, text),) = sent
    command = text[text.index("flask tv rename") :].rstrip(".")
    assert shlex.split(command) == [
        "flask",
        "tv",
        "rename",
        str(series.id),
        "Bob's Burgers (2011)",
    ]
    assert "Some or all records have the new folder" in text


def rename_retries(app):
    """Return the booked retries of the series rename."""

    from tests.test_scheduling import scheduled_jobs

    return [
        job
        for job in scheduled_jobs(app.file_queue)
        if job.func_name == "app.media.series_rename.rename_tv_series_task"
    ]


def test_a_resumed_rename_that_meets_a_stray_file_books_a_retry(
    app, sonarr, caplog, monkeypatch
):
    """Run 1 renames each row and waits for an import (review of #758).

    The import fails, and its file stays in the Sonarr folder with no
    record. Run 2 must not stop for good, because the rows carry the new
    folder already. The retries have a limit, and the last one tells
    the admin."""

    from app.media.importing import chain
    from app import db
    from app.models import File, TVSeries
    from app.media.series_rename import MAX_RENAME_STRAY_WAITS, rename_tv_series_task

    sent = admin_mails(monkeypatch)
    series_id, ids = two_batman_episodes(app)
    place(app, MOVED)
    place(app, SECOND)
    sonarr(sonarr_series("Batman (1966)"))

    # The import is in flight only at the end of the loop of run 1.
    real_flight = chain.series_import_in_flight
    calls = []
    new = f"{NEW_FOLDER}/Batman (1966) - S01E03 - [WEBDL-1080p].mkv"

    def flight(titles):
        calls.append(titles)
        return new if len(calls) == 2 else real_flight(titles)

    monkeypatch.setattr(chain, "series_import_in_flight", flight)
    assert rename_tv_series_task(series_id, "Batman (1966)") is False
    monkeypatch.setattr(chain, "series_import_in_flight", real_flight)
    with app.app_context():
        assert db.session.get(TVSeries, series_id).title == "Batman"
        assert [db.session.get(File, i).file_path for i in ids] == [MOVED, SECOND]
    for retry in rename_retries(app):
        retry.delete()

    # A rename that waits marks the series. The records can name a folder
    # that Sonarr moved away, and the marker holds the restores of the
    # weekly sync in that time.
    from app.media.series_rename import SONARR_CONFLICT_KEY

    marker = SONARR_CONFLICT_KEY.format(series_id=series_id)
    assert app.redis.exists(marker)
    app.redis.delete(marker)

    place(app, new)
    caplog.clear()
    assert rename_tv_series_task(series_id, "Batman (1966)") is False
    assert "nothing changed" not in caplog.text
    assert "The rows that are renamed already stay renamed" in caplog.text
    (retry,) = rename_retries(app)
    assert retry.kwargs == {"stray_waits": 1}
    assert app.redis.exists(marker)
    retry.delete()
    assert sent == []

    # The last retry stops, and the admin gets a mail.
    caplog.clear()
    assert (
        rename_tv_series_task(
            series_id, "Batman (1966)", stray_waits=MAX_RENAME_STRAY_WAITS
        )
        is False
    )
    assert rename_retries(app) == []
    assert "It stops" in caplog.text
    ((subject, text),) = sent
    assert subject == "Series rename needs triage"
    assert new in text and f"flask tv rename {series_id}" in text


def test_a_fault_of_the_walk_books_the_volume_wait(app, sonarr, monkeypatch):
    """os.walk reports a read fault through onerror. The task waits for the volume (#758)."""

    from app import db
    from app.models import File
    from app.media.series_rename import rename_tv_series_task

    series_id, ids = two_batman_episodes(app)
    with app.app_context():
        before = [db.session.get(File, file_id).file_path for file_id in ids]
    place(app, MOVED)
    place(app, SECOND)
    show = os.path.join(app.config["TV_LIBRARY"], "Batman (1966)")
    real_walk = os.walk

    def walk(top, onerror=None, **kwargs):
        if top == show:
            onerror(PermissionError(13, "Permission denied", top))
            return iter(())
        return real_walk(top, onerror=onerror, **kwargs)

    monkeypatch.setattr("app.media.series_rename.os.walk", walk)
    assert rename_tv_series_task(series_id, "Batman (1966)") is False
    (retry,) = rename_retries(app)
    assert retry.kwargs == {"volume_waits": 1}
    with app.app_context():
        assert [db.session.get(File, file_id).file_path for file_id in ids] == before


def test_a_copy_under_the_old_name_is_not_claimed_while_the_old_path_holds_the_file(
    app, sonarr, caplog, monkeypatch
):
    """A record claims its old name in the new folder only when its old path is empty.

    Else a copy made by hand passes the count and stays with no record
    (review of #758)."""

    from app import db
    from app.models import File, TVSeries
    from app.media.series_rename import rename_tv_series_task

    sent = admin_mails(monkeypatch)
    with app.app_context():
        series_id, file_id = batman(app)
        before = db.session.get(File, file_id).file_path
    copy = f"{NEW_FOLDER}/Batman - S01E01 - [Bluray-1080p].mkv"
    place(app, copy)

    assert rename_tv_series_task(series_id, "Batman (1966)") is False
    assert f"belong to no record of the series, so nothing changed: [{copy!r}]" in (
        caplog.text
    )
    with app.app_context():
        assert db.session.get(TVSeries, series_id).title == "Batman"
        assert db.session.get(File, file_id).file_path == before
    assert [subject for subject, _text in sent] == ["Series rename needs triage"]


def test_a_file_with_an_other_size_than_the_record_is_not_claimed(app, sonarr, caplog):
    """Sonarr gave the library file the planned name of an S3-only sibling (review of #758).

    The sibling is an other quality of the same episode. Its size is not
    the size of the file. Thus, the file stays unclaimed, and the task
    refuses the rename. A record with the size of its file claims it."""

    from app import db
    from app.media.series_rename import rename_tv_series_task

    with app.app_context():
        series = make_tv_series("Batman", tvdb_id=TVDB, tmdb_id=2287)
        files = [
            make_tv_file(series, 1, 1, "Bluray-1080p", filesize_bytes=5),
            make_tv_file(series, 1, 1, "DVD", filesize_bytes=999),
            make_tv_file(series, 1, 2, "Bluray-1080p", filesize_bytes=5),
        ]
        for file in files:
            file.untouched_basename = file.basename
        db.session.commit()
        series_id = series.id
    renamed = f"{NEW_FOLDER}/Batman (1966) - S01E01 - [DVD].mkv"
    place(app, renamed)
    place(app, SECOND)
    sonarr(sonarr_series("Batman (1966)"))

    assert rename_tv_series_task(series_id, "Batman (1966)") is False
    assert f"so nothing changed: [{renamed!r}]" in caplog.text


def test_a_record_with_no_library_file_still_gets_the_new_name(app, sonarr):
    """Each file in the Sonarr folder belongs to a record (#758).

    Then a record with no file anywhere is an archived-only record, and
    it gets the new name."""

    from app import db
    from app.models import File
    from app.media.series_rename import rename_tv_series_task

    series_id, ids = two_batman_episodes(app)
    place(app, MOVED)
    sonarr(sonarr_series("Batman (1966)"))

    assert rename_tv_series_task(series_id, "Batman (1966)") is True
    with app.app_context():
        assert [db.session.get(File, file_id).file_path for file_id in ids] == [
            MOVED,
            SECOND,
        ]
    assert not os.path.exists(os.path.join(app.config["LIBRARY_DIR"], SECOND))


# The webhook starts no check.


def auth_header():
    token = base64.b64encode(f"{ADMIN_EMAIL}:{ADMIN_API_KEY}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def event(app, event_type, folder, **extra):
    """Return a Sonarr event payload for the series Batman at a folder."""

    return {
        "eventType": event_type,
        "series": {
            "id": 5,
            "tvdbId": TVDB,
            "title": "Batman",
            "path": os.path.join(app.config["TV_LIBRARY"], folder),
        },
        **extra,
    }


@pytest.mark.parametrize("event_type", ["Rename", "SeriesAdd", "Grab"])
def test_an_event_with_a_different_folder_queues_nothing(app, client, event_type):
    """Only the nightly check compares the folders. No event starts a check."""

    with app.app_context():
        batman(app)

    response = client.post(
        "/api/sonarr/add",
        json=event(app, event_type, "Batman (1966)"),
        headers=auth_header(),
    )
    assert response.status_code == 202
    assert app.maintenance_queue.count == 0 and rename_jobs(app) == []


# The follow-up of the review (#370).


def localizing(app, basename):
    """Queue a localization of an episode name. No worker runs it."""

    return app.import_queue.enqueue(
        "app.media.importing.localization.localization_task",
        args=(f"/import/{basename}",),
    )


def scheduled_rename_retry(app, series_id):
    from rq.registry import ScheduledJobRegistry

    from app.tasks import requeue_job_id

    retry = requeue_job_id("app.media.series_rename.rename_tv_series_task", series_id)
    return retry in ScheduledJobRegistry(queue=app.file_queue).get_job_ids()


@pytest.mark.parametrize("name", ["Batman", "Batman (1966)"])
def test_a_rename_waits_for_an_import_of_the_series(app, name):
    """An import attaches by title at its end. The rename must wait for it."""

    from app import db
    from app.models import File, TVSeries
    from app.media.series_rename import rename_tv_series_task

    with app.app_context():
        series_id, file_id = batman(app)
        old = db.session.get(File, file_id).file_path
    localizing(app, f"{name} - S01E03 - [WEBDL-1080p].mkv")

    assert rename_tv_series_task(series_id, "Batman (1966)") is False
    assert scheduled_rename_retry(app, series_id)
    with app.app_context():
        assert db.session.get(TVSeries, series_id).title == "Batman"
        assert db.session.get(File, file_id).file_path == old


def test_a_rename_waits_for_a_held_episode_lock(app, held_lock):
    from app import db
    from app.models import File, TVSeries
    from app.media.series_rename import rename_tv_series_task

    with app.app_context():
        series_id, file_id = batman(app)
        identifier = db.session.get(File, file_id).file_identifier()
    held_lock(identifier)

    assert rename_tv_series_task(series_id, "Batman (1966)") is False
    assert scheduled_rename_retry(app, series_id)
    with app.app_context():
        assert db.session.get(TVSeries, series_id).title == "Batman"


def test_an_import_after_a_rename_attaches_to_the_renamed_series(app):
    """The parse saw "Batman". The series is "Batman (1966)" at the attach."""

    from app import db
    from app.media.importing.cataloging import _attach_tv_series
    from app.models import File, TVSeries

    with app.app_context():
        renamed = make_tv_series("Batman (1966)", tvdb_id=TVDB, tmdb_id=2287)
        tagged = make_tv_series("The Office (2005)", tmdb_id=2316)
        db.session.commit()
        with db.session.no_autoflush:
            for details, wanted in (
                ({"title": "Batman"}, renamed),
                ({"title": "The Office", "tmdb_id": 2316}, tagged),
            ):
                file = File()
                _attach_tv_series(file, details)
                assert file.tv_series is wanted
            assert TVSeries.query.count() == 2
        db.session.rollback()


def test_a_series_on_another_sonarr_root_is_a_conflict(
    app, sonarr, caplog, monkeypatch, tmp_path
):
    """A repoint keeps the Sonarr parent. On another root, it names a missing folder."""

    from app.media.series_rename import reconcile_sonarr_paths

    other = tmp_path / "Other TV"
    (other / "Batman (1966)").mkdir(parents=True)
    monkeypatch.setitem(
        app.config, "SONARR_ROOT_FOLDERS", [app.config["TV_LIBRARY"], str(other)]
    )
    with app.app_context():
        batman(app)
    fake = sonarr(sonarr_series("Batman (1966)", root=str(other)))

    assert reconcile_sonarr_paths()["conflicts"] == 1
    assert fake.puts == [] and rename_jobs(app) == []
    assert "not under the TV library. Not changed" in caplog.text


def test_a_series_matched_by_tmdb_takes_the_tvdb_id_and_is_repointed(app, sonarr):
    from app import db
    from app.models import TVSeries
    from app.media.series_rename import reconcile_sonarr_paths

    with app.app_context():
        series = make_tv_series("Batman", tmdb_id=2287)
        file = make_tv_file(series, 1, 1, "Bluray-1080p")
        db.session.commit()
        place(app, file.file_path)
        series_id = series.id
    fake = sonarr(sonarr_series("Batman (Old)", tvdb_id=999, tmdbId=2287))

    assert reconcile_sonarr_paths()["repointed"] == 1
    assert [put["path"] for put in fake.puts] == [
        os.path.join(app.config["TV_LIBRARY"], "Batman")
    ]
    with app.app_context():
        assert db.session.get(TVSeries, series_id).tvdb_id == 999

    # The next night finds the folders equal.
    assert reconcile_sonarr_paths()["repointed"] == 0


# The series split of a Sonarr year (#372) and the adopt stage (#373).


def test_an_episode_attached_before_the_rename_joins_the_renamed_series(
    app, sonarr, caplog
):
    """The old folder is gone, and 1 row is in the Sonarr folder already.

    The nightly check reports the 2 folders. The rename of the admin
    counts that row as done (#372, #374)."""

    from app import db
    from app.models import TVSeries
    from app.media.series_rename import (
        fitzflix_folders,
        reconcile_sonarr_paths,
        rename_tv_series_task,
    )
    from tests.factories import make_file

    with app.app_context():
        series_id, file_id = batman(app, on_disk=False)
        series = db.session.get(TVSeries, series_id)
        make_file(
            "Batman (1966) - S01E02 - [Bluray-1080p].mkv",
            NEW_FOLDER,
            "Batman (1966) - S01E02",
            "TV Shows",
            "Bluray-1080p",
            series_id=series_id,
            season=1,
            episode=2,
            last_episode=2,
        )
        db.session.commit()
    place(app, MOVED)
    place(app, f"{NEW_FOLDER}/Batman (1966) - S01E02 - [Bluray-1080p].mkv")
    sonarr(sonarr_series("Batman (1966)"))

    assert reconcile_sonarr_paths()["conflicts"] == 1
    assert "are in more than one show folder" in caplog.text
    assert rename_tv_series_task(series_id, "Batman (1966)") is True
    with app.app_context():
        series = db.session.get(TVSeries, series_id)
        assert series.title == "Batman (1966)"
        assert fitzflix_folders(series) == {"Batman (1966)"}
    assert reconcile_sonarr_paths()["conflicts"] == 0


def test_a_rename_waits_for_a_sonarr_adopt(app):
    """The adopt task queues the localization after the Sonarr rename (#373)."""

    from app import db
    from app.models import TVSeries
    from app.media.series_rename import rename_tv_series_task

    with app.app_context():
        series_id, _file_id = batman(app)
    download = "/Volumes/TV Shows/Batman/Season 01/Batman - S01E03 - [Bluray-1080p].mkv"
    app.request_queue.enqueue(
        "app.arr.sonarr_push.adopt_episode_task",
        args=(5, 9, download, download.replace("Bluray", "WEBDL"), "WEBDL-1080p"),
    )

    assert rename_tv_series_task(series_id, "Batman (1966)") is False
    assert scheduled_rename_retry(app, series_id)
    with app.app_context():
        assert db.session.get(TVSeries, series_id).title == "Batman"


def test_an_import_that_starts_in_the_loop_holds_the_title(app, monkeypatch):
    """The rename checks again before the title changes (#373)."""

    import app.media.videos as videos

    from app import db
    from app.models import TVSeries
    from app.media.series_rename import rename_tv_series_task

    real_rename = videos._rename_with_retries
    queued = []

    def import_arrives(src, dst, *args, **kwargs):
        if not queued:
            queued.append(localizing(app, "Batman - S01E04 - [WEBDL-1080p].mkv"))
        return real_rename(src, dst, *args, **kwargs)

    monkeypatch.setattr(videos, "_rename_with_retries", import_arrives)
    with app.app_context():
        series_id, _file_id = batman(app)

    assert rename_tv_series_task(series_id, "Batman (1966)") is False
    assert scheduled_rename_retry(app, series_id)
    with app.app_context():
        assert db.session.get(TVSeries, series_id).title == "Batman"

    app.import_queue.remove(queued[0])
    assert rename_tv_series_task(series_id, "Batman (1966)") is True
    with app.app_context():
        assert db.session.get(TVSeries, series_id).title == "Batman (1966)"
