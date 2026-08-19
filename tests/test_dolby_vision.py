"""Dolby Vision profile parsing (#65): the flavor label from
MediaInfo's HDR-format string, and its storage/clearing through the
track-metadata writer."""

from tests.factories import make_movie, make_movie_file


def test_parse_dolby_vision_profile_reads_mediainfo_strings(app):
    from app.videos import parse_dolby_vision_profile

    # Blu-ray dual-layer (profile 7)
    assert (
        parse_dolby_vision_profile(
            "Dolby Vision, Version 1.0, dvhe.07.06, BL+EL+RPU / "
            "SMPTE ST 2086, HDR10 compatible"
        )
        == "7"
    )

    # WEB single-layer HDR10-compatible (8.1)
    assert (
        parse_dolby_vision_profile(
            "Dolby Vision, Version 1.0, dvhe.08.06, BL+RPU, HDR10 compatible"
            " / SMPTE ST 2086, HDR10 compatible"
        )
        == "8.1"
    )

    # HLG-compatible profile 8 (8.4)
    assert (
        parse_dolby_vision_profile(
            "Dolby Vision, Version 1.0, dvhe.08.06, BL+RPU, HLG compatible"
        )
        == "8.4"
    )

    # Streaming-only profile 5 (no cross-compatibility)
    assert (
        parse_dolby_vision_profile("Dolby Vision, Version 1.0, dvhe.05.06, BL+RPU")
        == "5"
    )

    # AV1-carried DV
    assert (
        parse_dolby_vision_profile("Dolby Vision, Version 1.0, dav1.10.01, BL+RPU")
        == "10"
    )

    # Plain HDR10 and absent values are not Dolby Vision
    assert parse_dolby_vision_profile("SMPTE ST 2086, HDR10 compatible") is None
    assert parse_dolby_vision_profile(None) is None


def test_track_metadata_writer_stores_and_clears_the_profile(app):
    from app import db
    from app.models import File
    from app.videos import save_track_metadata

    with app.app_context():
        movie = make_movie("Dolby Suspect", 2023)
        file = make_movie_file(movie, "Bluray-2160p")
        db.session.commit()
        file_id = file.id

        dv_details = {
            "video": {
                "format": "HEVC",
                "hdr_format": (
                    "Dolby Vision, Version 1.0, dvhe.07.06, BL+EL+RPU / "
                    "SMPTE ST 2086, HDR10 compatible"
                ),
                "dolby_vision_profile": "7",
            },
            "audio_tracks": [],
            "subtitle_tracks": [],
            "filesize_bytes": 1000,
        }
        save_track_metadata(file_id, dv_details)
        db.session.expire_all()
        stored = db.session.get(File, file_id)
        assert stored.dolby_vision_profile == "7"
        assert "Dolby Vision" in stored.hdr_format

        # A replacement file without DV clears the stale flavor — the
        # extractor always includes the keys, None when absent

        sdr_details = {
            "video": {
                "format": "HEVC",
                "hdr_format": None,
                "dolby_vision_profile": None,
            },
            "audio_tracks": [],
            "subtitle_tracks": [],
            "filesize_bytes": 1000,
        }
        save_track_metadata(file_id, sdr_details)
        db.session.expire_all()
        stored = db.session.get(File, file_id)
        assert stored.dolby_vision_profile is None
        assert stored.hdr_format is None
