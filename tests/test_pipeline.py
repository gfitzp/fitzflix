"""Test the per-file pipeline trails.

This covers the stage registry and the extractors, the queue-side
hooks, the in-place lifecycle updates, and the payload and markup of
the queue page."""

from datetime import timedelta

from tests.factories import make_movie, make_movie_file


def test_enqueue_leaves_a_queued_trail_entry(app):
    """Write the trail of the file when the app enqueues a pipeline task.

    The TrackedQueue of the app writes the stage from the registry, the
    basename from the path argument, and the status queued."""

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
    """Show enqueue_in as a scheduled ("waiting to retry") entry.

    enqueue_in is the deferred-retry pattern of the pipeline."""

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
    """Resolve the basename of a file_id-keyed task through the File record.

    These tasks are upload, transcode, and the remuxes. The lookup runs
    in the app context of the caller, never in the get_app() singleton."""

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


def test_a_late_queued_stamp_never_unwinds_a_finished_chip(app):
    """Drop a late "queued" stamp instead of freezing the chip at queued.

    The enqueue hook writes after the job is already claimable. Thus, a
    job that completes in milliseconds can beat its own "queued" stamp.
    Example: the deferred re-archive that skips a superseded key."""

    from app.pipeline import pipeline_trails, record_job_event

    with app.app_context():
        job = app.import_queue.enqueue(
            "app.videos.localization_task",
            args=("/import/Trail Late Stamp (2023) - [DVD].mkv",),
        )

    record_job_event(app.redis, job, "started")
    record_job_event(app.redis, job, "done")

    # The stamp from the enqueue side arrives after the worker has finished.

    record_job_event(app.redis, job, "queued")

    trails = pipeline_trails(app.redis)
    entries = [entry for entry in trails[0]["entries"] if entry["job"] == job.id]
    assert len(entries) == 1
    assert entries[0]["status"] == "done"


def test_lifecycle_updates_one_entry_in_place(app):
    """Keep 1 trail line for a job that moves from queued to started to done.

    Only the status of the line advances. The worker hooks call the same
    recorder."""

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
    """Keep a failure visible.

    The retry is a new job id. Thus, it adds a second entry for the same
    stage. It does not erase the first entry."""

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
    """Retry the WATCHed write and keep both stamps of the race.

    The move job starts the instant that localization enqueues it. Thus,
    the "started" stamp of the file-operation worker races the "done"
    stamp of the import worker on the same trail. In the old
    read-modify-write, the loser erased the winner. 2 files froze at
    "Localizing · running" overnight."""

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

    # Interleave the writes deterministically. The localization "done"
    # write is between its read and its write. At that moment, the
    # "started" of the move job goes into the same trail. The WATCH must
    # run and read again.

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
    """Put a phase inside 1 job on the same trail as its own chip.

    The staging copy is such a phase. Its key is the id of the job, but
    it has its own stage label. It goes AHEAD of the job-level entry.
    The chips read in pipeline order: staging copy, then localizing.
    They do not read in order of the first stamp. The job entry always
    wins that order, because it exists from the enqueue time."""

    import rq

    from app.pipeline import pipeline_trails, record_job_event, record_task_stage

    with app.app_context():
        job = app.import_queue.enqueue(
            "app.videos.localization_task",
            args=("/import/Trail Staging (2026) - [DVD].mkv",),
        )
    record_job_event(app.redis, job, "started")

    monkeypatch.setattr(rq, "get_current_job", lambda: job)

    # Only 1 chip runs at a time. While the staging copy runs, the chip
    # of the job yields to it and reads "queued".

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

    # The in-place update must not move it. When the job finishes, the
    # sub-stage stays ahead of its chip.

    record_job_event(app.redis, job, "done")
    entries = pipeline_trails(app.redis)[0]["entries"]
    assert [(entry["stage"], entry["status"]) for entry in entries] == [
        ("Copying to staging", "done"),
        ("Localizing", "done"),
    ]


def test_a_mid_flight_rename_merges_the_trail(app):
    """Merge the trail under the new name when the parse renames a file.

    The parse can rename a file in flight. Examples: the title becomes
    canonical against an existing series, or a container conversion.
    The key of the trail is the basename. Thus, localization merges the
    trail under the new name before it enqueues the move. 1 file stays
    ONE trail. In 2026-08, the Futurama S11 imports split into 2 trails
    that overwrote each other's chips on the File Activity card. A stamp
    that arrives under the old name after the rename follows the alias
    onto the merged trail. Example: the "done" of the localization
    worker."""

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

    # The sort anchor of the running banners must also survive the
    # rename. The "started" stamp wrote it under the old name.

    assert first_run(app.redis, localize) is not None


