"""Test that the Atmos supplement waits for a pending TMDB refresh.

The import queues the TMDB refresh and the Atmos supplement at the same
time. On 2026-10-04, the supplement took the title lock of 'Jungle
Cruise (2021)' 15 ms after the import released it. The refresh then
waited for the whole MediaConvert run, and so did the match from the
review page. The supplement now defers itself while a refresh of its
movie is queued, running, or scheduled.

Each test imports inside the function. The task modules capture the
singleton of get_app() at import time.
"""

from datetime import timedelta

from app import db

from tests.factories import make_movie, make_movie_file


def _movie_with_file(title="Jungle Cruise", year=2021):
    movie = make_movie(title, year)
    file = make_movie_file(movie, "Bluray-1080p")
    db.session.commit()
    return movie, file


def test_pending_sees_a_queued_fetch(app):
    """Test that a queued fetch job on the request queue counts as pending."""

    from app.tmdb_refresh import tmdb_refresh_pending

    with app.app_context():
        movie, _ = _movie_with_file()
        assert tmdb_refresh_pending("Movies", movie.id) is False
        app.request_queue.enqueue(
            "app.videos.refresh_tmdb_info",
            args=("Movies", movie.id, None),
            kwargs={"notify_if_missing": True},
            description="Refreshing TMDB data for 'Jungle Cruise (2021)'",
        )
        assert tmdb_refresh_pending("Movies", movie.id) is True
        assert tmdb_refresh_pending("Movies", movie.id + 1) is False
        assert tmdb_refresh_pending("TV Shows", movie.id) is False


def test_pending_sees_a_scheduled_apply_retry(app):
    """Test that an apply retry in the scheduled registry counts as pending.

    The retry takes keyword arguments. The fetch takes positional ones."""

    from app.tmdb_refresh import tmdb_refresh_pending

    with app.app_context():
        movie, _ = _movie_with_file()
        app.sql_queue.enqueue_in(
            timedelta(minutes=5),
            "app.videos.apply_tmdb_refresh",
            library="Movies",
            id=movie.id,
            tmdb_id=451048,
            tmdb_payload=None,
            notify_if_missing=True,
            job_id=f"retry_apply_tmdb_refresh_Movies_{movie.id}",
            description="Updating 'Jungle Cruise (2021)' with TMDB data",
        )
        assert tmdb_refresh_pending("Movies", movie.id) is True


def test_atmos_task_defers_while_a_refresh_is_pending(app):
    """Test that the supplement task reschedules itself and takes no lock."""

    from rq.registry import ScheduledJobRegistry

    from app.atmos import atmos_supplement_task

    with app.app_context():
        movie, file = _movie_with_file()
        file_id = file.id
        resource = file.file_identifier()
        app.request_queue.enqueue(
            "app.videos.refresh_tmdb_info",
            args=("Movies", movie.id, None),
            description="Refreshing TMDB data for 'Jungle Cruise (2021)'",
        )

    assert atmos_supplement_task(file_id) is True

    with app.app_context():
        registry = ScheduledJobRegistry(queue=app.transcode_queue)
        job_ids = registry.get_job_ids()
        assert len(job_ids) == 1
        job = app.transcode_queue.fetch_job(job_ids[0])
        assert job.func_name == "app.atmos.atmos_supplement_task"
        assert job.args == (file_id,)
        delay = registry.get_scheduled_time(job_ids[0]) - job.created_at.replace(
            tzinfo=registry.get_scheduled_time(job_ids[0]).tzinfo
        )
        assert timedelta(seconds=55) <= delay <= timedelta(minutes=3, seconds=5)

        # The task took no title lock. A fresh lock on the title succeeds.

        lock = app.lock_manager.lock(resource, 1000)
        assert lock
        app.lock_manager.unlock(lock)


def test_movie_page_shows_a_deferred_refresh(app, admin_client):
    """Test that the movie page names a deferred refresh and its next attempt.

    Without the note, a match from the form looked like it did nothing
    while the apply waited for the Atmos supplement to release the lock."""

    with app.app_context():
        movie, _ = _movie_with_file()
        movie_id = movie.id

    page = admin_client.get(f"/movie/{movie_id}").get_data(as_text=True)
    assert "A TMDB refresh is waiting for another task" not in page
    assert "A TMDB refresh is queued or running" not in page

    with app.app_context():
        app.sql_queue.enqueue_in(
            timedelta(minutes=12),
            "app.videos.apply_tmdb_refresh",
            library="Movies",
            id=movie_id,
            tmdb_id=451048,
            tmdb_payload=None,
            notify_if_missing=True,
            job_id=f"retry_apply_tmdb_refresh_Movies_{movie_id}",
            description="Updating 'Jungle Cruise (2021)' with TMDB data",
        )

    page = admin_client.get(f"/movie/{movie_id}").get_data(as_text=True)
    assert "A TMDB refresh is waiting for another task that holds this title" in page
    assert "Next attempt" in page


def test_movie_page_shows_a_queued_refresh(app, admin_client):
    """Test that a queued fetch shows the lighter in-progress note."""

    with app.app_context():
        movie, _ = _movie_with_file()
        movie_id = movie.id
        app.request_queue.enqueue(
            "app.videos.refresh_tmdb_info",
            args=("Movies", movie_id, 451048),
            description="Refreshing TMDB data for 'Jungle Cruise (2021)'",
        )

    page = admin_client.get(f"/movie/{movie_id}").get_data(as_text=True)
    assert "A TMDB refresh is queued or running" in page
    assert "waiting for another task" not in page
