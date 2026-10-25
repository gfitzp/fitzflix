"""Test the queue roster of app/queues.py and the task registry of app/tasks.py.

The roster spells each queue name 1 time. The registry gives each task
that Fitzflix enqueues by name 1 queue and 1 timeout. A test fails when
a producer names a task that the registry does not know. A test also
fails when the supervisor configuration names a queue that is not in
the roster.
"""

import ast
import importlib
import inspect
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent

TASK_NAME = re.compile(r"^app\.[a-z_]+\.[a-z_0-9]+$")

# These task parameters share a name with an option of enqueue_task. The
# producers pass them by position. Then the option does not take them.

POSITIONAL_ONLY_BY_CONVENTION = {"app.sonarr_push.adopt_episode_task": {"at_front"}}


def _source_files():
    return sorted((ROOT / "app").rglob("*.py")) + [
        ROOT / "scheduler.py",
        ROOT / "supervisor.py",
    ]


def _task_name_literals():
    """Return {string: [file:line]} for each dotted task path in the code."""

    found = {}
    for path in _source_files():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and TASK_NAME.match(node.value)
            ):
                found.setdefault(node.value, []).append(
                    f"{path.relative_to(ROOT)}:{node.lineno}"
                )
    return found


def test_the_supervisor_queues_are_in_the_roster():
    """Test that each queue argument of fitzflix_supervisor.ini is a roster queue."""

    import configparser

    from app.queues import QUEUE_NAMES

    ini = configparser.ConfigParser(interpolation=None)
    ini.read(ROOT / "fitzflix_supervisor.ini")
    named = set()
    for section in ini.sections():
        command = ini[section].get("command", "")
        if "supervisor.py" in command:
            named.update(command.split("supervisor.py", 1)[1].split())
    assert named
    assert named <= set(QUEUE_NAMES)

    # Each roster queue has a worker. A queue without one holds its jobs
    # forever.

    assert named == set(QUEUE_NAMES)


def test_the_program_roster_uses_only_roster_queues(app):
    """Test that PROGRAM_QUEUES and EXPECTED_WORKERS name only roster queues."""

    from app import maintenance
    from app.queues import QUEUE_NAMES, ROSTER

    for queues in maintenance.PROGRAM_QUEUES.values():
        assert set(queues) <= set(QUEUE_NAMES)
    assert list(maintenance.EXPECTED_WORKERS) == list(QUEUE_NAMES)
    assert all(count >= 1 for count in maintenance.EXPECTED_WORKERS.values())

    # The app has 1 attribute per roster entry, and its name is right.

    for spec in ROSTER:
        assert getattr(app, f"{spec.key}_queue").name == spec.name
    assert [queue.name for queue in app.queues.values()] == list(QUEUE_NAMES)


def test_no_module_spells_a_queue_list_by_hand():
    """Test that scheduler.py and the System page read the roster.

    Before the roster, 6 places spelled the 7 queue names. A new queue
    then reached some of them and not the others."""

    from app.queues import QUEUE_NAMES

    for path in (ROOT / "scheduler.py", ROOT / "app" / "main" / "admin.py"):
        text = path.read_text()
        for name in QUEUE_NAMES:
            assert f'"{name}"' not in text, (path.name, name)


def test_every_task_name_in_the_code_is_registered():
    """Test that each dotted task path in app/ and scheduler.py is a TASKS key.

    This covers each enqueue by string, each cron row, each retry, and
    the stage map of the pipeline trail. An unknown name fails here, not
    on a worker at night."""

    from app.tasks import TASKS

    found = _task_name_literals()
    missing = {name: sites for name, sites in found.items() if name not in TASKS}
    assert not missing, missing

    # The scan sees the producers. A stale spec with no producer is dead
    # weight.

    unused = set(TASKS) - set(found)
    assert not unused, sorted(unused)


def test_every_cron_row_is_registered(app):
    """Test that each cron row names a registered task and takes its spec."""

    from app import cron_table
    from app.queues import QUEUE_NAME
    from app.tasks import TASKS, task_timeout

    config = dict(
        app.config,
        AWS_SQS_URL="sqs",
        PLEX_URL="plex",
        PLEX_TOKEN="token",
        RADARR_URL="radarr",
        RADARR_API_KEY="key",
        DVR_TOKEN="dvr",
    )
    entries = cron_table(config)
    assert len(entries) >= 35
    for entry in entries:
        spec = TASKS[entry["func"]]
        assert entry["queue"] == QUEUE_NAME[spec.queue]
        assert entry["timeout"] == task_timeout(spec, config)
        assert entry["description"]


def test_every_registered_task_binds(app):
    """Test that each TASKS key resolves to a function and has a valid spec.

    This is the same check as test_every_scheduled_job_binds, applied to
    the whole registry. The task parameters must not collide with the
    keywords of enqueue_task, or a task argument would become an option."""

    from app.queues import QUEUE_NAME
    from app.tasks import TASKS

    reserved = {
        name
        for name, parameter in inspect.signature(
            importlib.import_module("app.tasks").enqueue_task
        ).parameters.items()
        if parameter.kind is inspect.Parameter.KEYWORD_ONLY
    }
    for name, spec in TASKS.items():
        module_name, func_name = name.rsplit(".", 1)
        func = getattr(importlib.import_module(module_name), func_name)
        assert callable(func), name
        assert spec.queue in QUEUE_NAME, name
        if isinstance(spec.timeout, str):
            assert isinstance(app.config[spec.timeout], int), name
        else:
            assert isinstance(spec.timeout, int) and spec.timeout > 0, name
        clash = reserved & set(inspect.signature(func).parameters)
        assert clash <= POSITIONAL_ONLY_BY_CONVENTION.get(name, set()), (name, clash)


def test_enqueue_task_fills_the_spec_and_takes_overrides(app):
    """Test the defaults from the spec and the explicit overrides."""

    from datetime import timedelta

    from rq.registry import ScheduledJobRegistry

    from app.tasks import enqueue_task

    with app.app_context():
        job = enqueue_task("app.videos.refresh_tmdb_info", "Movies", 1)
        try:
            assert job.origin == "fitzflix-user-request"
            assert job.timeout == app.config["SQL_TASK_TIMEOUT"]
            assert job.args == ("Movies", 1)
            assert job.kwargs == {}
        finally:
            job.delete()

        job = enqueue_task(
            "app.videos.refresh_tmdb_info",
            "Movies",
            2,
            queue="sql",
            at_front=True,
            description="'Jaws'",
            notify_if_missing=True,
        )
        try:
            assert job.origin == "fitzflix-sql"
            assert app.sql_queue.job_ids[0] == job.id
            assert job.description == "'Jaws'"
            assert job.kwargs == {"notify_if_missing": True}
        finally:
            job.delete()

        job = enqueue_task(
            "app.videos.sync_aws_s3_storage_task",
            delay=timedelta(minutes=5),
            job_id="test-registry-delay",
        )
        try:
            assert job.id == "test-registry-delay"
            assert job.timeout == 86400
            assert job.description == "Syncing files with AWS S3 storage"
            assert (
                job.id
                in ScheduledJobRegistry(queue=app.maintenance_queue).get_job_ids()
            )
        finally:
            ScheduledJobRegistry(queue=app.maintenance_queue).remove(job)
            job.delete()