def test_a_rename_with_no_prior_trail_just_redirects(app):
    """Leave only the alias when a file with no trail is renamed.

    The trail can be missing because it expired or because the hooks
    failed. There is no empty card. Later writes under the old name go
    into the new trail."""

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
    """Record nothing from the sub-stage emitter when there is no current job.

    A direct task call (from a test or a shell) has no current job. The
    emitter must not guess."""

    from app.pipeline import pipeline_trails, record_task_stage

    record_task_stage("Copying to staging", "started")
    assert pipeline_trails(app.redis) == []


def test_non_pipeline_tasks_leave_no_trail(app):
    """Record nothing for a task outside the stage registry.

    Examples: the refreshes and the sweeps."""

    from app.pipeline import pipeline_trails

    with app.app_context():
        app.sql_queue.enqueue(
            "app.videos.sync_aws_s3_storage_task",
            description="Syncing files with AWS S3 storage",
        )

    assert pipeline_trails(app.redis) == []


def test_queue_details_payload_carries_the_trails(app, admin_client):
    """Add files to the payload of the 5-second poll, newest first."""

    with app.app_context():
        app.import_queue.enqueue(
            "app.videos.localization_task",
            args=("/import/Trail Payload (2024) - [DVD].mkv",),
        )

    payload = admin_client.get("/api/queue-details").get_json()
    assert payload["files"][0]["basename"] == "Trail Payload (2024) - [DVD].mkv"
    assert payload["files"][0]["entries"][0]["stage"] == "Localizing"


def test_trail_entries_carry_their_job_ids(app, admin_client):
    """Name the rq job that stamped each entry.

    The rows of the queue page find the chips of their file by that id.
    They never match the description against a basename."""

    with app.app_context():
        job = app.import_queue.enqueue(
            "app.videos.localization_task",
            args=("/import/Trail Job Id (2024) - [DVD].mkv",),
        )

    payload = admin_client.get("/api/queue-details").get_json()
    entry = payload["files"][0]["entries"][0]
    assert entry["job"] == job.id
    assert [task["id"] for task in payload["all"]] == [job.id]
    assert payload["all"][0]["position"] == 1


def test_deferred_retries_list_on_the_queue_page(app, admin_client):
    """List a deferred retry after the live jobs, with its return time.

    A retry booked for later is in the ScheduledJobRegistry, not in the
    queue. It has no position, because it is not in line yet. Thus, a
    file that waits on a lock is visible where its amber chip paints."""

    with app.app_context():
        live = app.import_queue.enqueue(
            "app.videos.localization_task",
            args=("/import/Trail Live (2024) - [DVD].mkv",),
        )
        deferred = app.import_queue.enqueue_in(
            timedelta(minutes=10),
            "app.videos.localization_task",
            args=("/import/Trail Deferred (2024) - [DVD].mkv",),
            description="Localizing Trail Deferred (2024) - [DVD].mkv",
        )

    payload = admin_client.get("/api/queue-details").get_json()
    assert [task["id"] for task in payload["all"]] == [live.id, deferred.id]
    assert payload["count"] == 1
    booked = payload["all"][1]
    assert booked["status"] == "scheduled"
    assert booked["position"] is None
    assert booked["scheduled_for"]
    assert booked["description"] == "Localizing Trail Deferred (2024) - [DVD].mkv"
    assert payload["files"][0]["entries"][0]["job"] == deferred.id
    assert payload["files"][0]["entries"][0]["status"] == "scheduled"


def test_queue_details_files_limit_is_adjustable(app, admin_client):
    """Clamp the ?files=… value of the shared poll.

    The pipeline page asks the shared poll for more than the newest 25
    of the queue page through ?files=…. Fitzflix clamps the value. Thus,
    a hand-typed query cannot ask Redis for too much."""

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


def test_pipeline_trails_render_on_the_queue_page_and_landed_cards(admin_client):
    """Ask the poll for the full retained set from the queue page.

    The in-flight trails paint on the job rows of the queue page. Glenn
    folded the separate in-flight list of the File Activity page into
    them in 2026-08. The File Activity dashboard keeps only the chips of
    its completed cards. The old dedicated pipeline page is gone."""

    queue_page = admin_client.get("/queue").get_data(as_text=True)
    assert "window.pipelineTrailLimit = 100" in queue_page
    assert 'id="all-tasks"' in queue_page

    page = admin_client.get("/file-activity").get_data(as_text=True)
    assert 'id="pipeline-files-section"' not in page
    assert "In flight" not in page
    assert "window.pipelineTrailLimit = 100" in page

    assert admin_client.get("/maintenance/pipeline").status_code == 404

    maintenance = admin_client.get("/maintenance").get_data(as_text=True)
    assert "/file-activity" in maintenance
    assert "View file activity" in maintenance


