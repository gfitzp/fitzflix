"""Test the default-flag rule on real Matroska files.

The rule from Glenn (2026-08-24, closed #194): the FIRST audio track is
always the default track. An E-AC-3 Atmos track is the default only when
it is the first track. This is important because Infuse plays the track
that has the flag. Thus, the flag decides what a viewer hears. The
channel count does not decide. The Plex display does not decide.

3 code paths write that flag: inspect_localized_file in the import, the
mkvpropedit editor, and the Atmos supplement. None of them had a test.
These tests build a file with 3 audio tracks with ffmpeg and mkvmerge.
That is the same harness that test_localization.py uses. They check the
rule end to end. This includes the half that the code does not make
obvious: when the caller selects a different default, Fitzflix REORDERS
the file. That track becomes the first track. Thus, "default" and
"first" stay the same thing.
"""

import os
import shutil

import pytest

from tests.conftest import _TMP
from tests.media import make_sample_mkv


@pytest.fixture(scope="session")
def three_audio_mkv(app):
    """Build a 1-second Matroska with eng/fra/deu audio.

    The default flag is on the SECOND track on purpose. Thus, the rule
    has something to correct."""

    return make_sample_mkv(
        os.path.join(_TMP, "three-audio.mkv"),
        audio=(("eng", False), ("fra", True), ("deu", False)),
    )


def _audio(app, path):
    """Return (language, default) for each audio track, read the same way as the app."""

    from app.tracks import get_audio_tracks_from_file

    with app.app_context():
        return [
            (track["language"], bool(track["default"]))
            for track in get_audio_tracks_from_file(path)
        ]


def _uids(file_id):
    """Return the audio track UIDs of the library file of a row, in file order.

    The edits address a track by its UID (#611). Call this in an app
    context."""

    from flask import current_app

    from app import db
    from app.models import File
    from app.tracks import get_audio_tracks_from_file

    file = db.session.get(File, file_id)
    path = os.path.join(current_app.config["LIBRARY_DIR"], file.file_path)
    return [track["uid"] for track in get_audio_tracks_from_file(path)]


def _uid(file_id, position):
    """Return the UID of the audio track at a 1-based position of a row's file."""

    return _uids(file_id)[position - 1]


def test_the_fixture_really_starts_out_wrong(app, three_audio_mkv):
    """Check the fixture itself.

    If mkvmerge stops obeying the flags that this fixture asks for, the
    tests below pass without proof of anything."""

    assert _audio(app, three_audio_mkv) == [
        ("eng", False),
        ("fra", True),
        ("deu", False),
    ]


def test_import_makes_the_first_audio_track_the_only_default(
    app, three_audio_mkv, tmp_path
):
    """Test the half of the rule in the import path.

    The flag on the disc is not important. The file that reaches the
    library has its first track as the default."""

    from app.importing import inspect_localized_file

    staged = str(tmp_path / "Flag Test (2021) - [Bluray-1080p].mkv")
    shutil.copy(three_audio_mkv, staged)

    with app.app_context():
        inspect_localized_file(staged, "Matroska")

    assert _audio(app, staged) == [("eng", True), ("fra", False), ("deu", False)]


def test_choosing_a_later_default_moves_it_to_the_front(app, three_audio_mkv):
    """Test the half of the rule that the flag edit alone does not show.

    When the caller selects a default that is not first, Fitzflix remuxes
    the file. That track becomes first. This keeps "the default track"
    and "the first track" the same thing. The Atmos twins depend on this
    invariant."""

    from app import db
    from app.tracks import mkvpropedit_unlocked
    from tests.factories import make_movie, make_movie_file

    with app.app_context():
        movie = make_movie("Reorder Test", 2021)
        file = make_movie_file(movie, "Bluray-1080p")
        db.session.commit()
        file_id = file.id
        library_path = os.path.join(app.config["LIBRARY_DIR"], file.file_path)

    os.makedirs(os.path.dirname(library_path), exist_ok=True)
    shutil.copy(three_audio_mkv, library_path)

    # Ask for the SECOND track (fra) as the default.

    with app.app_context():
        assert mkvpropedit_unlocked(file_id, _uid(file_id, 2), None, None) is True

    # French is now first, and it is the only default. The other 2 tracks
    # keep their order behind it.

    assert _audio(app, library_path) == [
        ("fra", True),
        ("eng", False),
        ("deu", False),
    ]


