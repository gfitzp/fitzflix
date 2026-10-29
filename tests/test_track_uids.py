"""Test the track edits that address a track by its Matroska UID (#611).

A remux keeps the UID of a track and changes its position. The page
posts UIDs. The edit maps them to the tracks of a new read of the file
under the lock. A UID that the file does not have stops the edit and
queues a scan. The backfill fills the UIDs of the rows from before the
column. These tests use real Matroska files and read the results with
mkvmerge -J."""

import importlib.util
import inspect
import json
import os
import shutil
import subprocess

import pytest

from tests.media import make_sample_mkv


@pytest.fixture
def two_by_two_mkv(app, tmp_path):
    """Build a Matroska file with 2 audio tracks and 2 subtitle tracks."""

    subtitles = []
    for number in (1, 2):
        path = tmp_path / f"cue{number}.srt"
        path.write_text(f"1\n00:00:00,000 --> 00:00:00,500\nLine {number}\n")
        subtitles.append(path)
    return make_sample_mkv(
        str(tmp_path / "two-by-two.mkv"),
        audio=(("eng", True), ("fra", False)),
        subtitles=subtitles,
    )


def _identify(app, path):
    """Return the tracks of mkvmerge -J for a file."""

    result = subprocess.run(
        [app.config["MKVMERGE_BIN"], "-J", path],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)["tracks"]


def _by_uid(app, path):
    """Return the mkvmerge -J track properties of a file by track UID (text)."""

    return {
        str(track["properties"]["uid"]): dict(
            track["properties"], type=track["type"], id=track["id"]
        )
        for track in _identify(app, path)
    }


def _uids(app, path, kind):
    """Return the UIDs of the audio or subtitles tracks of a file, in file order."""

    return [
        str(track["properties"]["uid"])
        for track in _identify(app, path)
        if track["type"] == kind
    ]


