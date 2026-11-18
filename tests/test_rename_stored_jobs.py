"""Test scripts/rename_stored_jobs.py: the new names of the jobs that Redis holds."""

import importlib.util
import pathlib
import re

from rq.job import Job

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _script():
    """Load the script as a module. The scripts folder is not a package."""

    path = ROOT / "scripts" / "rename_stored_jobs.py"
    spec = importlib.util.spec_from_file_location("rename_stored_jobs", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_pair_renames_one_task_or_each_task_of_a_module():
    new_name = _script().new_name
    pairs = [("app.old", "app.new"), ("app.one.task", "app.two.job")]

    assert new_name("app.old.task", pairs) == "app.new.task"
    assert new_name("app.old.deeper.task", pairs) == "app.new.deeper.task"
    assert new_name("app.one.task", pairs) == "app.two.job"

    # A longer name with the same start is a different name.

    assert new_name("app.older.task", pairs) is None
    assert new_name("app.one.task_two", pairs) is None
    assert new_name(None, pairs) is None


def test_the_report_changes_nothing_and_apply_renames_each_place(app):
    script = _script()
    pairs = [("app.settings", "app.moved_settings")]
    with app.app_context():
        job = app.maintenance_queue.enqueue(
            "app.settings.get_setting",
            "PLEX_TRASH_LIMIT",
            meta={"func": "app.settings.get_setting"},
        )
        other = app.maintenance_queue.enqueue("app.frames.refresh_frame_pool_task")
        app.redis.hset(
            script.LATEST_KEY,
            "app.settings.get_setting|0 5 * * *",
            "2026-11-17T05:00:00",
        )
        app.redis.hset(
            script.LATEST_KEY, "app.frames.refresh_frame_pool_task|30 6 * * *", "kept"
        )

        counts = script.rename_stored_jobs(app.redis, pairs)
        assert counts == {
            "jobs with the status queued": 1,
            "jobs with the name in their meta": 1,
            "last-run records of the scheduler": 1,
        }
        assert Job.fetch(job.id, connection=app.redis).func_name == (
            "app.settings.get_setting"
        )

        assert script.rename_stored_jobs(app.redis, pairs, apply=True) == counts
        stored = Job.fetch(job.id, connection=app.redis)
        assert stored.func_name == "app.moved_settings.get_setting"
        assert stored.args == ("PLEX_TRASH_LIMIT",)
        assert stored.meta["func"] == "app.moved_settings.get_setting"
        assert stored.get_status().value == "queued"

        # A job and a record with an other name stay as they are.

        assert Job.fetch(other.id, connection=app.redis).func_name == (
            "app.frames.refresh_frame_pool_task"
        )
        assert app.redis.hgetall(script.LATEST_KEY) == {
            b"app.moved_settings.get_setting|0 5 * * *": b"2026-11-17T05:00:00",
            b"app.frames.refresh_frame_pool_task|30 6 * * *": b"kept",
        }

        # A second run finds nothing.

        assert script.rename_stored_jobs(app.redis, pairs, apply=True) == {}


def test_the_script_names_the_record_of_the_scheduler():
    """scheduler.py is a program, not a module. The script repeats its key."""

    source = (ROOT / "scheduler.py").read_text()
    assert re.search(r'^LATEST_KEY = "([^"]+)"$', source, re.M).group(1) == (
        _script().LATEST_KEY
    )
