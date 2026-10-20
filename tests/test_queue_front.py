"""Test the move of a waiting job to the front of its queue (#236).

This covers the atomic move, the order of the queue page, the admin
route, and the button on the page."""

from tests.conftest import page_csrf_token


def _enqueue(app, title, queue=None):
    """Enqueue a localization job for a film and return the job."""

    with app.app_context():
        return (queue or app.import_queue).enqueue(
            "app.videos.localization_task",
            args=(f"/import/{title} - [DVD].mkv",),
            description=f"'{title} - [DVD].mkv'",
        )


def test_move_to_front_puts_the_job_at_the_head_of_its_queue(app):
    """Move the job to the head of the rq list. Keep the order of the others."""

    from app.pipeline import FRONT_KEY, move_to_front

    first = _enqueue(app, "Front One (2001)")
    second = _enqueue(app, "Front Two (2002)")
    third = _enqueue(app, "Front Three (2003)")

    assert move_to_front(app.import_queue, third.id) == 2
    assert app.import_queue.job_ids == [third.id, first.id, second.id]
    assert app.redis.zscore(FRONT_KEY, third.id) is not None

    # A job that is already at the head stays there. Its mark is new
    assert move_to_front(app.import_queue, third.id) == 0
    assert app.import_queue.job_ids == [third.id, first.id, second.id]


def test_move_to_front_of_a_job_that_does_not_wait_changes_nothing(app):
    """Return None for a job that is not in the queue. Write no mark."""

    from app.pipeline import FRONT_KEY, move_to_front

    first = _enqueue(app, "Still Here (2001)")

    assert move_to_front(app.import_queue, "not-a-job") is None
    assert app.import_queue.job_ids == [first.id]
    assert app.redis.zcard(FRONT_KEY) == 0


def test_a_worker_clears_the_front_mark_when_it_starts_the_job(app):
    """Remove the mark of a moved job when a PipelineWorker starts it."""

    from app.pipeline import FRONT_KEY, PipelineWorker, move_to_front

    with app.app_context():
        job = app.import_queue.enqueue("os.getpid")
    move_to_front(app.import_queue, job.id)
    assert app.redis.zscore(FRONT_KEY, job.id) is not None

    PipelineWorker([app.import_queue], connection=app.redis).work(burst=True)

    assert app.redis.zscore(FRONT_KEY, job.id) is None


def test_the_queue_page_lists_a_moved_job_first(app, admin_client):
    """List the moved jobs before the other waiting jobs, newest move first."""

    from app.pipeline import move_to_front

    first = _enqueue(app, "Order One (2001)")
    second = _enqueue(app, "Order Two (2002)")
    third = _enqueue(app, "Order Three (2003)")
    other = _enqueue(app, "Order Transcode (2004)", queue=app.transcode_queue)

    payload = admin_client.get("/api/queue-details").get_json()
    assert [task["id"] for task in payload["all"]] == [
        first.id,
        second.id,
        third.id,
        other.id,
    ]

    move_to_front(app.import_queue, second.id)
    move_to_front(app.transcode_queue, other.id)

    payload = admin_client.get("/api/queue-details").get_json()
    assert [task["id"] for task in payload["all"]] == [
        other.id,
        second.id,
        first.id,
        third.id,
    ]
    assert [task["position"] for task in payload["all"]] == [1, 2, 3, 4]
    assert [task["fronted"] for task in payload["all"]] == [True, True, False, False]


def test_an_admin_moves_a_job_to_the_front_from_the_queue_page(app, admin_client):
    """Move the job through the route. Tell the admin what moved."""

    first = _enqueue(app, "Route One (2001)")
    second = _enqueue(app, "Route Two (2002)")

    response = admin_client.post(
        "/queue/front",
        data={
            "csrf_token": page_csrf_token(admin_client, "/queue"),
            "job_id": second.id,
        },
        follow_redirects=True,
    )

    page = response.get_data(as_text=True)
    assert "Moved &#39;Route Two (2002) - [DVD].mkv&#39; to the front" in page
    assert "of the import queue." in page
    assert app.import_queue.job_ids == [second.id, first.id]


def test_the_route_refuses_a_job_that_does_not_wait(app, admin_client):
    """Tell the admin that the job does not wait. Change no queue."""

    first = _enqueue(app, "Gone One (2001)")

    response = admin_client.post(
        "/queue/front",
        data={
            "csrf_token": page_csrf_token(admin_client, "/queue"),
            "job_id": "no-such-job",
        },
        follow_redirects=True,
    )

    assert "That job does not wait in a queue now." in response.get_data(as_text=True)
    assert app.import_queue.job_ids == [first.id]


def test_the_route_refuses_a_job_on_a_queue_that_the_page_does_not_list(
    app, admin_client
):
    """Move no job of a queue that the queue page does not list."""

    with app.app_context():
        job = app.maintenance_queue.enqueue("os.getpid")
        app.maintenance_queue.enqueue("os.getcwd")
        before = app.maintenance_queue.job_ids

    response = admin_client.post(
        "/queue/front",
        data={
            "csrf_token": page_csrf_token(admin_client),
            "job_id": job.id,
        },
        follow_redirects=True,
    )

    assert "That job does not wait in a queue now." in response.get_data(as_text=True)
    assert app.maintenance_queue.job_ids == before


def test_the_route_needs_a_csrf_token(app, admin_client):
    """Refuse a post without a csrf token. Change no queue."""

    first = _enqueue(app, "Token One (2001)")
    second = _enqueue(app, "Token Two (2002)")

    response = admin_client.post(
        "/queue/front", data={"job_id": second.id}, follow_redirects=True
    )

    assert "The form is not valid." in response.get_data(as_text=True)
    assert app.import_queue.job_ids == [first.id, second.id]


def test_a_member_cannot_move_a_job(app, user_client):
    """Send a member to the home page. Change no queue."""

    first = _enqueue(app, "Member One (2001)")
    second = _enqueue(app, "Member Two (2002)")

    response = user_client.post(
        "/queue/front",
        data={"csrf_token": page_csrf_token(user_client), "job_id": second.id},
    )

    assert response.status_code == 302
    assert app.import_queue.job_ids == [first.id, second.id]


def test_only_an_admin_sees_the_button_on_waiting_rows(app, admin_client, user_client):
    """Show the button on the waiting rows for an admin only.

    A deferred retry is not in line. Thus, it gets no button."""

    from datetime import timedelta

    _enqueue(app, "Button One (2001)")
    with app.app_context():
        app.import_queue.enqueue_in(
            timedelta(minutes=10),
            "app.videos.localization_task",
            args=("/import/Button Later (2001) - [DVD].mkv",),
            description="'Button Later (2001) - [DVD].mkv'",
        )

    admin_page = admin_client.get("/queue").get_data(as_text=True)
    assert admin_page.count('class="queue-front"') == 1
    assert 'aria-label="Move to front"' in admin_page
    assert "window.queueFront" in admin_page

    member_page = user_client.get("/queue").get_data(as_text=True)
    assert 'class="queue-front"' not in member_page
    assert "window.queueFront =" not in member_page
