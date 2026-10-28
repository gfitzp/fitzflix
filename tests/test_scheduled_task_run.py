"""Test the command column and the Run now button of the Scheduled tasks table.

The System page shows the CLI command of each scheduled task, when the
task has one. The Run now button queues the task at once. It never
queues a task that waits or runs already, and it never queues a task
that is outside the cron table.
"""

from tests.conftest import page_csrf_token

IMPORT_SWEEP = "app.videos.manual_import_task"
HOURLY = "0 * * * *"


def register_cron_job(app, func_name, cron=HOURLY):
    """Register 1 cron job the way that scheduler.py does.

    The System page reads the jobs of each live CronScheduler from
    Redis. The test suite runs no scheduler process. Thus, the test
    publishes 1 job itself."""

    import importlib

    from rq.cron import CronScheduler

    from app.tasks import TASKS

    module_name, name = func_name.rsplit(".", 1)
    func = getattr(importlib.import_module(module_name), name)
    scheduler = CronScheduler(connection=app.redis, name="test")
    scheduler.register(
        func,
        queue_name="fitzflix-maintenance",
        cron=cron,
        job_timeout=60,
        meta={
            "cron_string": cron,
            "description": TASKS[func_name].description,
            "func": func_name,
        },
        result_ttl=60,
    )
    scheduler.register_birth()
    scheduler.save_jobs_data()
    return scheduler


def run_now(client, func_name):
    """Post the Run now button of a task row and follow the redirect."""

    token = page_csrf_token(client, "/system")
    return client.post(
        "/system",
        data={"csrf_token": token, "task_func": func_name, "run_submit": "Run now"},
        follow_redirects=True,
    )


def test_scheduled_task_rows_show_the_command_and_a_run_button(app, admin_client):
    """Test that a task row shows its CLI command and a Run now button.

    The command comes from the task registry of the web process, not
    from the job data of the scheduler. Thus, a new command shows
    without a restart of the scheduler."""

    with app.app_context():
        register_cron_job(app, IMPORT_SWEEP)

    body = admin_client.get("/system/metrics").get_data(as_text=True)
    assert '<th scope="col">Command</th>' in body
    assert '<code class="small text-nowrap">flask scan</code>' in body
    assert f'name="task_func" type="hidden" value="{IMPORT_SWEEP}"' in body
    assert 'value="Run now"' in body
    assert 'action="/system"' in body


def test_a_task_with_no_command_shows_a_dash(app, admin_client):
    with app.app_context():
        register_cron_job(app, "app.maintenance.rotate_logs", "0 0 * * *")

    body = admin_client.get("/system/metrics").get_data(as_text=True)
    assert "Rotating application logs" in body
    assert "<code" not in body.split("Rotating application logs")[1].split("</tr>")[0]


def test_run_now_queues_the_task_once(app, admin_client):
    """Test that Run now queues the task on its own queue, 1 time.

    A second post while the first job waits queues nothing. The
    import sweep and the S3 sync do their whole work on each run.
    Thus, a second copy behind the first one is only a repeat."""

    with app.app_context():
        register_cron_job(app, IMPORT_SWEEP)

    response = run_now(admin_client, IMPORT_SWEEP)
    assert response.status_code == 200
    assert "Queued &#39;Scanning import directory for files&#39;" in response.get_data(
        as_text=True
    )

    with app.app_context():
        jobs = app.maintenance_queue.jobs
        assert [job.func_name for job in jobs] == [IMPORT_SWEEP]
        assert jobs[0].timeout == 3600
        assert jobs[0].description == "Scanning import directory for files"

    response = run_now(admin_client, IMPORT_SWEEP)
    assert "waits or runs already" in response.get_data(as_text=True)
    with app.app_context():
        assert len(app.maintenance_queue) == 1


def test_run_now_refuses_a_task_outside_the_cron_table(app, admin_client):
    """Test that only a task of the cron table can be queued from the page.

    The hidden field names a task. A hand-made post must not queue a
    pipeline task, for example a localization with made-up arguments."""

    response = run_now(admin_client, "app.videos.localization_task")
    assert "not a scheduled task" in response.get_data(as_text=True)
    with app.app_context():
        assert len(app.import_queue) == 0
        assert len(app.maintenance_queue) == 0


