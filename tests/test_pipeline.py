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


def test_concurrent_stage_writes_do_not_erase_each_other(app, monkeypatch):
    """#76: the move job starts the instant localization enqueues it,
    so the file-operation worker's "started" stamp races the import
    worker's "done" stamp on the same trail. The loser of the old
    read-modify-write erased the winner (two files froze at
    "Localizing · running" overnight); the WATCHed write must retry
    and keep both."""

    import app.pipeline as pipeline
    from app.pipeline import pipeline_trails, record_job_event

    basename = "Trail Race (2026) - [DVD].mkv"
    with app.app_context():
        localize = app.import_queue.enqueue(
            "app.videos.localization_task",
            args=(f"/import/{basename}",),
        )
        move = app.file_queue.enqueue(
            "app.videos.move_localized_file",
            args=(f"/staging/.{basename}", {"basename": basename}, None, None),
        )
    record_job_event(app.redis, localize, "started")

    # Interleave deterministically: while the localization "done" write
    # sits between its read and its write, the move job's "started"
    # lands on the same trail — the WATCH must fire and re-read

    real_decode = pipeline._decode_trail
    interleaved = []

    def racing_decode(raw):
        if not interleaved:
            interleaved.append(True)
            record_job_event(app.redis, move, "started")
        return real_decode(raw)

    monkeypatch.setattr(pipeline, "_decode_trail", racing_decode)
    record_job_event(app.redis, localize, "done")

    statuses = {
        (entry["stage"], entry["status"])
        for entry in pipeline_trails(app.redis)[0]["entries"]
    }
    assert ("Localizing", "done") in statuses
    assert ("Moving into the library", "started") in statuses


def test_task_sub_stage_rides_the_jobs_trail(app, monkeypatch):
    """A phase inside one job — the staging copy — lands on the same
    trail as its own chip, keyed by the job's id but under its own
    stage label, and AHEAD of the job-level entry: the chips read in
    pipeline order (staging copy, then localizing), not in order of
    first stamp, which the job entry always wins by existing from
    enqueue time."""

    import rq

    from app.pipeline import pipeline_trails, record_job_event, record_task_stage

    with app.app_context():
        job = app.import_queue.enqueue(
            "app.videos.localization_task",
            args=("/import/Trail Staging (2026) - [DVD].mkv",),
        )
    record_job_event(app.redis, job, "started")

    monkeypatch.setattr(rq, "get_current_job", lambda: job)

    # Only one chip runs at a time: while the staging copy runs, the
    # job's own chip yields to it and reads "queued"

    record_task_stage("Copying to staging", "started")
    entries = pipeline_trails(app.redis)[0]["entries"]
    assert [(entry["stage"], entry["status"]) for entry in entries] == [
        ("Copying to staging", "started"),
        ("Localizing", "queued"),
    ]

    record_task_stage("Copying to staging", "done")
    entries = pipeline_trails(app.redis)[0]["entries"]
    assert [(entry["stage"], entry["status"]) for entry in entries] == [
        ("Copying to staging", "done"),
        ("Localizing", "started"),
    ]

    # The in-place update must not have moved it: finishing the job
    # keeps the sub-stage ahead of its chip

    record_job_event(app.redis, job, "done")
    entries = pipeline_trails(app.redis)[0]["entries"]
    assert [(entry["stage"], entry["status"]) for entry in entries] == [
        ("Copying to staging", "done"),
        ("Localizing", "done"),
    ]


def test_a_mid_flight_rename_merges_the_trail(app):
    """The parse can rename a file mid-flight — the title canonicalized
    against an existing series, or a container conversion — and the
    trail is keyed by basename, so localization merges it under the new
    name before enqueueing the move. One file stays ONE trail (the
    Futurama S11 imports split into two that overwrote each other's
    chips on the File Activity card, Aug 2026), and the stamps that
    arrive under the old name after the rename — the localization
    worker's own "done" — follow the alias onto the merged trail."""

    from app.pipeline import (
        first_run,
        migrate_trail,
        pipeline_trails,
        record_job_event,
    )

    old = "Trail Rename (1999) - S01E01 - [WEBDL-1080p].mkv"
    new = "Trail Rename - S01E01 - [WEBDL-1080p].mkv"

    with app.app_context():
        localize = app.import_queue.enqueue(
            "app.videos.localization_task", args=(f"/import/{old}",)
        )
        record_job_event(app.redis, localize, "started")

        migrate_trail(app.redis, old, new)

        app.file_queue.enqueue(
            "app.videos.move_localized_file",
            args=(f"/staging/.{new}", {"basename": new}, None, None),
        )

    record_job_event(app.redis, localize, "done")

    trails = pipeline_trails(app.redis)
    assert len(trails) == 1
    assert trails[0]["basename"] == new
    assert [(entry["stage"], entry["status"]) for entry in trails[0]["entries"]] == [
        ("Localizing", "done"),
        ("Moving into the library", "queued"),
    ]

    # The running banners' sort anchor — stamped under the old name at
    # "started" — must survive the rename too

    assert first_run(app.redis, localize) is not None


