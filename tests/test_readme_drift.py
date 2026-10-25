"""Test that the README matches the code on the points of #332."""

import os

README = os.path.join(os.path.dirname(__file__), "..", "README.md")


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


def test_the_readme_has_no_retired_notes(app):
    readme = _readme()

    # The library moved from SMB to NFS on 2026-08-26
    assert "nsmb.conf" not in readme
    assert "protocol_vers_map" not in readme

    # The HandBrake preset page setting was removed on 2026-10-20
    assert "the first preset in the file applies" not in readme

    # The leaving-Criterion refresh runs each day
    assert "(otherwise on the 1st of each month)" not in readme
