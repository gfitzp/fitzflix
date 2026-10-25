"""Test the small name helpers of app.naming.

tests/test_filenames.py holds the round trip of library_name over the
filename corpus.
"""


def test_retag_quality_changes_only_the_quality_tag(app):
    from app.naming import retag_quality

    name = "Ran (1985) - [Bluray-2160p].mkv"
    assert retag_quality(name, "Bluray-2160p", "Bluray-1080p") == (
        "Ran (1985) - [Bluray-1080p].mkv"
    )
    assert retag_quality(name, "DVD", "Bluray-1080p") == name


def test_with_extension_changes_the_last_extension(app):
    from app.naming import with_extension

    assert with_extension("Ran (1985) - [DVD].m2ts", "mkv") == "Ran (1985) - [DVD].mkv"
    assert with_extension("Ran (1985) - [DVD].mp4", ".mkv") == "Ran (1985) - [DVD].mkv"


def test_hidden_puts_a_period_before_the_name(app):
    from app.naming import hidden

    assert hidden("/library/Movies/Ran.mkv") == "/library/Movies/.Ran.mkv"
    assert hidden("/staging/Ran.mkv", ".partial") == "/staging/.Ran.mkv.partial"
    assert hidden("Ran.mkv") == ".Ran.mkv"


def test_episode_code_keeps_the_digits_of_the_name(app):
    from app.naming import episode_code

    assert episode_code(1, 2) == "S01E02"
    assert episode_code("1", "001", "3") == "S01E001-E03"
    assert episode_code(0, 0) == "S00E00"
