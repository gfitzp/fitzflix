"""The lossless-audio supplement pass (#55a): the planner that decides
which tracks get FLAC twins and the ffmpeg argument builder that
realizes a plan. Both are pure functions — the presence rule keeps the
pass idempotent across S3 re-downloads, and MakeMKV rips made with the
"FLAC Plus Original Audio" profile plan as pure copies."""

from app.videos import build_supplement_args, plan_audio_supplements


def track(fmt, compression=None, language="en", channels="5.1"):
    """A minimal audio-track dict in get_audio_tracks_from_file's shape."""

    return {
        "format": fmt,
        "compression_mode": compression,
        "language": language,
        "channels": channels,
    }


def test_lossless_track_gains_a_leading_flac_twin():
    """A bare lossless track plans as [FLAC twin, original]; lossy
    company keeps its position after the pair."""

    tracks = [
        track("MLP FBA", "Lossless"),
        track("AC-3", "Lossy"),
    ]
    assert plan_audio_supplements(tracks) == [
        ("flac", 0),
        ("copy", 0),
        ("copy", 1),
    ]


def test_flac_and_pcm_originals_need_no_twin():
    """FLAC and PCM already play natively — nothing to supplement."""

    tracks = [
        track("FLAC", "Lossless"),
        track("PCM", "Lossless"),
        track("AC-3", "Lossy"),
    ]
    assert plan_audio_supplements(tracks) == [
        ("copy", 0),
        ("copy", 1),
        ("copy", 2),
    ]


def test_existing_twin_makes_the_pass_a_no_op():
    """A MakeMKV-profile rip — FLAC twin already before the lossless
    original — plans as pure copies, so re-imports never stack twins."""

    tracks = [
        track("FLAC", "Lossless"),
        track("MLP FBA", "Lossless"),
        track("AC-3", "Lossy"),
    ]
    assert plan_audio_supplements(tracks) == [
        ("copy", 0),
        ("copy", 1),
        ("copy", 2),
    ]


def test_twins_match_count_wise_within_a_group():
    """One FLAC twin can't cover two identical-language lossless
    tracks — the uncovered one still earns its twin, in place."""

    tracks = [
        track("FLAC", "Lossless"),
        track("MLP FBA", "Lossless"),
        track("DTS", "Lossless"),
    ]
    assert plan_audio_supplements(tracks) == [
        ("copy", 0),
        ("copy", 1),
        ("flac", 2),
        ("copy", 2),
    ]


def test_twins_only_count_within_language_and_channels():
    """A French FLAC doesn't cover an English lossless track, and a
    stereo FLAC doesn't cover a 5.1 one."""

    tracks = [
        track("FLAC", "Lossless", language="fr"),
        track("FLAC", "Lossless", channels="2.0"),
        track("MLP FBA", "Lossless"),
    ]
    assert plan_audio_supplements(tracks) == [
        ("copy", 0),
        ("copy", 1),
        ("flac", 2),
        ("copy", 2),
    ]


def test_builder_numbers_by_output_position():
    """Codec options follow the OUTPUT index — a twin shifts every
    later track — while -map keeps the source index; the first output
    is the default and every other disposition is cleared."""

    plan = [("flac", 0), ("copy", 0), ("copy", 1)]
    assert build_supplement_args(plan) == [
        "-map",
        "0:a:0",
        "-c:a:0",
        "flac",
        "-map",
        "0:a:0",
        "-c:a:1",
        "copy",
        "-map",
        "0:a:1",
        "-c:a:2",
        "copy",
        "-disposition:a:0",
        "default",
        "-disposition:a:1",
        "none",
        "-disposition:a:2",
        "none",
    ]
