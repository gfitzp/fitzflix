"""Table tests for the pure helpers of mkvpropedit_unlocked (#364, #611).

build_propedit_args returns the argument list. Before, the task built
strings and split them on the spaces. track_order is one stable sort.
Before, it was 6 loops over the MediaInfo tracks. missing_track_uids
finds the selections that the file does not have."""

from types import SimpleNamespace

import pytest


def _track(track_type, default, streamorder):
    return SimpleNamespace(
        track_type=track_type, default=default, streamorder=streamorder
    )


def _uids(*uids):
    """Return the track dicts of a new read of a file with these track UIDs."""

    return [{"uid": uid} for uid in uids]


def _edit(uid, field):
    return ["--edit", f"track:={uid}", "--set", field]


@pytest.mark.parametrize(
    "audio, subtitles, default_audio, default_subtitle, forced, languages, expected",
    [
        # One audio track, no subtitles: only its default flag
        (_uids("71"), [], "71", None, [], None, _edit("71", "flag-default=1")),
        # The second audio track is the default
        (
            _uids("71", "72"),
            [],
            "72",
            None,
            [],
            None,
            _edit("71", "flag-default=0") + _edit("72", "flag-default=1"),
        ),
        # No default and no forced subtitle: the subtitle flags stay
        (
            _uids("71"),
            _uids("81", "82"),
            "71",
            None,
            [],
            None,
            _edit("71", "flag-default=1"),
        ),
        # A forced subtitle: each subtitle gets both flags
        (
            _uids("71"),
            _uids("81", "82"),
            "71",
            None,
            ["82"],
            None,
            _edit("71", "flag-default=1")
            + _edit("81", "flag-default=0")
            + _edit("81", "flag-forced=0")
            + _edit("82", "flag-default=0")
            + _edit("82", "flag-forced=1"),
        ),
        # No audio track for a default, and a language correction
        (
            _uids("71"),
            _uids("81"),
            None,
            None,
            [],
            {"71": "yue", "81": None},
            _edit("71", "flag-default=0") + _edit("71", "language=yue"),
        ),
    ],
)
def test_build_propedit_args(
    app, audio, subtitles, default_audio, default_subtitle, forced, languages, expected
):
    """The edits address the tracks by UID, not by position (#611)."""

    from app.tracks import build_propedit_args

    assert (
        build_propedit_args(
            audio, subtitles, default_audio, default_subtitle, forced, languages
        )
        == expected
    )


@pytest.mark.parametrize(
    "default_audio, default_subtitle, forced, languages, expected",
    [
        ("71", "0", [], None, []),
        ("79", "0", [], None, ["79"]),
        ("71", "89", ["81", "88"], {"77": "eng"}, ["77", "88", "89"]),
        # An audio UID is not a subtitle UID
        ("71", "71", [], None, ["71"]),
        (None, None, [], {"81": "fre"}, []),
    ],
)
def test_missing_track_uids(
    app, default_audio, default_subtitle, forced, languages, expected
):
    """A selected UID that the file does not have stops the edit (#611)."""

    from app.tracks import missing_track_uids

    assert (
        missing_track_uids(
            _uids("71"),
            _uids("81"),
            default_audio,
            default_subtitle,
            forced,
            languages,
        )
        == expected
    )


def test_a_track_with_no_uid_counts_as_missing(app):
    from app.tracks import missing_track_uids

    assert missing_track_uids(_uids("71", None), [], "71", None, []) == ["no UID"]


@pytest.mark.parametrize(
    "tracks, expected",
    [
        ([], ""),
        # The default audio track moves before the other audio track
        (
            [
                _track("General", None, None),
                _track("Video", "Yes", 0),
                _track("Audio", "No", 1),
                _track("Audio", "Yes", 2),
                _track("Text", "No", 3),
                _track("Text", "Yes", 4),
            ],
            "0:0,0:2,0:1,0:4,0:3",
        ),
        # 2 tracks of the same type and flag keep the order of the file
        (
            [
                _track("Audio", "No", 2),
                _track("Audio", "No", 1),
                _track("Video", "No", 0),
            ],
            "0:0,0:2,0:1",
        ),
        # A track without a default flag is not in the order, as before
        ([_track("Video", "Yes", 0), _track("Audio", None, 1)], "0:0"),
    ],
)
def test_track_order(app, tracks, expected):
    from app.tracks import track_order

    assert track_order(tracks) == expected
