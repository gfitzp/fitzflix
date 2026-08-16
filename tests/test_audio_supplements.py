"""The audio supplement passes: #55a's FLAC planner/builder and #55b's
E-AC-3 Atmos candidate detection and remux command construction. All
pure functions — presence rules keep both passes idempotent across S3
re-downloads, and already-supplemented files plan nothing.

Every test takes the `app` fixture and imports inside the function,
like the rest of the suite: task modules capture get_app()'s singleton
at import time, so importing them before conftest builds the test app
would silently point every task function at production. (It happened;
conftest now asserts against it.)"""


def track(fmt, compression=None, language="en", channels="5.1"):
    """A minimal audio-track dict in get_audio_tracks_from_file's shape."""

    return {
        "format": fmt,
        "compression_mode": compression,
        "language": language,
        "channels": channels,
    }


def atmos_track(codec, language="eng", fmt=None):
    """A minimal track dict for the Atmos candidate detector."""

    return {"codec": codec, "language": language, "format": fmt}


def test_lossless_track_gains_a_leading_flac_twin(app):
    """A bare lossless track plans as [FLAC twin, original]; lossy
    company keeps its position after the pair."""

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
    """FLAC and PCM already play natively — nothing to supplement."""

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
    """A MakeMKV-profile rip — FLAC twin already before the lossless
    original — plans as pure copies, so re-imports never stack twins."""

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
    """One FLAC twin can't cover two identical-language lossless
    tracks — the uncovered one still earns its twin, in place."""

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


def test_twins_only_count_within_language_and_channels(app):
    """A French FLAC doesn't cover an English lossless track, and a
    stereo FLAC doesn't cover a 5.1 one."""

    from app.videos import plan_audio_supplements

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


def test_builder_numbers_by_output_position(app):
    """Codec options follow the OUTPUT index — a twin shifts every
    later track — while -map keeps the source index; the first output
    is the default and every other disposition is cleared."""

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
    """A lone TrueHD Atmos track wants an E-AC-3 Atmos twin; plain
    TrueHD and lossy company never do."""

    from app.atmos import TRUEHD_ATMOS_CODEC, atmos_supplement_candidates

    tracks = [
        atmos_track(TRUEHD_ATMOS_CODEC),
        atmos_track("Dolby TrueHD"),
        atmos_track("Dolby Digital"),
    ]
    assert atmos_supplement_candidates(tracks) == [0]


def test_existing_eac3_atmos_twin_satisfies_the_source(app):
    """A file already carrying its DD+ Atmos twin plans nothing — the
    pass is idempotent across S3 re-downloads."""

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
    """A Japanese twin doesn't cover an English source, and one twin
    can't cover two same-language sources."""

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
    """The E-AC-3 twin leads its trio: it lands ahead of the FLAC twin
    that directly precedes the source, else directly ahead of the
    source itself."""

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
    """mkvmerge gets ec3 inputs first, the source last, and an explicit
    --track-order that lands each twin at its insertion position with
    video leading and subtitles trailing."""

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
    # video, then [ec3 twin, FLAC, TrueHD, other audio], then subtitles
    assert order == "1:0,0:0,1:1,1:2,1:3,1:4"
    language = command[command.index("--language") + 1]
    assert language == "0:eng"


def test_mediaconvert_settings_match_the_validated_job(app):
    """The job settings keep the validated shape: RAW container,
    EAC3_ATMOS at 9.1.6, and the bitrate passed through."""

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
