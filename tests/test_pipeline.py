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


def test_a_phased_jobs_done_own_chip_hides(app, monkeypatch):
    """Hide the own chip of a phased job when it is done.

    The Atmos job records all of its work as phase chips. Its own chip
    shows that more work comes while the job waits or runs. When the job
    is done, the chip tells nothing more (Incredibles 2, 2026-10-15). A
    failed chip and the chip of a job with no phases stay."""

    import rq

    from app import db
    from app.pipeline import TaskPhases, pipeline_trails, record_job_event

    def chips(basename):
        (trail,) = [t for t in pipeline_trails(app.redis) if t["basename"] == basename]
        return [(entry["stage"], entry["status"]) for entry in trail["entries"]]

    def start(title):
        with app.app_context():
            file = make_movie_file(make_movie(title, 2018), "WEBDL-1080p")
            db.session.commit()
            job = app.handoff_queue.enqueue(
                "app.atmos.atmos_supplement_task", args=(file.id,)
            )
            basename = file.basename
        record_job_event(app.redis, job, "started")
        monkeypatch.setattr(rq, "get_current_job", lambda: job)
        return job, basename

    job, basename = start("Phased Done")
    phases = TaskPhases()
    with app.app_context():
        phases.enter("Decoding TrueHD")
    assert chips(basename) == [
        ("Decoding TrueHD", "started"),
        ("Adding Atmos twin", "queued"),
    ]
    with app.app_context():
        phases.finish()
    assert chips(basename) == [
        ("Decoding TrueHD", "done"),
        ("Adding Atmos twin", "started"),
    ]
    record_job_event(app.redis, job, "done")
    assert chips(basename) == [("Decoding TrueHD", "done")]

    job, basename = start("Phased Failed")
    phases = TaskPhases()
    with app.app_context():
        phases.enter("Decoding TrueHD")
        phases.fail()
    record_job_event(app.redis, job, "failed")
    assert chips(basename) == [
        ("Decoding TrueHD", "failed"),
        ("Adding Atmos twin", "failed"),
    ]

    job, basename = start("Phased Skip")
    record_job_event(app.redis, job, "done")
    assert chips(basename) == [("Adding Atmos twin", "done")]


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
    its completed cards. The server paints those. Thus, that page asks the
    poll for no trails of its own. The old dedicated pipeline page is
    gone."""

    queue_page = admin_client.get("/queue").get_data(as_text=True)
    assert "window.pipelineTrailLimit = 100" in queue_page
    assert 'id="all-tasks"' in queue_page

    page = admin_client.get("/file-activity").get_data(as_text=True)
    assert 'id="pipeline-files-section"' not in page
    assert "In flight" not in page
    assert "window.pipelineTrailLimit = " not in page

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


def test_the_queue_table_and_the_banners_share_one_order(app, admin_client):
    """Sort the running rows of the table in the order of the banners.

    File A began to run first. Its work then stepped to the
    file-operation queue, as a move job that started after file B. The
    table sorted by the start of the current job. Thus, file A dropped
    below file B in the table and stayed above it in the banners. Now
    both sort by the first start of the file. The Started column shows
    that time."""

    import time

    from rq.job import Job

    from app.pipeline import FILE_KEY, _digest

    with app.app_context():
        app.redis.hset(
            FILE_KEY.format(digest=_digest("Table A (2020) - [DVD].mkv")),
            "first_run",
            "2026-01-01 10:00:00",
        )
        app.redis.hset(
            FILE_KEY.format(digest=_digest("Table B (2021) - [DVD].mkv")),
            "first_run",
            "2026-01-01 10:05:00",
        )
        for job_id, func, args, queue in (
            (
                "table-b-localize",
                "app.videos.localization_task",
                ("/import/Table B (2021) - [DVD].mkv",),
                "fitzflix-import",
            ),
            (
                "table-a-move",
                "app.videos.move_localized_file",
                (
                    "/staging/.Table A (2020) - [DVD].mkv",
                    {"basename": "Table A (2020) - [DVD].mkv"},
                    None,
                    None,
                ),
                "fitzflix-file-operation",
            ),
        ):
            job = Job.create(
                func, args=args, id=job_id, origin=queue, connection=app.redis
            )
            job.save()
            app.redis.zadd(f"rq:wip:{queue}", {f"{job_id}:test": time.time() + 600})

    payload = admin_client.get("/api/queue-details").get_json()
    banners = [item["id"] for item in payload["running"]]
    rows = [item["id"] for item in payload["all"] if item["status"] != "queued"]
    assert banners == ["table-a-move", "table-b-localize"]
    assert rows == banners
    assert [item["position"] for item in payload["all"][:2]] == [1, 2]

    page = admin_client.get("/queue").get_data(as_text=True)
    assert page.index("Table A (2020)") < page.index("Table B (2021)")


def test_a_cataloging_job_lists_on_the_queue_page(app, admin_client):
    """List the cataloging job of a file, which runs on the SQL queue.

    Before, the page listed only the import, transcode, and
    file-operation queues. A file in cataloging thus had no job row. Its
    trail showed as a "no job in queue" row at the end of the table. The
    other work of the SQL queue is not file work. It does not list. The
    SQL worker gets no progress banner."""

    import time

    from app.pipeline import pipeline_trails, queue_rows

    basename = "Trail Catalog (2026) - [DVD].mkv"
    with app.app_context():
        catalog = app.sql_queue.enqueue(
            "app.videos.finalize_localization",
            args=(f"/staging/{basename}", {"basename": basename}),
            description=f"'{basename}'",
        )
        app.sql_queue.enqueue(
            "app.tmdb_refresh.refresh_everything",
            description="An SQL refresh",
        )

    payload = admin_client.get("/api/queue-details").get_json()
    assert [item["id"] for item in payload["all"]] == [catalog.id]
    assert payload["count"] == 1

    with app.app_context():
        app.redis.lrem("rq:queue:fitzflix-sql", 0, catalog.id)
        app.redis.zadd("rq:wip:fitzflix-sql", {f"{catalog.id}:test": time.time() + 600})
    payload = admin_client.get("/api/queue-details").get_json()

    # The running job lists as row 1. It gets no banner (Glenn,
    # 2026-10-14). The banners stay at 1 for each pipeline worker.

    assert [item["id"] for item in payload["all"]] == [catalog.id]
    assert payload["running"] == []

    rows = queue_rows(payload["all"], pipeline_trails(app.redis))
    assert [row["task"]["id"] for row in rows] == [catalog.id]
    assert rows[0]["trail"]["basename"] == basename


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


def _arrive(app, basename, job_id, monkeypatch):
    """Take a file through localization, the move, and the catalog.

    The localization job id comes from the basename, as in the import
    sweep. Then move the stamps of that run into the past."""

    import json

    import rq

    from app.pipeline import FILE_KEY, _digest, record_job_event, record_task_stage

    with app.app_context():
        localize = app.import_queue.enqueue(
            "app.videos.localization_task",
            args=(f"/import/{basename}",),
            job_id=job_id,
        )
        record_job_event(app.redis, localize, "started")
        monkeypatch.setattr(rq, "get_current_job", lambda: localize)
        record_task_stage("Copying to staging", "started")
        record_task_stage("Copying to staging", "done")
        move = app.file_queue.enqueue(
            "app.videos.move_localized_file",
            args=(f"/staging/.{basename}", {"basename": basename}, None, None),
        )
        record_job_event(app.redis, localize, "done")
        record_job_event(app.redis, move, "started")
        record_job_event(app.redis, move, "done")
        catalog = app.import_queue.enqueue(
            "app.videos.finalize_localization",
            args=(None, {"basename": basename}),
        )
        record_job_event(app.redis, catalog, "started")
        record_job_event(app.redis, catalog, "done")

    key = FILE_KEY.format(digest=_digest(basename))
    trail = json.loads(app.redis.hget(key, "trail"))
    for entry in trail:
        entry["at"] = "2026-01-01 00:00:00"
    app.redis.hset(key, "trail", json.dumps(trail))
    app.redis.hset(key, "first_run", "2026-01-01 00:00:00")
    return key


def test_a_reimport_under_the_same_job_id_clears_the_earlier_chips(app, monkeypatch):
    """Remove the chips of the earlier run when the same job id runs again.

    The import sweep uses the basename as the localization job id. A
    re-import of the same file thus finds the settled chips of the
    earlier run. Before, the new Localizing chip ran while Moved into
    the library and Cataloged stayed green after it."""

    import rq

    from app.pipeline import pipeline_trails, record_job_event, record_task_stage

    basename = "Trail Reimport (2026) - [DVD].mkv"
    key = _arrive(app, basename, "trail-reimport", monkeypatch)
    entries = pipeline_trails(app.redis)[0]["entries"]
    assert [entry["stage"] for entry in entries] == [
        "Copying to staging",
        "Localizing",
        "Moving into the library",
        "Cataloging",
    ]

    with app.app_context():
        again = app.import_queue.enqueue(
            "app.videos.localization_task",
            args=(f"/import/{basename}",),
            job_id="trail-reimport",
        )
    entries = pipeline_trails(app.redis)[0]["entries"]
    assert [(entry["stage"], entry["status"]) for entry in entries] == [
        ("Localizing", "queued"),
    ]
    assert app.redis.hget(key, "first_run") is None

    record_job_event(app.redis, again, "started")
    monkeypatch.setattr(rq, "get_current_job", lambda: again)
    record_task_stage("Copying to staging", "started")
    entries = pipeline_trails(app.redis)[0]["entries"]
    assert [(entry["stage"], entry["status"]) for entry in entries] == [
        ("Copying to staging", "started"),
        ("Localizing", "queued"),
    ]
    assert app.redis.hget(key, "first_run") != b"2026-01-01 00:00:00"


def test_a_rerun_with_no_queued_stamp_clears_the_earlier_chips(app, monkeypatch):
    """Remove the chips of the earlier run when "started" finds a settled chip.

    A scheduled job has no enqueue time to compare. Thus, its waiting
    stamp holds back. The "started" stamp of the worker then starts the
    new run."""

    from app.pipeline import pipeline_trails, record_job_event

    basename = "Trail Restart (2026) - [DVD].mkv"
    key = _arrive(app, basename, "trail-restart", monkeypatch)
    app.redis.hset(key, "first_enqueued", "2026-01-01 00:00:00")

    with app.app_context():
        job = app.import_queue.fetch_job("trail-restart")
    record_job_event(app.redis, job, "started")

    entries = pipeline_trails(app.redis)[0]["entries"]
    assert [(entry["stage"], entry["status"]) for entry in entries] == [
        ("Localizing", "started"),
    ]
    assert app.redis.hget(key, "first_enqueued") != b"2026-01-01 00:00:00"


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


def test_queue_page_renders_its_rows_on_the_server(app, admin_client):
    """Render the queue table on the server, with the trail chips in place.

    Before, the table body was empty until the first poll filled it. The
    rows then popped into place a split second after the page loaded.
    Now the server renders the same rows that renderAll paints. The
    poll repaints them with the same markup."""

    with app.app_context():
        app.import_queue.enqueue(
            "app.videos.localization_task",
            args=("/import/Server Row Film (2020) - [DVD].mkv",),
            description="'Server Row Film (2020) - [DVD].mkv'",
        )

    page = admin_client.get("/queue").get_data(as_text=True)
    assert "Server Row Film (2020) - [DVD].mkv" in page
    assert "text-bg-secondary" in page
    assert "Localizing &middot; queued" in page
    assert 'id="all-tasks">' in page
    # The queued job holds position 1 in the live queue. It is at the
    # head of its queue. Thus, it gets no front button (#236)
    assert '<th scope="row" class="col-1 queue-position">1</th>' in page


def test_queue_rows_list_the_orphan_trails_after_the_jobs():
    """Put a trail with no job of its own after the jobs, once.

    A trail whose last stage is done is settled. It gets no row. A trail
    that a listed job owns gets no second row."""

    from app.pipeline import queue_rows

    owned = {"basename": "Owned.mkv", "entries": [{"job": "j1", "status": "started"}]}
    settled = {"basename": "Settled.mkv", "entries": [{"job": "j9", "status": "done"}]}
    orphan = {"basename": "Orphan.mkv", "entries": [{"job": "j8", "status": "failed"}]}
    tasks = [{"id": "j1", "position": 1}]

    rows = queue_rows(tasks, [owned, settled, orphan])
    assert [row["task"] and row["task"]["id"] for row in rows] == ["j1", None]
    assert rows[0]["trail"] is owned
    assert rows[1]["trail"] is orphan


def test_file_activity_cards_arrive_with_their_chips_painted(app, admin_client):
    """Paint the trail chips of a card on the server.

    The slot of a card with a trail is visible and holds the chips. The
    slot of a card with no trail stays hidden. The card fragment that
    the poll fetches carries its chips too. Thus, nothing pops into
    place after the first paint, and the poll sees the chips change."""

    from app import db
    from tests.factories import make_movie, make_movie_file

    with app.app_context():
        movie = make_movie("Painted Film", 2014)
        make_movie_file(movie, "DVD", untouched_basename="Painted.Film.2014.avi")
        bare = make_movie("Bare Film", 2015)
        make_movie_file(bare, "DVD", untouched_basename="Bare.Film.2015.avi")
        db.session.commit()

    def version(body, title):
        card = next(c for c in body.split('data-card-key="') if title in c)
        return card.split('data-card-version="', 1)[1].split('"', 1)[0]

    before = admin_client.get("/file-activity/cards").get_data(as_text=True)

    with app.app_context():
        # The trail is keyed by the import name with the converted extension
        app.import_queue.enqueue(
            "app.videos.localization_task",
            args=("/import/Painted.Film.2014.mkv",),
            description="'Painted.Film.2014.mkv'",
        )

    page = admin_client.get("/file-activity").get_data(as_text=True)
    # Each segment after a split runs from one card to the next card
    cards = page.split('data-card-key="')
    painted_html = next(c for c in cards if "Painted Film (2014)" in c)
    bare_html = next(c for c in cards if "Bare Film (2015)" in c)
    assert '<div class="mt-1" data-trail>' in painted_html
    assert "Localizing &middot; queued" in painted_html
    assert '<div class="mt-1" data-trail hidden>' in bare_html
    assert "Localizing" not in bare_html

    deeper = admin_client.get("/file-activity?page=2").get_data(as_text=True)
    assert "Localizing &middot; queued" not in deeper

    # A new trail entry changes the version of its card only. Thus, the
    # poll replaces that card and keeps the other one.

    listing = admin_client.get("/file-activity/cards").get_data(as_text=True)
    assert "Localizing &middot; queued" in listing
    assert version(listing, "Painted Film (2014)") != version(
        before, "Painted Film (2014)"
    )
    assert version(listing, "Bare Film (2015)") == version(before, "Bare Film (2015)")


def test_the_started_column_shows_the_first_start_of_the_file(app, admin_client):
    """Show the first start of the file as aware UTC in the Started column.

    The trail stores local wall clock. flask-moment reads each time as
    UTC. A local value would show hours away from the real start."""

    import time

    from datetime import datetime, timezone

    from rq.job import Job

    from app.pipeline import FILE_KEY, _digest

    basename = "Started Column (2020) - [DVD].mkv"
    with app.app_context():
        app.redis.hset(
            FILE_KEY.format(digest=_digest(basename)),
            "first_run",
            "2026-01-01 10:00:00",
        )
        job = Job.create(
            "app.videos.localization_task",
            args=(f"/import/{basename}",),
            id="started-column",
            origin="fitzflix-import",
            connection=app.redis,
        )
        job.save()
        app.redis.zadd(
            "rq:wip:fitzflix-import", {"started-column:test": time.time() + 600}
        )
        from app.models import User

        details = User.query.filter_by(admin=True).first().get_queue_details()

    started = details["all"][0]["started_at"]
    expected = datetime(2026, 1, 1, 10, 0, 0).astimezone().astimezone(timezone.utc)
    assert started == expected
    assert started.utcoffset().total_seconds() == 0


def test_a_jobs_phases_read_in_order_before_its_own_chip(app, monkeypatch):
    """Put each phase of 1 job before the own chip of the job, in order.

    The Atmos twin reports 6 phases. Before, each sub-stage went before
    the FIRST entry of its job. A second sub-stage thus went before the
    first one, and the chips read in reverse."""

    import rq

    from app.pipeline import TaskPhases, pipeline_trails, record_job_event

    with app.app_context():
        job = app.import_queue.enqueue(
            "app.videos.localization_task",
            args=("/import/Trail Phases (2026) - [DVD].mkv",),
        )
    record_job_event(app.redis, job, "started")
    monkeypatch.setattr(rq, "get_current_job", lambda: job)

    phases = TaskPhases()
    phases.enter("Copying to staging")
    phases.enter("Decoding TrueHD")
    entries = pipeline_trails(app.redis)[0]["entries"]
    assert [(entry["stage"], entry["status"]) for entry in entries] == [
        ("Copying to staging", "done"),
        ("Decoding TrueHD", "started"),
        ("Localizing", "queued"),
    ]

    phases.enter("Encoding E-AC-3 Atmos")
    phases.fail()
    record_job_event(app.redis, job, "failed")
    entries = pipeline_trails(app.redis)[0]["entries"]
    assert [(entry["stage"], entry["status"]) for entry in entries] == [
        ("Copying to staging", "done"),
        ("Decoding TrueHD", "done"),
        ("Encoding E-AC-3 Atmos", "failed"),
        ("Localizing", "failed"),
    ]


def test_the_atmos_twin_leaves_a_trail(app):
    """Give the Atmos twin job a trail, like the other stages of a file."""

    from app import db
    from app.pipeline import pipeline_trails

    with app.app_context():
        movie = make_movie("Trail Atmos", 1982)
        file = make_movie_file(movie, "WEBDL-1080p")
        db.session.commit()
        file_id, basename = file.id, file.basename
        app.transcode_queue.enqueue(
            "app.atmos.atmos_supplement_task",
            args=(file_id,),
            description=f"E-AC-3 Atmos twin for '{basename}'",
        )

    trails = pipeline_trails(app.redis)
    assert trails[0]["basename"] == basename
    assert [(entry["stage"], entry["status"]) for entry in trails[0]["entries"]] == [
        ("Adding Atmos twin", "queued"),
    ]


def test_both_chip_painters_know_the_past_tense_of_each_stage():
    """Keep the past tense of each chip the same in both painters.

    The server macro and paintChips in base.html each carry the map. A
    done chip with no entry kept its present tense ("Re-archiving to
    S3" after it completed)."""

    import pathlib
    import re

    from app.pipeline import STAGES

    templates = pathlib.Path(__file__).resolve().parent.parent / "app" / "templates"
    macro = (templates / "_trail_chips.html").read_text()
    macro = macro[macro.index("chip_past_tense") :]
    macro = macro[: macro.index("} %}")]
    script = (templates / "base.html").read_text()
    script = script[script.index("var chipPastTense") :]
    script = script[: script.index("};")]
    pair = re.compile(r'"([^"]+)": "([^"]+)"')
    server = dict(pair.findall(macro))
    browser = dict(pair.findall(script))

    assert server == browser

    # The own chip of the Atmos job comes after all its steps. It reads as
    # the end of the job. "Added Atmos twin" read as 1 more step after the
    # S3 archive (Glenn, 2026-10-15). A job with phase chips hides it when
    # done. Thus, only a job with no phases shows this label.

    assert server["Adding Atmos twin"] == "Atmos twin complete"

    # The Atmos step that writes the result over the library file is not
    # the move of an import. 2 chips with the same label read as the same
    # step (Glenn, 2026-10-16).

    assert server["Replacing the library file"] == "Replaced the library file"

    # Each sub-stage that the tasks record needs a past tense. The test
    # reads the labels from the source. Thus, a new phase cannot skip it.

    app_dir = templates.parent
    phase = re.compile(r'(?:phases\.enter|record_task_stage)\(\s*"([^"]+)"')
    sub_stages = {
        label
        for path in app_dir.glob("*.py")
        for label in phase.findall(path.read_text())
    }
    assert {"Decoding TrueHD", "Replacing the library file"} <= sub_stages
    labels = {label for label, _ in STAGES.values()} | sub_stages
    assert labels <= set(server)


def test_queue_rows_carry_the_phone_layout_markup(app, admin_client):
    """Give each queue row a line of times for a phone.

    On a phone, the 2 time columns do not show. The queue-times line in
    the File cell shows the same times. Each phrase stays whole. The
    poll (renderAll and phoneTimes) writes the same markup."""

    with app.app_context():
        app.import_queue.enqueue(
            "app.videos.localization_task",
            args=("/import/Phone Row (2026) - [DVD].mkv",),
            description="'Phone Row (2026) - [DVD].mkv'",
        )

    page = admin_client.get("/queue").get_data(as_text=True)
    assert 'class="table table-striped queue-table"' in page
    for kind in ("queue-position", "queue-file", "queue-enqueued", "queue-started"):
        assert kind in page
    times = page[page.index('<div class="queue-times') :]
    times = times[: times.index("</div>")]
    assert '<span class="text-nowrap">Enqueued ' in times
    assert "started " not in times
    assert (
        "fileCell.appendChild(phoneTimes(task, trailByJob[task.trail_job || task.id]))"
        in page
    )


def test_a_job_that_starts_before_its_queued_stamp_keeps_its_enqueue_time(app):
    """Anchor Enqueued at the first job, also when its stamp comes late.

    A free worker started the localization before the "queued" stamp of
    the enqueue side arrived. The late stamp was dropped, and its enqueue
    time with it. The library copy then filled first_enqueued at the end
    of the localization. The Enqueued column showed that time (Gandhi,
    How to Train Your Dragon The Hidden World, 2026-10-15)."""

    from datetime import timezone

    from rq import Queue

    from app.pipeline import FILE_KEY, _digest, record_job_event

    basename = "Trail Late Anchor (2026) - [DVD].mkv"
    key = FILE_KEY.format(digest=_digest(basename))
    with app.app_context():
        # A plain rq Queue writes no stamp. Thus, the test controls the
        # order of the 2 stamps.

        plain = Queue(app.import_queue.name, connection=app.redis)
        localize = plain.enqueue(
            "app.videos.localization_task", args=(f"/import/{basename}",)
        )
        expected = localize.enqueued_at.astimezone(timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S"
        )

        record_job_event(app.redis, localize, "started")
        record_job_event(app.redis, localize, "queued")
        assert app.redis.hget(key, "first_enqueued") == expected.encode()

        app.redis.hset(key, "first_enqueued", "2026-01-02 03:04:05")
        app.handoff_queue.enqueue(
            "app.videos.move_localized_file",
            args=(f"/staging/.{basename}", {"basename": basename}, None, None),
        )
        assert app.redis.hget(key, "first_enqueued") == b"2026-01-02 03:04:05"

    # The same holds when the late stamp never arrives. The "started"
    # stamp fills the anchor.

    other = "Trail Started Anchor (2026) - [DVD].mkv"
    other_key = FILE_KEY.format(digest=_digest(other))
    with app.app_context():
        job = plain.enqueue("app.videos.localization_task", args=(f"/import/{other}",))
        record_job_event(app.redis, job, "started")
        assert app.redis.hget(other_key, "first_enqueued") == (
            job.enqueued_at.astimezone(timezone.utc)
            .strftime("%Y-%m-%d %H:%M:%S")
            .encode()
        )


def test_a_job_that_books_its_own_retry_shows_only_the_retry(app):
    """Hide an attempt that deferred itself. Show its retry.

    The localization found a file that was still copying in. It booked a
    retry and returned without error. Its worker stamped it done. Thus,
    a "Localized" chip came before "Localizing · waiting to retry"
    (NOVA, 2026-10-15)."""

    from app.pipeline import pipeline_trails, record_job_event

    path = "/import/Trail Deferral (2026) - [DVD].mkv"
    with app.app_context():
        first = app.import_queue.enqueue("app.videos.localization_task", args=(path,))
        record_job_event(app.redis, first, "started")
        app.import_queue.enqueue_in(
            timedelta(minutes=2), "app.videos.localization_task", args=(path,)
        )
        record_job_event(app.redis, first, "done")

    (trail,) = [
        t
        for t in pipeline_trails(app.redis, limit=100)
        if t["basename"] == "Trail Deferral (2026) - [DVD].mkv"
    ]
    assert [(e["stage"], e["status"]) for e in trail["entries"]] == [
        ("Localizing", "scheduled"),
    ]


def test_a_library_copy_that_books_a_retry_shows_only_the_retry(app):
    """Hide a library copy that caught a transient error and booked a retry.

    The copy returned without error after it booked the retry. Thus, its
    chip read "Moved into the library" while the copy waited to try
    again. The localization before it stays done."""

    from app.pipeline import pipeline_trails, record_job_event

    basename = "Trail Copy Retry (2026) - [DVD].mkv"
    with app.app_context():
        localize = app.import_queue.enqueue(
            "app.videos.localization_task", args=(f"/import/{basename}",)
        )
        record_job_event(app.redis, localize, "started")
        move = app.handoff_queue.enqueue(
            "app.videos.move_localized_file",
            args=(f"/staging/.{basename}", {"basename": basename}, None, None),
        )
        record_job_event(app.redis, localize, "done")
        record_job_event(app.redis, move, "started")
        app.handoff_queue.enqueue_in(
            timedelta(minutes=5),
            "app.videos.move_localized_file",
            f"/staging/.{basename}",
            {"basename": basename},
            None,
            None,
            transient_retries=1,
        )
        record_job_event(app.redis, move, "done")

    (trail,) = [
        t for t in pipeline_trails(app.redis, limit=100) if t["basename"] == basename
    ]
    assert [(e["stage"], e["status"]) for e in trail["entries"]] == [
        ("Localizing", "done"),
        ("Moving into the library", "scheduled"),
    ]
