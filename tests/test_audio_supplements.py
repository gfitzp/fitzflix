"""Test the audio supplement passes.

The tests cover the FLAC planner and builder of #55a. They also cover
the E-AC-3 Atmos candidate detection and the remux command construction
of #55b. All of them are pure functions. The presence rules keep both
passes idempotent across S3 re-downloads. A file that already has its
supplements plans nothing.

Each test takes the `app` fixture and imports inside the function, like
the rest of the suite. The task modules capture the singleton of
get_app() at import time. An import before conftest builds the test app
would silently point each task function at production. (That occurred.
conftest now asserts against it.)"""


def track(fmt, compression=None, language="en", channels="5.1"):
    """Return a minimal audio-track dict shaped like get_audio_tracks_from_file."""

    return {
        "format": fmt,
        "compression_mode": compression,
        "language": language,
        "channels": channels,
    }


def atmos_track(codec, language="eng", fmt=None):
    """Return a minimal track dict for the Atmos candidate detector."""

    return {"codec": codec, "language": language, "format": fmt}


def test_lossless_track_gains_a_leading_flac_twin(app):
    """Plan a bare lossless track as [FLAC twin, original].

    A lossy track keeps its position after the pair."""

    from app.videos import plan_audio_supplements

    tracks = [
        track("MLP FBA", "Lossless"),
        track("AC-3", "Lossy"),
    ]
    assert plan_audio_supplements(tracks) == [
        ("flac", 0),
        ("copy", 0),
        ("copy", 1),
    ]


def test_flac_and_pcm_originals_need_no_twin(app):
    """Plan no twin for FLAC and PCM. They already play natively."""

    from app.videos import plan_audio_supplements

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


def test_existing_twin_makes_the_pass_a_no_op(app):
    """Plan pure copies for a MakeMKV-profile rip.

    That rip already has a FLAC twin before the lossless original. Thus,
    a re-import never stacks twins."""

    from app.videos import plan_audio_supplements

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


def test_twins_match_count_wise_within_a_group(app):
    """Match a twin only to its adjacent neighbor.

    The second lossless track has no FLAC before it. Thus, it still gets
    its twin, in place."""

    from app.videos import plan_audio_supplements

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


def test_twins_match_by_language_not_channels(app):
    """Match twins by language, not by channel count.

    A French FLAC does not cover an English lossless track. But a
    same-language FLAC with a DIFFERENT channel count does cover it.
    MediaInfo labels a DTS-ES Matrix source as 6.0. Its discrete content
    (and each FLAC decode of it) is 5.1. Thus, a channel-strict match
    would stack redundant twins on correct rips (the LOTR discs)."""

    from app.videos import plan_audio_supplements

    tracks = [
        track("FLAC", "Lossless", language="fr"),
        track("MLP FBA", "Lossless"),
    ]
    assert plan_audio_supplements(tracks) == [
        ("copy", 0),
        ("flac", 1),
        ("copy", 1),
    ]

    es_shaped = [
        track("FLAC", "Lossless", channels="5.1"),
        track("DTS-HD Master Audio", "Lossless", channels="6.0"),
        track("DTS", "Lossy", channels="6.0"),
    ]
    assert plan_audio_supplements(es_shaped) == [
        ("copy", 0),
        ("copy", 1),
        ("copy", 2),
    ]


def test_builder_numbers_by_output_position(app):
    """Number the codec options by the OUTPUT index.

    A twin shifts each later track. The -map option keeps the source
    index. The first output is the default. The builder clears each
    different disposition."""

    from app.videos import build_supplement_args

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


def test_truehd_atmos_without_twin_is_a_candidate(app):
    """Report a lone TrueHD Atmos track as a candidate for an E-AC-3 Atmos twin.

    Plain TrueHD and lossy tracks are never candidates."""

    from app.atmos import TRUEHD_ATMOS_CODEC, atmos_supplement_candidates

    tracks = [
        atmos_track(TRUEHD_ATMOS_CODEC),
        atmos_track("Dolby TrueHD"),
        atmos_track("Dolby Digital"),
    ]
    assert atmos_supplement_candidates(tracks) == [0]


def test_existing_eac3_atmos_twin_satisfies_the_source(app):
    """Plan nothing for a file that already has its DD+ Atmos twin.

    The pass is idempotent across S3 re-downloads."""

    from app.atmos import (
        EAC3_ATMOS_CODEC,
        TRUEHD_ATMOS_CODEC,
        atmos_supplement_candidates,
    )

    tracks = [
        atmos_track(EAC3_ATMOS_CODEC),
        atmos_track(TRUEHD_ATMOS_CODEC),
    ]
    assert atmos_supplement_candidates(tracks) == []


