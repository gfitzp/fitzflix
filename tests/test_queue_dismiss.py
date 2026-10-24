"""Test the Dismiss button of the "no job in queue" rows of the Queue page.

This covers dismiss_trail, the admin route, and the button on the
page."""

from app.pipeline import ACTIVE_KEY, FILE_KEY, _digest, pipeline_trails
from tests.conftest import page_csrf_token


def _orphan_trail(app, title):
    """Make a trail whose job failed before its failure stamp.

    That is a "no job in queue" row, the same as on 2026-10-22, before
    the cleanup settles it."""

    with app.app_context():
        job = app.import_queue.enqueue(
            "app.videos.localization_task",
            args=(f"/import/{title} - [DVD].mkv",),
        )
    app.import_queue.remove(job)
    return job, _digest(f"{title} - [DVD].mkv")


def test_an_admin_dismisses_a_row_with_no_job(app, admin_client):
    job, digest = _orphan_trail(app, "Dismissed (2001)")
    app.redis.hset(f"rq:job:{job.id}", "status", "failed")
    assert [trail["digest"] for trail in pipeline_trails(app.redis)] == [digest]

    response = admin_client.post(
        "/queue/dismiss",
        data={"csrf_token": page_csrf_token(admin_client), "digest": digest},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert not app.redis.exists(FILE_KEY.format(digest=digest))
    assert app.redis.zscore(ACTIVE_KEY, digest) is None
    assert pipeline_trails(app.redis) == []


def test_a_row_with_a_live_job_stays(app, admin_client):
    """A queued job is work in line. Its trail stays."""

    with app.app_context():
        app.import_queue.enqueue(
            "app.videos.localization_task",
            args=("/import/Still Waiting (2002) - [DVD].mkv",),
        )
    digest = _digest("Still Waiting (2002) - [DVD].mkv")

    response = admin_client.post(
        "/queue/dismiss",
        data={"csrf_token": page_csrf_token(admin_client), "digest": digest},
        follow_redirects=True,
    )

    assert "That file has a job in a queue now." in response.get_data(as_text=True)
    assert app.redis.exists(FILE_KEY.format(digest=digest))


def test_the_dismiss_route_refuses_a_bad_digest_and_a_member(
    app, admin_client, user_client
):
    job, digest = _orphan_trail(app, "Guarded (2003)")
    app.redis.hset(f"rq:job:{job.id}", "status", "failed")

    response = admin_client.post(
        "/queue/dismiss",
        data={"csrf_token": page_csrf_token(admin_client), "digest": "x' OR 1"},
        follow_redirects=True,
    )
    assert "The form is not valid." in response.get_data(as_text=True)

    response = admin_client.post(
        "/queue/dismiss", data={"digest": digest}, follow_redirects=True
    )
    assert "The form is not valid." in response.get_data(as_text=True)

    response = user_client.post(
        "/queue/dismiss",
        data={"csrf_token": page_csrf_token(user_client), "digest": digest},
    )
    assert response.status_code == 302
    assert app.redis.exists(FILE_KEY.format(digest=digest))


def test_only_an_admin_sees_the_dismiss_button(app, admin_client, user_client):
    """A job that the queue lists has no button. A row with no job has
    one, for an admin only."""

    with app.app_context():
        job = app.import_queue.enqueue(
            "app.videos.localization_task",
            args=("/import/Button Row (2004) - [DVD].mkv",),
        )
    app.import_queue.remove(job)

    page = admin_client.get("/queue").get_data(as_text=True)
    assert page.count('class="queue-dismiss"') == 1
    assert _digest("Button Row (2004) - [DVD].mkv") in page
    assert "window.queueDismiss = {" in page

    page = user_client.get("/queue").get_data(as_text=True)
    assert 'class="queue-dismiss"' not in page
    assert "window.queueDismiss = {" not in page
