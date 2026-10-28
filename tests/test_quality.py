"""Test downgrade_quality_title, the webhook quality rename for non-physical media."""

import pytest

from app.api.arr import downgrade_quality_title

MIN_SCORE = 1600  # the default of RADARR_WEBDL_MIN_SCORE and SONARR_WEBDL_MIN_SCORE
HIGH_SCORE = 2000  # at or above the custom-format threshold. Result: WEBDL
LOW_SCORE = 100  # below the threshold. Result: WEBRip


@pytest.mark.parametrize(
    "original,expected",
    [
        # These are the mappings that the README documents.
        ("DVD", "WEBDL-480p"),
        ("Bluray-480p", "WEBDL-480p"),
        ("Bluray-720p", "WEBDL-720p"),
        ("Bluray-1080p", "WEBDL-1080p"),
        ("Bluray-1080p Remux", "WEBDL-1080p"),
        ("Remux-1080p", "WEBDL-1080p"),
        ("Bluray-2160p Remux", "WEBDL-2160p"),
        # Radarr titles that the old chain of replacements broke (#506).
        ("DVD-R", "WEBDL-480p"),
        ("DVDSCR", "WEBDL-480p"),
        ("Bluray-576p", "WEBDL-480p"),
        ("Remux-2160p", "WEBDL-2160p"),
        # A web source does not change.
        ("WEBDL-1080p", "WEBDL-1080p"),
        ("HDTV-720p", "HDTV-720p"),
    ],
)
def test_high_score_downgrades(original, expected):
    assert downgrade_quality_title(original, HIGH_SCORE, MIN_SCORE) == expected


@pytest.mark.parametrize(
    "original,expected",
    [
        ("DVD", "WEBRip-480p"),
        ("Bluray-1080p", "WEBRip-1080p"),
        ("Bluray-1080p Remux", "WEBRip-1080p"),
        ("WEBDL-1080p", "WEBRip-1080p"),
        # There is no WEBDL substring to demote. Thus, the title does not
        # change, even at a low score.
        ("HDTV-720p", "HDTV-720p"),
    ],
)
def test_low_score_becomes_webrip(original, expected):
    assert downgrade_quality_title(original, LOW_SCORE, MIN_SCORE) == expected


def test_threshold_boundary():
    assert downgrade_quality_title("Bluray-1080p", 1600, MIN_SCORE) == "WEBDL-1080p"
    assert downgrade_quality_title("Bluray-1080p", 1599, MIN_SCORE) == "WEBRip-1080p"


def test_the_threshold_is_an_argument():
    """The callers pass the setting of their service (#290). It can be negative."""

    assert downgrade_quality_title("Bluray-1080p", 100, 50) == "WEBDL-1080p"
    assert downgrade_quality_title("Bluray-1080p", 100, 500) == "WEBRip-1080p"
    assert downgrade_quality_title("Bluray-1080p", -20, -50) == "WEBDL-1080p"


def test_every_downgraded_title_is_a_ref_quality_title():
    """Each value of the map must be a title that the importer knows (#506)."""

    from app.api.arr import WEB_EQUIVALENT_QUALITY
    from tests.conftest import QUALITIES

    known = {title for title, _, _ in QUALITIES}
    for original in WEB_EQUIVALENT_QUALITY:
        assert downgrade_quality_title(original, HIGH_SCORE, MIN_SCORE) in known
        assert downgrade_quality_title(original, LOW_SCORE, MIN_SCORE) in known