def test_the_stored_track_rows_follow_the_reordered_file(app, three_audio_mkv):
    """Make sure that the rule also reaches the database.

    The File page and the Atmos candidate search read the track rows.
    Fitzflix rebuilds those rows from the reordered file, not from the
    order that the caller asked about."""

    from app import db
    from app.models import FileAudioTrack
    from app.tracks import mkvpropedit_unlocked
    from tests.factories import make_movie, make_movie_file

    with app.app_context():
        movie = make_movie("Row Order Test", 2021)
        file = make_movie_file(movie, "Bluray-1080p")
        db.session.commit()
        file_id = file.id
        library_path = os.path.join(app.config["LIBRARY_DIR"], file.file_path)

    os.makedirs(os.path.dirname(library_path), exist_ok=True)
    shutil.copy(three_audio_mkv, library_path)

    with app.app_context():
        assert mkvpropedit_unlocked(file_id, _uid(file_id, 3), None, None) is True

        rows = (
            FileAudioTrack.query.filter_by(file_id=file_id)
            .order_by(FileAudioTrack.track)
            .all()
        )
        assert [(row.track, row.language, bool(row.default)) for row in rows] == [
            (1, "deu", True),
            (2, "eng", False),
            (3, "fra", False),
        ]


def test_audio_in_a_language_with_no_2_letter_code_keeps_its_code(app, tmp_path):
    """Cantonese has no ISO 639-1 code. MediaInfo gives it 3 language elements."""

    import subprocess

    source = tmp_path / "source.mkv"
    tagged = tmp_path / "cantonese.mkv"
    subprocess.run(
        [
            app.config["FFMPEG_BIN"],
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=1:size=160x120:rate=10",
            "-f",
            "lavfi",
            "-i",
            "sine=duration=1",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            "-shortest",
            str(source),
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            app.config["MKVMERGE_BIN"],
            "-q",
            "-o",
            str(tagged),
            "--language",
            "1:yue",
            str(source),
        ],
        check=True,
        capture_output=True,
    )
    assert [language for language, _default in _audio(app, str(tagged))] == ["yue"]


def _stale_after_failed_commit(app, monkeypatch, three_audio_mkv, title, run):
    """Run a library rewrite whose row write fails after the file changed.

    Return the aws_stale flag of the row afterwards (#517)."""

    from app import db
    from app.models import File
    import app.tracks as tracks
    from tests.factories import make_movie, make_movie_file

    monkeypatch.setitem(app.config, "ARCHIVE_ORIGINAL_MEDIA", True)
    with app.app_context():
        movie = make_movie(title, 2021)
        file = make_movie_file(movie, "Bluray-1080p")
        db.session.commit()
        file_id = file.id
        library_path = os.path.join(app.config["LIBRARY_DIR"], file.file_path)

    os.makedirs(os.path.dirname(library_path), exist_ok=True)
    shutil.copy(three_audio_mkv, library_path)
    before = os.stat(library_path).st_ino

    def lost_row(file_id):
        raise RuntimeError("1020 record has changed since last read")

    with app.app_context():
        with monkeypatch.context() as patch:
            patch.setattr(tracks, "_fresh_file", lost_row)
            with pytest.raises(RuntimeError):
                run(tracks, file_id)
        # The remux renamed a new file over the old one.
        assert os.stat(library_path).st_ino != before
        return db.session.get(File, file_id).aws_stale


def test_a_reorder_that_fails_its_commit_marks_the_archive_stale(
    app, monkeypatch, three_audio_mkv
):
    """The file on disk is new and the row is old. The sync must upload it (#517)."""

    assert _stale_after_failed_commit(
        app,
        monkeypatch,
        three_audio_mkv,
        "Stale Reorder",
        lambda tracks, file_id: tracks.mkvpropedit_unlocked(
            file_id, _uid(file_id, 2), None, None
        ),
    )


def test_a_remux_that_fails_its_commit_marks_the_archive_stale(
    app, monkeypatch, three_audio_mkv
):
    assert _stale_after_failed_commit(
        app,
        monkeypatch,
        three_audio_mkv,
        "Stale Remux",
        lambda tracks, file_id: tracks.mkvmerge_unlocked(file_id, _uids(file_id), []),
    )


