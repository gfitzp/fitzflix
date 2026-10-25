"""Table tests for the 2 pure helpers of mkvpropedit_unlocked (#360).

build_propedit_args returns the argument list. Before, the task built
strings and split them on the spaces. track_order is one stable sort.
Before, it was 6 loops over the MediaInfo tracks."""

from types import SimpleNamespace

import pytest


def _track(track_type, default, streamorder):
    return SimpleNamespace(
        track_type=track_type, default=default, streamorder=streamorder
    )


@pytest.mark.parametrize(
    "audio_count, subtitle_count, default_audio, default_subtitle, forced, "
    "languages, expected",
    [
        # One audio track, no subtitles: only its default flag
        (1, 0, "1", None, [], None, ["--edit", "track:a1", "--set", "flag-default=1"]),
        # The second audio track is the default
        (
            2,
            0,
            "2",
            None,
            [],
            None,
            [
                "--edit",
                "track:a1",
                "--set",
                "flag-default=0",
                "--edit",
                "track:a2",
                "--set",
                "flag-default=1",
            ],
        ),
        # No default and no forced subtitle: the subtitle flags stay
        (
            1,
            2,
            "1",
            None,
            [],
            None,
            ["--edit", "track:a1", "--set", "flag-default=1"],
        ),
        # A forced subtitle: each subtitle gets both flags
        (
            1,
            2,
            "1",
            None,
            ["2"],
            None,
            [
                "--edit",
                "track:a1",
                "--set",
                "flag-default=1",
                "--edit",
                "track:s1",
                "--set",
                "flag-default=0",
                "--edit",
                "track:s1",
                "--set",
                "flag-forced=0",
                "--edit",
                "track:s2",
                "--set",
                "flag-default=0",
                "--edit",
                "track:s2",
                "--set",
                "flag-forced=1",
            ],
        ),
        # No audio track for a default, and a language correction
        (
            1,
            1,
            None,
            None,
            [],
            {"a1": "yue", "s1": None},
            [
                "--edit",
                "track:a1",
                "--set",
                "flag-default=0",
                "--edit",
                "track:a1",
                "--set",
                "language=yue",
            ],
        ),
    ],
)
def test_build_propedit_args(
    app,
    audio_count,
    subtitle_count,
    default_audio,
    default_subtitle,
    forced,
    languages,
    expected,
):
    from app.tracks import build_propedit_args

    assert (
        build_propedit_args(
            audio_count,
            subtitle_count,
            default_audio,
            default_subtitle,
            forced,
            languages,
        )
        == expected
    )


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
