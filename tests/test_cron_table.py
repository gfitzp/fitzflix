"""Test the cron table of the scheduler and its next-run times.

The table rows must be well formed and must have written descriptions.
The next run of a cron row follows the changes of the clock for
daylight saving time.
"""

import pytest

from app import cron_table


def test_cron_table_entries_are_well_formed(app):
    """Test that each cron-table row is well formed.

    Each row names a function that resolves, a 5-field cron string, the
    maintenance queue, and a description. The scheduler process trusts
    the table without a check."""

    import importlib

    with app.app_context():
        entries = cron_table(app.config)

    assert len(entries) >= 10
    for entry in entries:
        assert len(entry["cron"].split()) == 5, entry
        module_name, func_name = entry["func"].rsplit(".", 1)
        assert hasattr(importlib.import_module(module_name), func_name), entry
        assert entry["queue"] == "fitzflix-maintenance"
        assert entry["description"]
        assert isinstance(entry["timeout"], int)


def test_every_cron_table_schedule_gets_a_written_description():
    """Test that each cron-table schedule gets a written description.

    The schedule column of the System page never falls back to a raw
    cron string. The description came from a hardcoded map before. That
    map was 9 schedules behind the table. This test fails when the
    schedule of a new job is outside the grammar of the generator.

    The test switches on each config-dependent row. Thus, the test also
    covers the jobs that the test config leaves out of the table."""

    from app.main.admin import _cron_description

    entries = cron_table(
        {"AWS_SQS_URL": "sqs", "PLEX_URL": "plex", "PLEX_TOKEN": "token"}
    )

    raw = {
        entry["cron"]
        for entry in entries
        if _cron_description(entry["cron"]) == entry["cron"]
    }
    assert not raw, f"no written description for {sorted(raw)}"


def test_cron_descriptions_read_as_english():
    """Test the house phrasing for each frequency class.

    This includes the 2 times of day that have names, and the
    Oxford-comma list."""

    from app.main.admin import _cron_description

    assert _cron_description("* * * * *") == "Every minute"
    assert _cron_description("*/10 * * * *") == "Every 10 minutes"
    assert _cron_description("0 * * * *") == "Hourly"
    assert _cron_description("30 * * * *") == "Hourly at :30"
    assert _cron_description("7,37 * * * *") == "Twice hourly at :07 and :37"
    assert (
        _cron_description("3,18,33,48 * * * *")
        == "Four times hourly at :03, :18, :33, and :48"
    )
    assert _cron_description("0 0 * * *") == "Daily at midnight"
    assert _cron_description("0 12 * * *") == "Daily at noon"
    assert _cron_description("45 1 * * *") == "Daily at 1:45 AM"
    assert _cron_description("30 4 * * *") == "Daily at 4:30 AM"
    assert _cron_description("15 4 * * 1") == "Weekly on Monday at 4:15 AM"
    assert _cron_description("0 1 * * 0") == "Weekly on Sunday at 1:00 AM"
    assert _cron_description("0 4 1 * *") == "Monthly on the 1st at 4:00 AM"
    assert _cron_description("0 3 18 * *") == "Monthly on the 18th at 3:00 AM"


def test_cron_descriptions_fall_back_to_the_raw_string():
    """Test that an unsupported cron string shows as it is.

    The generator shows the cron string itself, not a wrong sentence."""

    from app.main.admin import _cron_description

    for cron_string in (
        "0 */6 * * *",  # an hour step
        "0-30 * * * *",  # a range
        "0 5 * * 1,4",  # 2 times weekly is not "weekly on"
        "0 0 1 1 *",  # yearly
        "0 0 1 * 1",  # day-of-month and day-of-week together
        "61 * * * *",  # out of range
        "nonsense",
    ):
        assert _cron_description(cron_string) == cron_string


def test_cron_frequency_sort_orders_by_class_then_parameter():
    """Test the ordering of the System page.

    The order is: every-X-minutes by X, hourly by minute, daily by time,
    weekly by day and time, monthly by day-of-month and time."""

    from app.main.admin import _cron_frequency_key

    ordered = [
        "*/10 * * * *",
        "*/15 * * * *",
        "0 * * * *",
        "30 * * * *",
        "0 0 * * *",
        "45 1 * * *",
        "15 2 * * *",
        "15 4 * * 1",
        "0 4 1 * *",
        "0 3 18 * *",
    ]
    assert sorted(reversed(ordered), key=_cron_frequency_key) == ordered