def test_running_banners_hold_first_run_order(app, admin_client):
    """Sort the running list by the time that each FILE first began to run.

    The start of the current job does not set the order. A file can step
    from the import queue to the file-operation queue. The payload
    iterates that queue last. That file keeps its place ahead of a file
    that started after it. This is the original banner-order request
    by Glenn."""

    import time

    from rq.job import Job

    from app.pipeline import FILE_KEY, _digest

    with app.app_context():
        # File A began to run at 10:00. Its work has moved on to the
        # file-operation queue. File B began at 10:05. It is still in
        # localization on the import queue. The payload iterates that
        # queue FIRST.

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
        # StartedJobRegistry.add in rq 2 is NotImplemented. Workers add
        # executions. Thus, seed the raw wip zset: {job_id}:{execution_id}
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


def test_a_cancelled_jobs_stranded_chip_heals_away(app):
    """Prune the chip of a queued job that rq no longer knows.

    A queued job that is cancelled and deleted outside the pipeline
    leaves a "queued" stamp. Nothing advances that stamp. Example: the
    cancelled scaffold re-archives, 2026-08. When rq no longer knows the
    job, the read of the queue page prunes the chip. When nothing else
    remains, it deletes the trail. It does not show a phantom job until
    the TTL runs out."""

    from app.pipeline import ACTIVE_KEY, pipeline_trails

    with app.app_context():
        job = app.import_queue.enqueue(
            "app.videos.localization_task",
            args=("/import/Trail Orphan (2025) - [DVD].mkv",),
        )

    assert len(pipeline_trails(app.redis)) == 1

    job.delete()
    assert pipeline_trails(app.redis) == []
    assert app.redis.zcard(ACTIVE_KEY) == 0
    assert pipeline_trails(app.redis) == []


def test_a_cancelled_deferred_retrys_chip_heals_away(app):
    """Heal a stranded "scheduled" stamp the same as a queued stamp.

    This is the scheduled variant. A deferred retry that is deleted
    before it comes back strands a "scheduled" stamp."""

    from app.pipeline import pipeline_trails

    with app.app_context():
        job = app.import_queue.enqueue_in(
            timedelta(minutes=10),
            "app.videos.localization_task",
            args=("/import/Trail Orphan Retry (2025) - [DVD].mkv",),
        )

    assert len(pipeline_trails(app.redis)) == 1
    job.delete()
    assert pipeline_trails(app.redis) == []


def test_healing_keeps_terminal_history(app):
    """Prune only the waiting chips of the jobs that are gone.

    A finished chip is history. It survives after the job hash of rq
    expires. Thus, a cancelled follow-up disappears, and the completed
    stages that share its trail remain."""

    from app.pipeline import pipeline_trails, record_job_event

    with app.app_context():
        finished = app.import_queue.enqueue(
            "app.videos.localization_task",
            args=("/import/Trail Mixed (2025) - [DVD].mkv",),
        )
        record_job_event(app.redis, finished, "started")
        record_job_event(app.redis, finished, "done")
        followup = app.import_queue.enqueue(
            "app.videos.localization_task",
            args=("/import/Trail Mixed (2025) - [DVD].mkv",),
        )

    # The hash of the finished job expires (result TTL). That must not
    # remove its done chip. Only the chip of the cancelled follow-up goes.

    finished.delete()
    followup.delete()

    trails = pipeline_trails(app.redis)
    assert len(trails) == 1
    assert [entry["status"] for entry in trails[0]["entries"]] == ["done"]


def test_a_live_waiting_chip_never_heals(app):
    """Keep a chip if rq still holds its job.

    That chip is real work in line, not an orphan. The wait time is not
    important."""

    from app.pipeline import pipeline_trails

    with app.app_context():
        job = app.import_queue.enqueue(
            "app.videos.localization_task",
            args=("/import/Trail Waiting (2025) - [DVD].mkv",),
        )

    trails = pipeline_trails(app.redis)
    assert trails[0]["entries"][0]["status"] == "queued"
    assert app.redis.exists(f"rq:job:{job.id}")