def test_the_scan_marks_the_archive_stale_before_its_remux(app, monkeypatch):
    """A scan that fails after the empty-subtitle remux keeps the flag (#517)."""

    from app import db
    from app.models import File
    import app.tracks as tracks
    from tests.factories import make_movie, make_movie_file

    monkeypatch.setitem(app.config, "ARCHIVE_ORIGINAL_MEDIA", True)
    with app.app_context():
        file = make_movie_file(make_movie("Stale Scan", 2021), "Bluray-1080p")
        db.session.commit()
        file_id = file.id

    class Matroska:
        track_type, format = "General", "Matroska"

    def remux(path, before_replace=None):
        before_replace()
        return True

    def unreadable(path):
        raise OSError("the volume went away")

    monkeypatch.setattr(
        tracks.MediaInfo, "parse", lambda path: type("I", (), {"tracks": [Matroska]})
    )
    monkeypatch.setattr(tracks, "remove_empty_subtitle_tracks", remux)
    monkeypatch.setattr(tracks, "_extract_media_details", unreadable)
    with app.app_context():
        with pytest.raises(OSError):
            tracks.extract_track_metadata("/nowhere.mkv", file_id)
        assert db.session.get(File, file_id).aws_stale


def _propedit_fixture(app, monkeypatch, three_audio_mkv, title, binary):
    from app import db
    from tests.factories import make_movie, make_movie_file

    monkeypatch.setitem(app.config, "MKVPROPEDIT_BIN", str(binary))
    monkeypatch.setitem(app.config, "ARCHIVE_ORIGINAL_MEDIA", True)
    with app.app_context():
        file = make_movie_file(make_movie(title, 2021), "Bluray-1080p")
        db.session.commit()
        file_id = file.id
        library_path = os.path.join(app.config["LIBRARY_DIR"], file.file_path)
    os.makedirs(os.path.dirname(library_path), exist_ok=True)
    shutil.copy(three_audio_mkv, library_path)
    return file_id, library_path


def test_a_mkvpropedit_that_cannot_start_leaves_the_archive_current(
    app, monkeypatch, three_audio_mkv, tmp_path
):
    """The stale flag waits for the tool to run (follow-up #517, #581).

    The flag was committed before mkvpropedit started. A tool that never
    wrote left a stale row. That row refused a direct restore."""

    from app import db
    from app.models import File
    import app.tracks as tracks

    file_id, library_path = _propedit_fixture(
        app, monkeypatch, three_audio_mkv, "Absent Propedit", tmp_path / "absent"
    )
    with app.app_context():
        with pytest.raises(FileNotFoundError):
            tracks.mkvpropedit_unlocked(file_id, _uid(file_id, 2), None, None)
        db.session.expire_all()
        assert not db.session.get(File, file_id).aws_stale
    assert os.path.exists(library_path)


def test_a_failed_mkvpropedit_marks_the_archive_stale(
    app, monkeypatch, three_audio_mkv, tmp_path
):
    """A tool that ran and failed can have written the file (#561).

    mkvpropedit writes in place. An error exit after its first write
    left new bytes under a clean flag. The flag now goes on once the
    tool ran."""

    import subprocess

    from app import db
    from app.models import File
    import app.tracks as tracks

    broken = tmp_path / "mkvpropedit"
    broken.write_text("#!/bin/sh\necho 'Error: the file is locked'\nexit 2\n")
    broken.chmod(0o755)
    file_id, library_path = _propedit_fixture(
        app, monkeypatch, three_audio_mkv, "Failed Propedit", broken
    )
    with app.app_context():
        with pytest.raises(subprocess.CalledProcessError):
            tracks.mkvpropedit_unlocked(file_id, _uid(file_id, 2), None, None)
        db.session.expire_all()
        assert db.session.get(File, file_id).aws_stale


def test_a_failed_stale_flag_stops_the_remux_before_the_rename(
    app, monkeypatch, three_audio_mkv, tmp_path
):
    """A flag that cannot commit stops the rewrite (#561).

    Before, the flag swallowed the fault and the rename went on. The
    same database fault then broke the commit after the rewrite."""

    import app.tracks as tracks

    def lost(*args, **kwargs):
        raise RuntimeError("Lost connection to MySQL server")

    library_path = tmp_path / "Flag Fault (2021) - [Bluray-1080p].mkv"
    shutil.copy(three_audio_mkv, library_path)
    before = library_path.read_bytes()
    monkeypatch.setitem(app.config, "ARCHIVE_ORIGINAL_MEDIA", True)
    monkeypatch.setitem(app.config, "STAGING_DIR", str(tmp_path / "staging"))
    os.makedirs(app.config["STAGING_DIR"], exist_ok=True)
    with app.app_context():
        monkeypatch.setattr(tracks.db.session, "commit", lost)
        with pytest.raises(RuntimeError):
            tracks.remux_file(
                str(library_path),
                lambda source, output: [
                    app.config["MKVMERGE_BIN"],
                    "-o",
                    output,
                    source,
                ],
                None,
                library_path.name,
                "Remuxing",
                before_replace=lambda: tracks.flag_archive_before_rewrite(1),
            )
    assert library_path.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []


