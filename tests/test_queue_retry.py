"""Test the on-demand retry of a booked job (Queue page, 2026-10-30).

A deferred retry waits in the ScheduledJobRegistry for its time. The
Retry now button moves it into its queue at once. This covers the
claim, the due case, the admin route, and the button on the rows."""

from datetime import timedelta

from rq.registry import ScheduledJobRegistry

from tests.conftest import page_csrf_token


def _book(app, title, minutes=10, queue=None):
    """Book a deferred localization retry for a film and return the job."""

    with app.app_context():
        return (queue or app.import_queue).enqueue_in(
            timedelta(minutes=minutes),
            "app.videos.localization_task",
            args=(f"/import/{title} - [DVD].mkv",),
            description=f"'{title} - [DVD].mkv'",
        )


def _enqueue(app, title):
    """Enqueue a localization job for a film and return the job."""

    with app.app_context():
        return app.import_queue.enqueue(
            "app.videos.localization_task",
            args=(f"/import/{title} - [DVD].mkv",),
            description=f"'{title} - [DVD].mkv'",
        )


def test_retry_now_moves_the_booked_job_to_the_front_of_its_queue(app):
    """Take the job out of the registry. Put it at the head, with a front mark."""

    from app.pipeline import FRONT_KEY, retry_now

    first = _enqueue(app, "Retry One (2001)")
    booked = _book(app, "Retry Later (2002)")
    registry = ScheduledJobRegistry(queue=app.import_queue)
    assert booked.id in registry.get_job_ids()

    assert retry_now(app.import_queue, booked.id) == "moved"

    assert booked.id not in registry.get_job_ids()
    assert app.import_queue.job_ids == [booked.id, first.id]
    assert app.redis.zscore(FRONT_KEY, booked.id) is not None
    with app.app_context():
        assert booked.get_status(refresh=True) == "queued"

    # A second press finds no booked job
    assert retry_now(app.import_queue, booked.id) is None
    assert app.import_queue.job_ids == [booked.id, first.id]


def test_retry_now_leaves_a_due_job_to_the_scheduler(app):
    """Claim no job whose time came. The scheduler moves that one."""

    from app.pipeline import retry_now

    due = _book(app, "Retry Due (2001)", minutes=-1)
    registry = ScheduledJobRegistry(queue=app.import_queue)

    assert retry_now(app.import_queue, due.id) == "due"

    assert due.id in registry.get_job_ids()
    assert app.import_queue.job_ids == []


def test_retry_now_of_a_job_that_is_not_booked_changes_nothing(app):
    """Return None for a queued job and for an unknown id. Write no mark."""

    from app.pipeline import FRONT_KEY, retry_now

    first = _enqueue(app, "Retry Queued (2001)")

    assert retry_now(app.import_queue, first.id) is None
    assert retry_now(app.import_queue, "not-a-job") is None
    assert app.import_queue.job_ids == [first.id]
    assert app.redis.zcard(FRONT_KEY) == 0


def test_the_moved_retry_stamps_its_trail_as_queued(app, admin_client):
    """The chip of the file reads queued, not waiting to retry, after the move."""

    from app.pipeline import retry_now

    booked = _book(app, "Retry Chip (2001)")
    page = admin_client.get("/queue").get_data(as_text=True)
    assert "Localizing &middot; waiting to retry" in page

    retry_now(app.import_queue, booked.id)

    page = admin_client.get("/queue").get_data(as_text=True)
    assert "waiting to retry" not in page
    assert "Localizing &middot; queued" in page


def test_an_admin_retries_a_booked_job_from_the_queue_page(app, admin_client):
    """Move the job through the route. Tell the admin what moved."""

    first = _enqueue(app, "Route One (2001)")
    booked = _book(app, "Route Later (2002)")

    response = admin_client.post(
        "/queue/retry",
        data={
            "csrf_token": page_csrf_token(admin_client, "/queue"),
            "job_id": booked.id,
        },
        follow_redirects=True,
    )

    page = response.get_data(as_text=True)
    assert "Moved &#39;Route Later (2002) - [DVD].mkv&#39; to the front" in page
    assert "of the import queue for a retry now." in page
    assert app.import_queue.job_ids == [booked.id, first.id]