def test_run_now_requires_admin(app, user_client):
    token = page_csrf_token(user_client)
    response = user_client.post(
        "/system",
        data={"csrf_token": token, "task_func": IMPORT_SWEEP, "run_submit": "Run now"},
    )
    assert response.status_code == 302
    with app.app_context():
        assert len(app.maintenance_queue) == 0


def enqueue_like_the_scheduler(app, func_name, delay=None):
    """Queue the function object with no meta, as an old cron job does (#456).

    rq then names the job after the module that defines the function.
    For the import sweep, that is app.importing, not app.videos."""

    import importlib
    from datetime import timedelta

    module_name, name = func_name.rsplit(".", 1)
    func = getattr(importlib.import_module(module_name), name)
    if delay is None:
        return app.maintenance_queue.enqueue(func)
    return app.maintenance_queue.enqueue_in(timedelta(minutes=delay), func)


def test_a_cron_job_of_the_task_blocks_run_now(app, admin_client):
    """The scheduler queues the callable. Its func_name is the defining module."""

    with app.app_context():
        register_cron_job(app, IMPORT_SWEEP)
        job = enqueue_like_the_scheduler(app, IMPORT_SWEEP)
        assert job.func_name == "app.importing.manual_import_task"

    response = run_now(admin_client, IMPORT_SWEEP)
    assert "waits or runs already" in response.get_data(as_text=True)
    with app.app_context():
        assert len(app.maintenance_queue) == 1


def test_a_scheduled_retry_of_the_task_blocks_run_now(app, admin_client):
    """A defer or a retry waits in the ScheduledJobRegistry. It counts as live."""

    with app.app_context():
        register_cron_job(app, IMPORT_SWEEP)
        enqueue_like_the_scheduler(app, IMPORT_SWEEP, delay=5)
        assert len(app.maintenance_queue) == 0

    response = run_now(admin_client, IMPORT_SWEEP)
    assert "waits or runs already" in response.get_data(as_text=True)
    with app.app_context():
        assert len(app.maintenance_queue) == 0


def test_the_run_now_gate_stops_a_second_post_at_the_same_time(app):
    """2 posts to 2 web workers can both see an empty queue. The gate stops one."""

    from app.tasks import finish_task_run, start_task_run

    with app.app_context():
        assert start_task_run(IMPORT_SWEEP) is True
        assert start_task_run(IMPORT_SWEEP) is False
        finish_task_run(IMPORT_SWEEP)
        assert start_task_run(IMPORT_SWEEP) is True
        finish_task_run(IMPORT_SWEEP)


def test_the_sync_row_has_no_run_now_button(app, admin_client):
    """The S3 sync needs the password and the bulk gate of the Maintenance page."""

    sync = "app.videos.sync_aws_s3_storage_task"
    with app.app_context():
        register_cron_job(app, sync, "0 3 * * 0")

    body = admin_client.get("/system/metrics").get_data(as_text=True)
    assert f'value="{sync}"' not in body
    assert "Maintenance page" in body

    response = run_now(admin_client, sync)
    assert "Maintenance page" in response.get_data(as_text=True)
    with app.app_context():
        assert len(app.maintenance_queue) == 0


def test_the_sync_button_sees_a_cron_sync(app, admin_client):
    """A sync that the scheduler queued keeps the Maintenance button out (#456)."""

    from tests.conftest import ADMIN_PASSWORD, csrf_token_from

    sync = "app.videos.sync_aws_s3_storage_task"
    with app.app_context():
        enqueue_like_the_scheduler(app, sync)

    page = admin_client.get("/maintenance").get_data(as_text=True)
    body = admin_client.post(
        "/maintenance",
        data={
            "csrf_token": csrf_token_from(page),
            "password": ADMIN_PASSWORD,
            "sync_submit": "1",
        },
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "already queued or running" in body
    with app.app_context():
        assert len(app.maintenance_queue) == 1
