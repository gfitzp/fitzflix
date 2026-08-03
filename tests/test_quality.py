"""downgrade_quality_title: webhook quality renaming for non-physical media."""

import pytest

from app.api.arr import downgrade_quality_title

HIGH_SCORE = 2000  # at or above the custom-format threshold -> WEBDL
LOW_SCORE = 100  # below it -> WEBRip


@pytest.mark.parametrize(
    "original,expected",
    [
        # The README's documented mappings
        ("DVD", "WEBDL-480p"),
        ("Bluray-480p", "WEBDL-480p"),
        ("Bluray-720p", "WEBDL-720p"),
        ("Bluray-1080p", "WEBDL-1080p"),
        ("Bluray-1080p Remux", "WEBDL-1080p"),
        ("Remux-1080p", "WEBDL-1080p"),
        ("Bluray-2160p Remux", "WEBDL-2160p"),
        # Web sources pass through unchanged
        ("WEBDL-1080p", "WEBDL-1080p"),
        ("HDTV-720p", "HDTV-720p"),
    ],
)
def test_high_score_downgrades(original, expected):
    assert downgrade_quality_title(original, HIGH_SCORE) == expected


@pytest.mark.parametrize(
    "original,expected",
    [
        ("DVD", "WEBRip-480p"),
        ("Bluray-1080p", "WEBRip-1080p"),
        ("Bluray-1080p Remux", "WEBRip-1080p"),
        ("WEBDL-1080p", "WEBRip-1080p"),
        # No WEBDL substring to demote: unchanged even at a low score
        ("HDTV-720p", "HDTV-720p"),
    ],
)
def test_low_score_becomes_webrip(original, expected):
    assert downgrade_quality_title(original, LOW_SCORE) == expected


def test_threshold_boundary():
    assert downgrade_quality_title("Bluray-1080p", 1600) == "WEBDL-1080p"
    assert downgrade_quality_title("Bluray-1080p", 1599) == "WEBRip-1080p"