def test_cron_table_refreshes_streaming_availability_nightly(app):
    """Test that the cron table refreshes the streaming availability nightly.

    The availability refresh runs nightly (2026-08). The watchlist, the
    Criterion, and the filmography pages read the cache that it fills.
    They never fetch inline."""

    with app.app_context():
        entries = {entry["func"]: entry for entry in cron_table(app.config)}

    entry = entries["app.streaming.refresh_availability"]
    assert entry["cron"] == "30 4 * * *"
    assert entry["timeout"] >= 3600


@pytest.mark.parametrize(
    "utc_now,expected",
    [
        # 2026-11-01: the clock goes back 1 hour. 05:00 EST is 10:00 UTC.
        ("2026-10-31T09:00:01", "2026-11-01T10:00:00"),
        # 2027-03-14: the clock goes forward 1 hour. 05:00 EDT is 09:00 UTC.
        ("2027-03-13T10:00:01", "2027-03-14T09:00:00"),
    ],
)
def test_cron_fields_follow_daylight_saving_time(monkeypatch, utc_now, expected):
    """The first run after a change of the clock must not move by 1 hour."""

    from datetime import datetime

    from dateutil import tz

    from app import next_cron_time

    monkeypatch.setattr(tz, "tzlocal", lambda: tz.gettz("America/New_York"))
    assert next_cron_time(
        "0 5 * * *", datetime.fromisoformat(utc_now + "+00:00")
    ) == datetime.fromisoformat(expected + "+00:00")


def test_a_job_in_the_repeated_hour_runs_once(monkeypatch):
    """When the clock goes back, 1:15 occurs 2 times. The job runs at the first."""

    from datetime import datetime, timedelta

    from dateutil import tz

    from app import next_cron_time

    monkeypatch.setattr(tz, "tzlocal", lambda: tz.gettz("America/New_York"))
    first = next_cron_time(
        "15 1 * * *", datetime.fromisoformat("2026-10-31T12:00:00+00:00")
    )
    assert first == datetime.fromisoformat("2026-11-01T05:15:00+00:00")
    second = next_cron_time("15 1 * * *", first + timedelta(seconds=1))
    assert second == datetime.fromisoformat("2026-11-02T06:15:00+00:00")


def test_an_hourly_job_keeps_its_runs_in_the_repeated_hour(monkeypatch):
    """A job that runs each hour also runs in the second 1:00 to 1:59."""

    from datetime import datetime, timedelta

    from dateutil import tz

    from app import next_cron_time

    monkeypatch.setattr(tz, "tzlocal", lambda: tz.gettz("America/New_York"))
    t = datetime.fromisoformat("2026-11-01T04:55:00+00:00")
    runs = []
    for _ in range(4):
        t = next_cron_time("0 * * * *", t)
        runs.append(t.strftime("%H:%M"))
        t += timedelta(seconds=1)
    assert runs == ["05:00", "06:00", "07:00", "08:00"]


def test_the_sonarr_path_check_runs_after_the_radarr_check(app):
    """Test the cron row and the spec of the Sonarr path check (#370).

    The row needs the Sonarr settings, as the Radarr row needs the
    Radarr settings. The spec takes the queue and the timeout of the
    Radarr check."""

    from app.tasks import TASKS

    name = "app.series_rename.reconcile_sonarr_paths"
    rows = cron_table({"SONARR_URL": "http://sonarr", "SONARR_API_KEY": "key"})
    assert [row["cron"] for row in rows if row["func"] == name] == ["45 5 * * *"]
    assert name not in {row["func"] for row in cron_table({"SONARR_URL": "x"})}

    radarr = TASKS["app.radarr_push.reconcile_radarr_paths"]
    for task in (name, "app.series_rename.reconcile_sonarr_series"):
        assert TASKS[task].queue == radarr.queue == "maintenance"
        assert TASKS[task].timeout == radarr.timeout
