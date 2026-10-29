"""Test the mkvmerge ids of the track remux (#315, #611).

The form gives the track UID of each kept track. mkvmerge selects the
tracks by their ids in the file. The remux maps each UID to its id from
a new mkvmerge -J read under the lock. tests/test_track_uids.py runs the
remux on real files."""

import pytest


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
