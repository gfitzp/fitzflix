"""Test the mkvmerge ids of the track remux (#315, #611).

The form gives the track UID of each kept track. mkvmerge selects the
tracks by their ids in the file. The remux maps each UID to its id from
a new mkvmerge -J read under the lock. A UID that the file does not
have stops the remux and queues a scan."""

import pytest

from tests.factories import make_movie, make_movie_file

# The ids by UID of a file with interleaved streams: video 0, subtitle 1,
# audio 2, audio 3, subtitle 4.

IDS = {
    "audio": {"72": 2, "73": 3},
    "subtitles": {"81": 1, "84": 4},
}


class Stop(Exception):
    """Stop the task after the command is built."""


def _remux_command(app, monkeypatch, audio, subtitles):
    """Run the track remux up to the remux. Return (result, mkvmerge commands)."""

    import app.tracks as tracks
    from app import db

    with app.app_context():
        movie = make_movie("Interleaved Film", 2001)
        file = make_movie_file(movie, "Bluray-1080p")
        db.session.commit()
        file_id = file.id

    commands = []

    def fake_remux(file_path, build, job, basename, label, before_replace=None):
        commands.append(build("in.mkv", "out.mkv"))
        raise Stop()

    monkeypatch.setattr(tracks, "mkvmerge_track_ids", lambda path: IDS)
    monkeypatch.setattr(tracks, "remux_file", fake_remux)
    try:
        result = tracks.mkvmerge_unlocked(file_id, audio, subtitles)
    except Stop:
        result = None
    return file_id, result, commands


def test_the_track_remux_maps_the_uids_to_the_ids(app, monkeypatch):
    file_id, result, (command,) = _remux_command(app, monkeypatch, ["73"], ["84", "81"])
    pairs = list(zip(command, command[1:]))
    assert ("-a", "3") in pairs
    assert ("-s", "1,4") in pairs


def test_an_unknown_uid_stops_the_remux_and_queues_a_scan(app, monkeypatch):
    from tests.test_scheduling import requeue_id, scheduled_jobs

    file_id, result, commands = _remux_command(app, monkeypatch, ["72"], ["99"])
    assert result is False
    assert commands == []
    wanted = requeue_id("app.videos.track_metadata_scan_task", file_id)
    assert [job.id for job in scheduled_jobs(app.file_queue)] == [wanted]


def test_mkvmerge_track_ids_reads_the_uids_of_a_real_file(app, tmp_path):
    """mkvmerge -J and MediaInfo give the same UID for each track (#611)."""

    from app.tracks import (
        get_audio_tracks_from_file,
        get_subtitle_tracks_from_file,
        mkvmerge_track_ids,
    )
    from tests.media import make_sample_mkv

    subtitle = tmp_path / "cue.srt"
    subtitle.write_text("1\n00:00:00,000 --> 00:00:00,500\nHello\n")
    path = make_sample_mkv(
        str(tmp_path / "uids.mkv"),
        audio=(("eng", True), ("fra", False)),
        subtitles=(subtitle,),
    )
    with app.app_context():
        ids = mkvmerge_track_ids(path)
        audio = get_audio_tracks_from_file(path)
        subtitles = get_subtitle_tracks_from_file(path)
    assert {track["uid"]: track["streamorder"] for track in audio} == ids["audio"]
    assert {track["uid"]: track["streamorder"] for track in subtitles} == ids[
        "subtitles"
    ]
    assert len(ids["audio"]) == 2 and len(ids["subtitles"]) == 1


def test_a_non_matroska_track_has_no_uid():
    from app.tracks import track_uid

    assert track_uid({}) is None
    assert track_uid({"unique_id": "1234"}) == "1234"
    assert track_uid({"unique_id": "18446744073709551615 (0xFFFFFFFFFFFFFFFF)"}) == (
        "18446744073709551615"
    )


@pytest.mark.parametrize("value", ["", "abc", None])
def test_a_track_uid_that_is_not_a_number_is_none(value):
    from app.tracks import track_uid

    assert track_uid({"unique_id": value}) is None