def _scheduled_scans(app, file_id):
    """Return the ids of the scheduled track scans of a file."""

    from rq.registry import ScheduledJobRegistry

    from app.tasks import requeue_job_id

    wanted = requeue_job_id("app.videos.track_metadata_scan_task", file_id)
    return [
        job_id
        for job_id in ScheduledJobRegistry(queue=app.file_queue).get_job_ids()
        if job_id == wanted
    ]


@pytest.mark.parametrize(
    "title, run",
    [
        (
            "Rescan Reorder",
            lambda tracks, file_id: tracks.mkvpropedit_task(
                file_id, _uid(file_id, 2), None, None
            ),
        ),
        (
            "Rescan Remux",
            lambda tracks, file_id: tracks.mkvmerge_task(file_id, _uids(file_id), []),
        ),
    ],
)
def test_a_fault_after_a_restructure_queues_a_track_scan(
    app, monkeypatch, three_audio_mkv, title, run
):
    """The rows keep the old layout after the fault. A scan repairs them (#583).

    Before, only the maintenance button queued a scan. The file page and
    the Atmos search read the wrong rows until then. The task wrapper
    queues the scan on each raise (#614)."""

    from rq.job import Job

    with app.app_context():
        _stale_after_failed_commit(app, monkeypatch, three_audio_mkv, title, run)
        from app.models import File

        file_id = File.query.order_by(File.id.desc()).first().id
    scans = _scheduled_scans(app, file_id)
    assert len(scans) == 1
    job = Job.fetch(scans[0], connection=app.redis)
    assert job.func_name == "app.videos.track_metadata_scan_task"
    assert job.kwargs == {"file_id": file_id}


def test_a_clean_edit_queues_no_track_scan(app, monkeypatch, three_audio_mkv):
    """A committed edit leaves the rows on the file layout. No scan is queued."""

    from app import db
    from app.tracks import mkvpropedit_unlocked
    from tests.factories import make_movie, make_movie_file

    with app.app_context():
        file = make_movie_file(make_movie("No Rescan", 2021), "Bluray-1080p")
        db.session.commit()
        file_id = file.id
        library_path = os.path.join(app.config["LIBRARY_DIR"], file.file_path)
    os.makedirs(os.path.dirname(library_path), exist_ok=True)
    shutil.copy(three_audio_mkv, library_path)
    with app.app_context():
        assert mkvpropedit_unlocked(file_id, _uid(file_id, 2), None, None) is True
    assert _scheduled_scans(app, file_id) == []


def test_a_failed_flag_after_a_clean_mkvpropedit_still_commits_and_uploads(
    app, monkeypatch, three_audio_mkv
):
    """The flag after the in-place write is best effort (#584).

    The strict flag raised on a database blip after mkvpropedit wrote
    the file. The row commit and the upload at the end never ran, and
    the archive kept the old bytes with a clean flag."""

    import sqlalchemy.exc

    from app import db
    from app.models import FileAudioTrack
    import app.tracks as tracks
    from tests.factories import make_movie, make_movie_file

    monkeypatch.setitem(app.config, "ARCHIVE_ORIGINAL_MEDIA", True)
    with app.app_context():
        file = make_movie_file(make_movie("Flag Blip", 2021), "Bluray-1080p")
        db.session.commit()
        file_id = file.id
        library_path = os.path.join(app.config["LIBRARY_DIR"], file.file_path)
    os.makedirs(os.path.dirname(library_path), exist_ok=True)
    shutil.copy(three_audio_mkv, library_path)

    flags = []

    def blip(file_id, strict=False):
        flags.append(strict)
        raise sqlalchemy.exc.OperationalError("UPDATE", {}, Exception("gone away"))

    uploads = []
    monkeypatch.setattr(tracks, "_flag_rewritten_archive", blip)
    monkeypatch.setattr(
        tracks, "archive_library_file", lambda file, path: uploads.append(path)
    )
    with app.app_context():
        assert (
            tracks.mkvpropedit_unlocked(file_id, _uid(file_id, 1), None, None) is True
        )
        assert FileAudioTrack.query.filter_by(file_id=file_id).count() == 3
    assert flags == [True]
    assert uploads == [library_path]
    assert _audio(app, library_path) == [("eng", True), ("fra", False), ("deu", False)]