def test_a_rename_with_no_prior_trail_just_redirects(app):
    """Renaming a file whose trail never materialized (expired, or the
    hooks failed) leaves only the alias — no empty card — and later
    writes under the old name land on the new trail."""

    from app.pipeline import migrate_trail, pipeline_trails

    old = "Trail Ghost (1999) - S01E01 - [DVD].mkv"
    new = "Trail Ghost - S01E01 - [DVD].mkv"
    migrate_trail(app.redis, old, new)
    assert pipeline_trails(app.redis) == []

    with app.app_context():
        app.import_queue.enqueue(
            "app.videos.localization_task", args=(f"/import/{old}",)
        )

    trails = pipeline_trails(app.redis)
    assert len(trails) == 1
    assert trails[0]["basename"] == new


def test_task_sub_stage_without_a_job_is_a_noop(app):
    """Direct task calls (tests, shells) have no current job; the
    sub-stage emitter must record nothing rather than guess."""

    from app.pipeline import pipeline_trails, record_task_stage

    record_task_stage("Copying to staging", "started")
    assert pipeline_trails(app.redis) == []


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


def test_queue_details_files_limit_is_adjustable(app, admin_client):
    """The pipeline page asks the shared poll for more than the queue
    page's newest 25 via ?files=… (#76); the value is clamped so a
    hand-typed query can't ask Redis for the moon."""

    with app.app_context():
        for index in range(3):
            app.import_queue.enqueue(
                "app.videos.localization_task",
                args=(f"/import/Trail Limit {index} (2024) - [DVD].mkv",),
            )

    limited = admin_client.get("/api/queue-details?files=2").get_json()
    assert len(limited["files"]) == 2

    clamped = admin_client.get("/api/queue-details?files=99999").get_json()
    assert len(clamped["files"]) == 3

    nonsense = admin_client.get("/api/queue-details?files=-5").get_json()
    assert len(nonsense["files"]) == 1


def test_pipeline_trails_render_on_the_file_activity_dashboard(admin_client):
    """The trails live on the File Activity dashboard since the pages
    merged (Glenn's call, Aug 2026), linked from Library Maintenance;
    the queue page still doesn't carry the section, and the old
    dedicated page is gone."""

    page = admin_client.get("/file-activity").get_data(as_text=True)
    assert 'id="pipeline-files-section"' in page
    assert 'id="pipeline-files"' in page
    assert "window.pipelineTrailLimit = 100" in page

    queue_page = admin_client.get("/queue").get_data(as_text=True)
    assert 'id="pipeline-files-section"' not in queue_page
    assert "pipelineTrailLimit = " not in queue_page

    assert admin_client.get("/maintenance/pipeline").status_code == 404

    maintenance = admin_client.get("/maintenance").get_data(as_text=True)
    assert "/file-activity" in maintenance
    assert "View file activity" in maintenance


def test_running_banners_hold_first_run_order(app, admin_client):
    """The running list sorts by when each FILE first began running,
    not by the current job's own start — a file whose work hops from
    the import queue to the file-operation queue (iterated last) keeps
    its place ahead of a file that started after it (Glenn's original
    #18 ask)."""

    import time

    from rq.job import Job

    from app.pipeline import FILE_KEY, _digest

    with app.app_context():
        # File A began running at 10:00 and its work has moved on to
        # the file-operation queue; file B began at 10:05 and is still
        # localizing on the import queue, which the payload iterates
        # FIRST

        app.redis.hset(
            FILE_KEY.format(digest=_digest("Order A (2020) - [DVD].mkv")),
            "first_run",
            "2026-01-01 10:00:00",
        )
        app.redis.hset(
            FILE_KEY.format(digest=_digest("Order B (2021) - [DVD].mkv")),
            "first_run",
            "2026-01-01 10:05:00",
        )

        job_a = Job.create(
            "app.videos.move_localized_file",
            args=(
                "/staging/.Order A (2020) - [DVD].mkv",
                {"basename": "Order A (2020) - [DVD].mkv"},
                None,
                None,
            ),
            id="order-a-move",
            origin="fitzflix-file-operation",
            connection=app.redis,
        )
        job_a.save()
        # rq 2's StartedJobRegistry.add is NotImplemented (workers add
        # executions), so seed the raw wip zset: {job_id}:{execution_id}
        app.redis.zadd(
            "rq:wip:fitzflix-file-operation", {"order-a-move:test": time.time() + 600}
        )

        job_b = Job.create(
            "app.videos.localization_task",
            args=("/import/Order B (2021) - [DVD].mkv",),
            id="order-b-localize",
            origin="fitzflix-import",
            connection=app.redis,
        )
        job_b.save()
        app.redis.zadd(
            "rq:wip:fitzflix-import", {"order-b-localize:test": time.time() + 600}
        )

    payload = admin_client.get("/api/queue-details").get_json()
    order = [item["id"] for item in payload["running"]]
    assert order == ["order-a-move", "order-b-localize"]
    assert payload["running"][0]["first_run"] == "2026-01-01 10:00:00"
