"""Test that the README matches the code on the points of #332."""

import os

README = os.path.join(os.path.dirname(__file__), "..", "README.md")
CONFIG = os.path.join(os.path.dirname(__file__), "..", "config.py")


def _readme():
    with open(README, encoding="utf-8") as f:
        return f.read()


def test_the_readme_gives_the_trail_retention_of_the_code(app):
    from app.pipeline import TRAIL_TTL_SECONDS

    days = {3: "three", 7: "seven"}[TRAIL_TTL_SECONDS // 86400]
    assert f"The trails stay for {days} days." in _readme()


def test_the_readme_documents_the_settings_that_config_reads(app):
    readme = _readme()
    for name in ("MOUNT_URLS", "TODO_EMAIL", "MOVE_TASK_TIMEOUT", "FILE_TASK_TIMEOUT"):
        assert f"`{name}`" in readme, name


def _settings_that_config_reads():
    """Return each name that config.py reads from the environment."""

    import re

    with open(CONFIG, encoding="utf-8") as f:
        source = f.read()
    names = re.findall(r'os\.environ\.get\(\s*"([A-Z0-9_]+)"', source)
    names += re.findall(r'_on_by_default\(\s*"([A-Z0-9_]+)"', source)
    return sorted(set(names))


def test_the_readme_names_each_setting_that_config_reads(app):
    """A new setting of config.py must come with its README entry (#350)."""

    readme = _readme()
    names = _settings_that_config_reads()
    assert len(names) > 100
    missing = [name for name in names if name not in readme]
    assert missing == []


def test_the_readme_has_no_retired_notes(app):
    readme = _readme()

    # The library moved from SMB to NFS on 2026-08-26
    assert "nsmb.conf" not in readme
    assert "protocol_vers_map" not in readme

    # The HandBrake preset page setting was removed on 2026-10-20
    assert "the first preset in the file applies" not in readme

    # The leaving-Criterion refresh runs each day
    assert "(otherwise on the 1st of each month)" not in readme
