"""Pin the command listing of the Flask CLI.

The CLI commands live in register() of app/cli.py and in the
register_cli() of the modules that own them. Each group must list
the same commands as before the move. db comes from Flask-Migrate."""

import pytest

# Each group and its commands, as `flask <group> --help` lists them.

LISTING = {
    "": [
        "alerts",
        "audio",
        "aws",
        "catalog",
        "db",
        "frames",
        "recs",
        "refresh",
        "scan",
        "smb",
        "sqs",
        "sync",
        "triage",
        "tv",
    ],
    "alerts": ["availability"],
    "audio": ["atmos"],
    "aws": ["cdn-url", "provision"],
    "catalog": ["exclude"],
    "db": [
        "branches",
        "check",
        "current",
        "downgrade",
        "edit",
        "heads",
        "history",
        "init",
        "list-templates",
        "merge",
        "migrate",
        "revision",
        "show",
        "stamp",
        "upgrade",
    ],
    "frames": ["refresh"],
    "recs": [
        "awards",
        "catalog",
        "copref",
        "evaluate",
        "leaving",
        "newly-added",
        "recompute",
        "streaming",
    ],
    "refresh": ["criterion", "file", "tmdb"],
    "smb": ["history", "probe", "recheck", "repair", "status"],
    "triage": ["backfill", "backfill-audio"],
    "tv": ["rename"],
}


def _runner(app):
    """Return a CLI runner with the Fitzflix commands registered."""

    if "refresh" not in app.cli.commands:
        from app.cli import register

        register(app)
    return app.test_cli_runner()


def _listed(output):
    """Return the command names of a --help output."""

    if "Commands:" not in output:
        return None
    part = output.split("Commands:", 1)[1]
    return [line.strip().split()[0] for line in part.splitlines() if line.strip()]


@pytest.mark.parametrize("group", sorted(LISTING))
def test_each_group_lists_the_same_commands(app, group):
    args = [group, "--help"] if group else ["--help"]
    result = _runner(app).invoke(args=args)
    assert result.exit_code == 0, result.output
    assert _listed(result.output) == LISTING[group]


def test_each_command_has_its_help(app):
    """Every command still answers --help with its own docstring."""

    runner = _runner(app)
    for group, names in LISTING.items():
        if not group:
            continue
        for name in names:
            result = runner.invoke(args=[group, name, "--help"])
            assert result.exit_code == 0, (group, name, result.output)
            assert "Usage:" in result.output