def test_atmos_twins_match_per_language_and_count(app):
    """Match Atmos twins per language and per count.

    A Japanese twin does not cover an English source. One twin cannot
    cover 2 same-language sources."""

    from app.atmos import (
        EAC3_ATMOS_CODEC,
        TRUEHD_ATMOS_CODEC,
        atmos_supplement_candidates,
    )

    tracks = [
        atmos_track(EAC3_ATMOS_CODEC, language="jpn"),
        atmos_track(TRUEHD_ATMOS_CODEC, language="eng"),
        atmos_track(TRUEHD_ATMOS_CODEC, language="jpn"),
        atmos_track(TRUEHD_ATMOS_CODEC, language="jpn"),
    ]
    assert atmos_supplement_candidates(tracks) == [1, 3]


def test_insertion_lands_ahead_of_the_flac_twin(app):
    """Put the E-AC-3 twin at the front of its trio.

    The twin goes before the FLAC twin that directly precedes the
    source. If there is no FLAC twin, it goes directly before the
    source."""

    from app.atmos import insertion_point

    paired = [
        {"format": "FLAC", "language": "eng"},
        {"format": "MLP FBA", "language": "eng"},
    ]
    assert insertion_point(paired, 1) == 0

    bare = [
        {"format": "AC-3", "language": "eng"},
        {"format": "MLP FBA", "language": "eng"},
    ]
    assert insertion_point(bare, 1) == 1
    assert insertion_point(bare[1:], 0) == 0


def test_remux_command_orders_the_trio(app):
    """Order the trio in the mkvmerge command.

    mkvmerge gets the ec3 inputs first and the source last. It gets an
    explicit --track-order. That order puts each twin at its insertion
    position, with the video first and the subtitles last."""

    from app.atmos import build_remux_command

    command = build_remux_command(
        "mkvmerge",
        "out.mkv",
        "source.mkv",
        video_orders=[0],
        audio_orders=[1, 2, 3],
        text_orders=[4],
        inserts=[(0, "twin.ec3", "eng")],
    )
    assert command[:3] == ["mkvmerge", "-o", "out.mkv"]
    assert "twin.ec3" in command and "source.mkv" in command
    assert command.index("twin.ec3") < command.index("source.mkv")
    order = command[command.index("--track-order") + 1]
    # Order: video, then [ec3 twin, FLAC, TrueHD, other audio], then subtitles
    assert order == "1:0,0:0,1:1,1:2,1:3,1:4"
    language = command[command.index("--language") + 1]
    assert language == "0:eng"


def test_mediaconvert_settings_match_the_validated_job(app):
    """Keep the validated shape of the job settings.

    The shape is a RAW container, EAC3_ATMOS at 9.1.6, and the bitrate
    passed through."""

    from app.atmos import mediaconvert_job_settings

    settings = mediaconvert_job_settings("s3://b/in.atmos", "s3://b/out/x", 1024000)
    assert settings["Inputs"][0]["FileInput"] == "s3://b/in.atmos"
    output = settings["OutputGroups"][0]["Outputs"][0]
    assert output["ContainerSettings"]["Container"] == "RAW"
    codec = output["AudioDescriptions"][0]["CodecSettings"]
    assert codec["Codec"] == "EAC3_ATMOS"
    assert codec["Eac3AtmosSettings"] == {
        "Bitrate": 1024000,
        "CodingMode": "CODING_MODE_9_1_6",
        "SampleRate": 48000,
    }


def test_trailing_flac_is_never_counted_as_a_twin(app):
    """Never count a FLAC AFTER a lossless track as a twin.

    That FLAC could be anything, for example a commentary. Thus, it
    never satisfies the twin rule (the Father Goose case). The lossless
    track gets a new twin before it. The unknown FLAC keeps its place.
    The planner does not judge its identity."""

    from app.videos import plan_audio_supplements

    tracks = [
        track("DTS-HD Master Audio", "Lossless"),
        track("FLAC", "Lossless"),
    ]
    assert plan_audio_supplements(tracks) == [
        ("flac", 0),
        ("copy", 0),
        ("copy", 1),
    ]

    # After the supplement, the file is in the twinned shape. The second
    # run plans pure copies

    supplemented = [
        track("FLAC", "Lossless"),
        track("DTS-HD Master Audio", "Lossless"),
        track("FLAC", "Lossless"),
    ]
    assert plan_audio_supplements(supplemented) == [
        ("copy", 0),
        ("copy", 1),
        ("copy", 2),
    ]


def test_non_adjacent_flac_is_not_a_twin(app):
    """Count only an adjacent FLAC as a twin.

    A FLAC that a different track separates from the lossless track does
    not count. This is true even when the language and the channels
    match."""

    from app.videos import plan_audio_supplements

    tracks = [
        track("FLAC", "Lossless"),
        track("AC-3", "Lossy"),
        track("MLP FBA", "Lossless"),
    ]
    assert plan_audio_supplements(tracks) == [
        ("copy", 0),
        ("copy", 1),
        ("flac", 2),
        ("copy", 2),
    ]