def _library_file(app, source, title):
    """Copy source into the library under a new movie row. Return (id, path)."""

    from app import db
    from app.tracks import replace_track_rows
    from app.tracks import get_audio_tracks_from_file, get_subtitle_tracks_from_file
    from tests.factories import make_movie, make_movie_file

    with app.app_context():
        file = make_movie_file(
            make_movie(title, 2021), "Bluray-1080p", container="Matroska"
        )
        db.session.commit()
        path = os.path.join(app.config["LIBRARY_DIR"], file.file_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        shutil.copy(source, path)
        replace_track_rows(
            file, get_audio_tracks_from_file(path), get_subtitle_tracks_from_file(path)
        )
        db.session.commit()
        return file.id, path


def _remux_away(app, path, keep_audio, keep_subtitles):
    """Remux the file in place with only the given mkvmerge track ids.

    This is a second edit that renumbers the tracks between the page
    load and the job."""

    output = f"{path}.remux.mkv"
    subprocess.run(
        [app.config["MKVMERGE_BIN"], "-o", output]
        + ["-a", ",".join(map(str, keep_audio))]
        + ["-s", ",".join(map(str, keep_subtitles))]
        + [path],
        check=True,
        capture_output=True,
    )
    os.replace(output, path)


def _scans(app, file_id):
    from tests.test_scheduling import requeue_id, scheduled_jobs

    wanted = requeue_id("app.videos.track_metadata_scan_task", file_id)
    return [job for job in scheduled_jobs(app.file_queue) if job.id == wanted]


def test_the_scan_stores_the_uid_that_mkvmerge_reports(app, two_by_two_mkv):
    from app.models import FileAudioTrack, FileSubtitleTrack

    file_id, path = _library_file(app, two_by_two_mkv, "UID Scan")
    try:
        with app.app_context():
            audio = FileAudioTrack.query.filter_by(file_id=file_id).all()
            subtitles = FileSubtitleTrack.query.filter_by(file_id=file_id).all()
            rows = sorted(audio + subtitles, key=lambda row: row.streamorder)
        ids = {uid: props["id"] for uid, props in _by_uid(app, path).items()}
        assert [(row.uid, row.streamorder) for row in rows] == [
            (uid, ids[uid]) for uid in [row.uid for row in rows]
        ]
        assert len(rows) == 4 and all(row.uid for row in rows)
    finally:
        os.remove(path)


def test_a_flag_edit_survives_a_renumbering_before_the_job(app, two_by_two_mkv):
    """The page saved the UIDs. A remux then dropped a track. The edit still lands.

    By position, "subtitle track 2" no longer exists after the remux.
    "Audio track 2" stays, but the job reorders the file for it. The
    saved UIDs reach the right tracks."""

    from app.models import FileAudioTrack, FileSubtitleTrack
    from app.tracks import mkvpropedit_unlocked

    file_id, path = _library_file(app, two_by_two_mkv, "UID Renumber")
    try:
        audio = _uids(app, path, "audio")
        subtitles = _uids(app, path, "subtitles")
        before = _by_uid(app, path)

        # The page posts: audio 2 is the default, subtitle 2 is forced.
        # Before the job runs, a remux drops subtitle 1.

        _remux_away(
            app,
            path,
            [before[uid]["id"] for uid in audio],
            [before[subtitles[1]]["id"]],
        )
        assert _uids(app, path, "subtitles") == [subtitles[1]]

        with app.app_context():
            assert mkvpropedit_unlocked(file_id, audio[1], "0", [subtitles[1]]) is True

        after = _by_uid(app, path)
        assert _uids(app, path, "audio") == [audio[1], audio[0]]
        assert after[audio[1]]["default_track"] is True
        assert after[audio[0]]["default_track"] is False
        assert after[subtitles[1]]["forced_track"] is True
        assert after[subtitles[1]]["default_track"] is False

        with app.app_context():
            rows = FileAudioTrack.query.filter_by(file_id=file_id).all()
            assert sorted((row.track, row.uid) for row in rows) == [
                (1, audio[1]),
                (2, audio[0]),
            ]
            (subtitle,) = FileSubtitleTrack.query.filter_by(file_id=file_id).all()
            assert (subtitle.track, subtitle.uid, subtitle.forced) == (
                1,
                subtitles[1],
                True,
            )
        assert _scans(app, file_id) == []
    finally:
        os.remove(path)


def test_a_remux_survives_a_renumbering_before_the_job(app, two_by_two_mkv):
    """The page kept audio 2 and subtitle 2. A remux dropped audio 1 first."""

    from app.tracks import mkvmerge_unlocked

    file_id, path = _library_file(app, two_by_two_mkv, "UID Remux Renumber")
    try:
        audio = _uids(app, path, "audio")
        subtitles = _uids(app, path, "subtitles")
        before = _by_uid(app, path)
        _remux_away(
            app,
            path,
            [before[audio[1]]["id"]],
            [before[uid]["id"] for uid in subtitles],
        )

        with app.app_context():
            assert mkvmerge_unlocked(file_id, [audio[1]], [subtitles[1]]) is True

        assert _uids(app, path, "audio") == [audio[1]]
        assert _uids(app, path, "subtitles") == [subtitles[1]]
        assert _by_uid(app, path)[audio[1]]["default_track"] is True
    finally:
        os.remove(path)


@pytest.mark.parametrize("task", ["mkvpropedit", "mkvmerge"])
def test_an_edit_for_a_uid_that_is_gone_stops_and_queues_a_scan(
    app, two_by_two_mkv, task
):
    """The file changed after the page load. The edit writes nothing (#611)."""

    from app.tracks import mkvmerge_unlocked, mkvpropedit_unlocked

    file_id, path = _library_file(app, two_by_two_mkv, f"UID Gone {task}")
    try:
        audio = _uids(app, path, "audio")
        with open(path, "rb") as handle:
            original = handle.read()
        with app.app_context():
            if task == "mkvpropedit":
                result = mkvpropedit_unlocked(file_id, audio[0], "0", ["12345"])
            else:
                result = mkvmerge_unlocked(file_id, [audio[0], "12345"], [])
        assert result is False
        with open(path, "rb") as handle:
            assert handle.read() == original
        (scan,) = _scans(app, file_id)
        assert scan.kwargs == {"file_id": file_id}
    finally:
        os.remove(path)


@pytest.mark.parametrize("uid", ["999", "None"])
def test_the_task_refuses_a_uid_that_is_gone(app, two_by_two_mkv, uid):
    """The queued task, not only the unlocked body, stops on a gone UID.

    A row with no UID posts "None" (#611). The edit stops the same way,
    and the rescan fills the UIDs."""

    from app import tracks

    file_id, path = _library_file(app, two_by_two_mkv, f"UID Gone Task {uid}")
    try:
        with app.app_context():
            assert tracks.mkvpropedit_task(file_id, uid, None, []) is False
            assert tracks.mkvmerge_task(file_id, [uid], []) is False
        assert len(_scans(app, file_id)) == 1
    finally:
        os.remove(path)


def _clear_uids(app, file_id):
    from app import db
    from app.models import FileAudioTrack, FileSubtitleTrack

    with app.app_context():
        for model in (FileAudioTrack, FileSubtitleTrack):
            model.query.filter_by(file_id=file_id).update({"uid": None})
        db.session.commit()


def _migration(name):
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "migrations",
        "versions",
        name,
    )
    spec = importlib.util.spec_from_file_location("track_uid_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def test_the_migration_adds_the_uids_before_the_restart():
    migration = _migration("8e2cf89e1536_track_uids.py")
    assert migration.revision == "8e2cf89e1536"
    assert migration.down_revision == "d7b1f4a09e52"
    assert migration.TRACK_TABLES == ("file_audio_track", "file_subtitle_track")
    source = inspect.getsource(migration.upgrade)
    assert "drop_column" not in source


def test_the_counter_drops_in_its_own_step_after_the_restart():
    migration = _migration("b3a9d1e7c4f2_drop_track_edit_seq.py")
    assert migration.revision == "b3a9d1e7c4f2"
    assert migration.down_revision == "c4e8f2a6d1b9"
    assert "track_edit_seq" in inspect.getsource(migration.upgrade)


def test_the_models_carry_the_uid(app):
    from app.models import FileAudioTrack, FileSubtitleTrack

    assert FileAudioTrack.__table__.c.uid.type.length == 20
    assert FileSubtitleTrack.__table__.c.uid.type.length == 20