def test_the_route_tells_the_admin_when_the_retry_is_due(app, admin_client):
    """Leave a due job in the registry. Say that the scheduler moves it."""

    due = _book(app, "Route Due (2001)", minutes=-1)

    response = admin_client.post(
        "/queue/retry",
        data={"csrf_token": page_csrf_token(admin_client), "job_id": due.id},
        follow_redirects=True,
    )

    assert "That retry is due." in response.get_data(as_text=True)
    assert app.import_queue.job_ids == []
    assert due.id in ScheduledJobRegistry(queue=app.import_queue).get_job_ids()


def test_the_route_refuses_a_job_that_is_not_booked(app, admin_client):
    """Tell the admin that the job is not booked. Change no queue."""

    first = _enqueue(app, "Gone One (2001)")

    for job_id in ("no-such-job", first.id):
        response = admin_client.post(
            "/queue/retry",
            data={"csrf_token": page_csrf_token(admin_client), "job_id": job_id},
            follow_redirects=True,
        )
        assert "That job is not booked for a retry now." in response.get_data(
            as_text=True
        )
    assert app.import_queue.job_ids == [first.id]


def test_the_route_refuses_a_job_on_a_queue_that_the_page_does_not_list(
    app, admin_client
):
    """Move no booked job of a queue that the queue page does not list."""

    with app.app_context():
        job = app.maintenance_queue.enqueue_in(timedelta(minutes=10), "os.getpid")
    registry = ScheduledJobRegistry(queue=app.maintenance_queue)

    response = admin_client.post(
        "/queue/retry",
        data={"csrf_token": page_csrf_token(admin_client), "job_id": job.id},
        follow_redirects=True,
    )

    assert "That job is not booked for a retry now." in response.get_data(as_text=True)
    assert job.id in registry.get_job_ids()
    assert app.maintenance_queue.job_ids == []


def test_the_route_needs_a_csrf_token(app, admin_client):
    """Refuse a post without a csrf token. Change no queue."""

    booked = _book(app, "Token Later (2001)")

    response = admin_client.post(
        "/queue/retry", data={"job_id": booked.id}, follow_redirects=True
    )

    assert "The form is not valid." in response.get_data(as_text=True)
    assert app.import_queue.job_ids == []


def test_a_member_cannot_retry_a_job(app, user_client):
    """Send a member to the home page. Change no queue."""

    booked = _book(app, "Member Later (2001)")

    response = user_client.post(
        "/queue/retry",
        data={"csrf_token": page_csrf_token(user_client), "job_id": booked.id},
    )

    assert response.status_code == 302
    assert app.import_queue.job_ids == []
    assert booked.id in ScheduledJobRegistry(queue=app.import_queue).get_job_ids()


def test_only_an_admin_sees_the_button_on_booked_rows(app, admin_client, user_client):
    """Show the button on the booked rows for an admin only.

    A queued job gets no Retry now button."""

    _enqueue(app, "Button One (2001)")
    booked = _book(app, "Button Later (2002)")

    admin_page = admin_client.get("/queue").get_data(as_text=True)
    assert admin_page.count('class="queue-retry"') == 1
    assert 'aria-label="Retry now"' in admin_page
    assert f'name="job_id" value="{booked.id}"' in admin_page

    member_page = user_client.get("/queue").get_data(as_text=True)
    assert 'class="queue-retry"' not in member_page

    # The poll fetches the same partial (#639).

    admin_rows = admin_client.get("/queue/rows").get_data(as_text=True)
    assert admin_rows.count('class="queue-retry"') == 1
    member_rows = user_client.get("/queue/rows").get_data(as_text=True)
    assert 'class="queue-retry"' not in member_rows
