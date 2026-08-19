"""Per-file pipeline trails (#18): the stage registry and extractors,
the queue-side hooks, in-place lifecycle updates, and the queue page's
payload and markup."""

from datetime import timedelta

from tests.factories import make_movie, make_movie_file


def test_enqueue_leaves_a_queued_trail_entry(app):
    """Enqueuing a pipeline task through the app's TrackedQueue writes
    the file's trail: stage from the registry, basename from the path
    argument, status queued."""

    from app.pipeline import pipeline_trails

    with app.app_context():
        app.import_queue.enqueue(
            "app.videos.localization_task",
            args=("/import/Trail Film (2020) - [DVD].mkv",),
            description="'Trail Film (2020) - [DVD].mkv'",
        )

    trails = pipeline_trails(app.redis)
    assert len(trails) == 1
    assert trails[0]["basename"] == "Trail Film (2020) - [DVD].mkv"
    assert len(trails[0]["entries"]) == 1
    assert trails[0]["entries"][0]["stage"] == "Localizing"
    assert trails[0]["entries"][0]["status"] == "queued"


def test_deferred_retry_records_a_scheduled_entry(app):
    """enqueue_in — the pipeline's deferred-retry pattern — surfaces
    as a scheduled ("waiting to retry") entry."""

    from app.pipeline import pipeline_trails

    with app.app_context():
        app.import_queue.enqueue_in(
            timedelta(minutes=5),
            "app.videos.localization_task",
            args=("/import/Trail Deferred (2021) - [DVD].mkv",),
        )

    trails = pipeline_trails(app.redis)
    assert trails[0]["basename"] == "Trail Deferred (2021) - [DVD].mkv"
    assert trails[0]["entries"][0]["status"] == "scheduled"


def test_file_id_stages_look_the_basename_up(app):
    """file_id-keyed tasks (upload, transcode, remuxes) resolve their
    basename through the File record — inside the caller's own app
    context, never the get_app() singleton."""

    from app import db
    from app.pipeline import pipeline_trails

    with app.app_context():
        movie = make_movie("Trail Upload", 1999)
        file = make_movie_file(movie, "Bluray-1080p")
        db.session.commit()
        file_id, basename = file.id, file.basename

        app.file_queue.enqueue(
            "app.videos.upload_task",
            args=(file_id,),
            description=f"'{basename}'",
        )

    trails = pipeline_trails(app.redis)
    assert trails[0]["basename"] == basename
    assert trails[0]["entries"][0]["stage"] == "Archiving to S3"


def test_lifecycle_updates_one_entry_in_place(app):
    """A job moving queued → started → done is ONE trail line whose
    status advances — the worker hooks call the same recorder."""

    from app.pipeline import pipeline_trails, record_job_event

    with app.app_context():
        job = app.import_queue.enqueue(
            "app.videos.localization_task",
            args=("/import/Trail Lifecycle (2022) - [DVD].mkv",),
        )

    record_job_event(app.redis, job, "started")
    record_job_event(app.redis, job, "done")

    trails = pipeline_trails(app.redis)
    assert len(trails[0]["entries"]) == 1
    assert trails[0]["entries"][0]["status"] == "done"


def test_a_retry_after_failure_appends_a_fresh_entry(app):
    """A failure stays visible: the retry is a new job id, so it adds
    a second entry for the same stage instead of erasing the first."""

    from app.pipeline import pipeline_trails, record_job_event

    with app.app_context():
        first = app.import_queue.enqueue(
            "app.videos.localization_task",
            args=("/import/Trail Retry (2023) - [DVD].mkv",),
        )
        record_job_event(app.redis, first, "started")
        record_job_event(app.redis, first, "failed")
        app.import_queue.enqueue_in(
            timedelta(minutes=10),
            "app.videos.localization_task",
            args=("/import/Trail Retry (2023) - [DVD].mkv",),
        )

    entries = pipeline_trails(app.redis)[0]["entries"]
    assert [entry["status"] for entry in entries] == ["failed", "scheduled"]
    assert entries[0]["stage"] == entries[1]["stage"] == "Localizing"


def test_non_pipeline_tasks_leave_no_trail(app):
    """Tasks outside the stage registry — refreshes, sweeps — record
    nothing."""

    from app.pipeline import pipeline_trails

    with app.app_context():
        app.sql_queue.enqueue(
            "app.videos.sync_aws_s3_storage_task",
            description="Syncing files with AWS S3 storage",
        )

    assert pipeline_trails(app.redis) == []


def test_queue_details_payload_carries_the_trails(app, admin_client):
    """The 5-second poll's payload gains files, newest first."""

    with app.app_context():
        app.import_queue.enqueue(
            "app.videos.localization_task",
            args=("/import/Trail Payload (2024) - [DVD].mkv",),
        )

    payload = admin_client.get("/api/queue-details").get_json()
    assert payload["files"][0]["basename"] == "Trail Payload (2024) - [DVD].mkv"
    assert payload["files"][0]["entries"][0]["stage"] == "Localizing"


def test_queue_page_renders_the_files_section(admin_client):
    """The queue page carries the section the poll fills."""

    page = admin_client.get("/queue").get_data(as_text=True)
    assert 'id="pipeline-files-section"' in page
    assert 'id="pipeline-files"' in page
    assert "Files in the pipeline" in page