def test_the_enqueued_time_holds_still_across_queue_hops(app, admin_client):
    """Anchor the Enqueued column to the time that the FILE first entered the pipeline.

    Each step to a new queue is a new job with its own new enqueued_at.
    Before, that made the time jump forward at each stage."""

    from app.pipeline import FILE_KEY, _digest, record_job_event

    basename = "Trail Anchor (2026) - [DVD].mkv"
    with app.app_context():
        localize = app.import_queue.enqueue(
            "app.videos.localization_task", args=(f"/import/{basename}",)
        )
        record_job_event(app.redis, localize, "started")
        move = app.file_queue.enqueue(
            "app.videos.move_localized_file",
            args=(f"/staging/{basename}", {"basename": basename}),
            description=f"Moving {basename}",
        )
        record_job_event(app.redis, localize, "done")

    # Pin the anchor that the first enqueue stamped to a known value.
    # Then the assertion does not race the seconds of the clock.

    key = FILE_KEY.format(digest=_digest(basename))
    app.redis.hset(key, "first_enqueued", "2026-01-02 03:04:05")

    payload = admin_client.get("/api/queue-details").get_json()
    entry = next(task for task in payload["all"] if task["id"] == move.id)
    assert entry["enqueued_at"] == "Fri, 02 Jan 2026 03:04:05 GMT"


def test_a_waiting_stamp_never_moves_a_journeys_anchor(app):
    """Keep the anchor of the first detection through mid-journey enqueues.

    Examples: the move job booked while localization still runs, or a
    retry after a failure."""

    from app.pipeline import FILE_KEY, _digest, record_job_event

    basename = "Trail Anchor Hold (2026) - [DVD].mkv"
    key = FILE_KEY.format(digest=_digest(basename))
    with app.app_context():
        localize = app.import_queue.enqueue(
            "app.videos.localization_task", args=(f"/import/{basename}",)
        )
        record_job_event(app.redis, localize, "started")
        app.redis.hset(key, "first_enqueued", "2026-01-02 03:04:05")
        app.file_queue.enqueue(
            "app.videos.move_localized_file",
            args=(f"/staging/{basename}", {"basename": basename}),
        )
    assert app.redis.hget(key, "first_enqueued") == b"2026-01-02 03:04:05"

    with app.app_context():
        record_job_event(app.redis, localize, "failed")
        app.import_queue.enqueue_in(
            timedelta(minutes=10),
            "app.videos.localization_task",
            args=(f"/import/{basename}",),
        )
    assert app.redis.hget(key, "first_enqueued") == b"2026-01-02 03:04:05"


def test_a_fresh_journey_restarts_the_enqueue_anchor(app):
    """Start the Enqueued time again when a waiting stamp arrives on a settled trail.

    Examples: a re-import, or a later re-archive of a completed file.
    That is a NEW journey through the pipeline. Thus, the Enqueued time
    does not show the old journey."""

    from app.pipeline import FILE_KEY, _digest, record_job_event

    basename = "Trail Anchor Fresh (2026) - [DVD].mkv"
    key = FILE_KEY.format(digest=_digest(basename))
    with app.app_context():
        localize = app.import_queue.enqueue(
            "app.videos.localization_task", args=(f"/import/{basename}",)
        )
        record_job_event(app.redis, localize, "started")
        record_job_event(app.redis, localize, "done")

    app.redis.hset(key, "first_enqueued", "2026-01-02 03:04:05")

    with app.app_context():
        app.import_queue.enqueue(
            "app.videos.localization_task", args=(f"/import/{basename}",)
        )
    assert app.redis.hget(key, "first_enqueued") != b"2026-01-02 03:04:05"


def test_a_mid_flight_rename_carries_the_enqueue_anchor(app):
    """Move the anchor with the journey in migrate_trail, the same as first_run.

    A title that becomes canonical keeps the Enqueued time of its
    detection."""

    from app.pipeline import FILE_KEY, _digest, migrate_trail

    old = "Trail Anchor Old (2026) - [DVD].mkv"
    new = "Trail Anchor New (2026) - [DVD].mkv"
    with app.app_context():
        app.import_queue.enqueue(
            "app.videos.localization_task", args=(f"/import/{old}",)
        )

    old_key = FILE_KEY.format(digest=_digest(old))
    app.redis.hset(old_key, "first_enqueued", "2026-01-02 03:04:05")

    migrate_trail(app.redis, old, new)
    new_key = FILE_KEY.format(digest=_digest(new))
    assert app.redis.hget(new_key, "first_enqueued") == b"2026-01-02 03:04:05"
