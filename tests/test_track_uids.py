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


def _row_uids(app, file_id):
    from app.models import FileAudioTrack, FileSubtitleTrack

    with app.app_context():
        return sorted(
            (row.streamorder, row.uid)
            for model in (FileAudioTrack, FileSubtitleTrack)
            for row in model.query.filter_by(file_id=file_id)
        )


def test_the_backfill_fills_the_uids_and_is_safe_to_repeat(app, two_by_two_mkv):
    from app.tracks_cli import backfill_track_uids

    file_id, path = _library_file(app, two_by_two_mkv, "UID Backfill")
    try:
        scanned = _row_uids(app, file_id)
        _clear_uids(app, file_id)
        assert all(uid is None for _, uid in _row_uids(app, file_id))

        with app.app_context():
            counts = backfill_track_uids(batch_size=1)
        assert counts["rows"] == 4 and counts["files"] == 1
        assert _row_uids(app, file_id) == scanned
        ids = {props["id"]: uid for uid, props in _by_uid(app, path).items()}
        assert scanned == [(order, ids[order]) for order, _ in scanned]

        with app.app_context():
            again = backfill_track_uids()
        assert again["rows"] == 0 and again["files"] == 0
        assert _row_uids(app, file_id) == scanned
    finally:
        os.remove(path)


def test_the_backfill_skips_a_missing_file_and_a_busy_lock(app, two_by_two_mkv):
    from app import db
    from app.models import File
    from app.tracks_cli import backfill_track_uids

    missing_id, missing_path = _library_file(app, two_by_two_mkv, "UID Missing")
    busy_id, busy_path = _library_file(app, two_by_two_mkv, "UID Busy")
    os.remove(missing_path)
    _clear_uids(app, missing_id)
    _clear_uids(app, busy_id)
    with app.app_context():
        identifier = db.session.get(File, busy_id).file_identifier()
    held = app.lock_manager.lock(identifier, 60000)
    try:
        with app.app_context():
            counts = backfill_track_uids()
        assert (counts["missing"], counts["busy"], counts["rows"]) == (1, 1, 0)
        assert all(uid is None for _, uid in _row_uids(app, busy_id))
    finally:
        app.lock_manager.unlock(held)
        os.remove(busy_path)


@pytest.fixture
def attached_mkv(app, tmp_path):
    """Build a Matroska file with 2 audio tracks, 1 subtitle and a cover image."""

    subtitle = tmp_path / "cue.srt"
    subtitle.write_text("1\n00:00:00,000 --> 00:00:00,500\nLine\n")
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 64)
    return make_sample_mkv(
        str(tmp_path / "attached.mkv"),
        audio=(("eng", True), ("fra", False)),
        subtitles=[subtitle],
        attachments=[(cover, "image/jpeg")],
    )


def _row_details(app, file_id):
    from app.models import FileAudioTrack, FileSubtitleTrack

    with app.app_context():
        return sorted(
            (model.__name__, row.streamorder, row.uid, row.language)
            for model in (FileAudioTrack, FileSubtitleTrack)
            for row in model.query.filter_by(file_id=file_id)
        )


def test_the_backfill_maps_each_row_of_a_file_with_an_attachment(app, attached_mkv):
    from app.tracks_cli import backfill_track_uids

    file_id, path = _library_file(app, attached_mkv, "UID Attached")
    try:
        scanned = _row_details(app, file_id)
        assert len(scanned) == 3
        _clear_uids(app, file_id)
        with app.app_context():
            counts = backfill_track_uids()
        assert (counts["rows"], counts["left"]) == (3, 0)
        assert _row_details(app, file_id) == scanned
    finally:
        os.remove(path)


@pytest.mark.parametrize("change", ["type", "language", "codec", "shared uid"])
def test_a_row_that_does_not_match_its_track_leaves_the_file_for_a_rescan(
    app, attached_mkv, change
):
    """A row older than the file can point at a different track (#611).

    A remux swapped the tracks, and its rescan failed. A fill by the
    stream order alone gave a row the UID of a different track. On any
    mismatch, every row of the file keeps no UID, and the file counts
    as left."""

    from app import db
    from app.models import FileAudioTrack, FileSubtitleTrack
    from app.tracks_cli import backfill_track_uids

    file_id, path = _library_file(app, attached_mkv, f"UID Mismatch {change}")
    try:
        _clear_uids(app, file_id)
        with app.app_context():
            first, second = (
                FileAudioTrack.query.filter_by(file_id=file_id)
                .order_by(FileAudioTrack.track)
                .all()
            )
            if change == "type":
                FileSubtitleTrack.query.filter_by(file_id=file_id).update(
                    {"streamorder": first.streamorder}
                )
            elif change == "language":
                first.streamorder, second.streamorder = (
                    second.streamorder,
                    first.streamorder,
                )
            elif change == "codec":
                first.format = "DTS"
            else:
                second.language = first.language
                second.streamorder = first.streamorder
            db.session.commit()
            counts = backfill_track_uids()
        assert (counts["rows"], counts["left"], counts["files"]) == (0, 1, 0)
        assert all(uid is None for _, _, uid, _ in _row_details(app, file_id))
    finally:
        os.remove(path)


@pytest.mark.parametrize(
    "row_format, codec_id, same",
    [
        ("AC-3", "A_AC3", True),
        ("AC-3", "A_EAC3", False),
        ("E-AC-3", "A_EAC3", True),
        ("DTS", "A_DTS/LOSSLESS", True),
        ("PCM", "A_PCM/INT/LIT", True),
        ("PCM", "A_MS/ACM", True),
        ("MLP FBA", "A_TRUEHD", True),
        ("UTF-8", "S_TEXT/UTF8", True),
        ("PGS", "S_HDMV/PGS", True),
        ("PGS", "S_HDMV/TEXTST", False),
        ("AAC", None, False),
        ("Unknown", "A_AAC", False),
    ],
)
def test_same_codec_pairs_a_mediainfo_format_with_a_codec_id(
    row_format, codec_id, same
):
    from app.tracks_cli import same_codec

    assert same_codec(row_format, codec_id) is same


def test_the_backfill_command_is_registered(app):
    from app.cli import register

    if "tracks" not in app.cli.commands:
        register(app)
    result = app.test_cli_runner().invoke(args=["tracks", "backfill-uids", "--help"])
    assert result.exit_code == 0
    assert "track UIDs" in result.output


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
    assert migration.down_revision == "8e2cf89e1536"
    assert "track_edit_seq" in inspect.getsource(migration.upgrade)


def test_the_models_carry_the_uid(app):
    from app.models import FileAudioTrack, FileSubtitleTrack

    assert FileAudioTrack.__table__.c.uid.type.length == 20
    assert FileSubtitleTrack.__table__.c.uid.type.length == 20
