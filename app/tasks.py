"""Hold the registry of the rq tasks that Fitzflix enqueues by name.

rq stores the job name of each queued job in Redis as a string, for
example "app.videos.localization_task". The key of TASKS is that
string. Its TaskSpec gives the default queue, timeout and description
of the task. enqueue_task fills each one from the spec. Thus, a task
gets the same queue and the same timeout from every producer. An
explicit argument at a call site is an override. Such a site says why
in a comment.

The queue of a spec is a key of the roster of queues. The timeout is
a number of seconds or the name of a config setting, for example
"SQL_TASK_TIMEOUT". The setting is read at enqueue time.
"""

from collections import namedtuple
from datetime import timedelta

from flask import current_app

from app.queues import QUEUE_NAME

TaskSpec = namedtuple("TaskSpec", "queue timeout description", defaults=(None,))

TASKS = {
    # The recurring maintenance jobs of cron_table. Each one runs on the
    # maintenance queue.
    "app.maintenance.rotate_logs": TaskSpec(
        "maintenance", 54000, "Rotating application logs"
    ),
    "app.log_digest.log_digest_task": TaskSpec(
        "maintenance", 600, "Digesting repeated log warnings"
    ),
    "app.maintenance.backup_database": TaskSpec(
        "maintenance", 3600, "Backing up the database"
    ),
    "app.videos.manual_import_task": TaskSpec(
        "maintenance", 3600, "Scanning import directory for files"
    ),
    "app.maintenance.restore_drill": TaskSpec(
        "maintenance", 3600, "Verifying the offsite database backup restores"
    ),
    "app.videos.refresh_criterion_collection_info": TaskSpec(
        "maintenance", 3600, "Refreshing Criterion Collection information"
    ),
    "app.criterion_catalog.refresh_criterion_releases": TaskSpec(
        "maintenance",
        900,
        "Fetching the Criterion Collection releases from Wikidata",
    ),
    "app.maintenance.cleanup_orphaned_files": TaskSpec(
        "maintenance", 3600, "Cleaning up orphaned partial files"
    ),
    "app.videos.sync_aws_s3_storage_task": TaskSpec(
        "maintenance", 86400, "Syncing files with AWS S3 storage"
    ),
    "app.maintenance.smb_handle_sweep": TaskSpec(
        "maintenance", 3600, "Probing library files for lost SMB handles"
    ),
    "app.maintenance.health_probe": TaskSpec(
        "maintenance", 600, "Probing system health"
    ),
    "app.letterboxd.sync_letterboxd_feeds": TaskSpec(
        "maintenance", 900, "Syncing Letterboxd diaries"
    ),
    "app.provider_catalog.refresh_provider_catalogs": TaskSpec(
        "maintenance",
        3600,
        "Discovering provider-catalog films for recommendations",
    ),
    "app.missing_files.missing_best_files_task": TaskSpec(
        "maintenance", 1800, "Scanning for best files that have no local copy"
    ),
    "app.recommendations.recompute_recommendations": TaskSpec(
        "maintenance", 3600, "Recomputing film recommendations"
    ),
    "app.criterion_now.heartbeat_criterion_now": TaskSpec(
        "maintenance", 300, "Checking the Criterion 24/7 poller's pulse"
    ),
    "app.leaving_criterion.refresh_leaving_criterion": TaskSpec(
        "maintenance", 3600, "Refreshing the leaving-Criterion film set"
    ),
    "app.newly_added.refresh_newly_added": TaskSpec(
        "maintenance", 1800, "Refreshing the newly-added streaming feeds"
    ),
    "app.tmdb_refresh.sync_certification_list": TaskSpec(
        "maintenance", 600, "Syncing the TMDB certification list"
    ),
    "app.awards.refresh_awards": TaskSpec(
        "maintenance", 7200, "Refreshing film awards from Wikidata"
    ),
    "app.streaming_rail.recompute_streaming_rail": TaskSpec(
        "maintenance", 3600, "Recomputing the streaming rail"
    ),
    "app.estimate_warm.warm_estimates": TaskSpec(
        "maintenance", 3600, "Pre-warming estimate payloads"
    ),
    "app.frames.refresh_frame_pool_task": TaskSpec(
        "maintenance", 3600, "Refreshing the Name That Frame pool"
    ),
    "app.tmdb_changes.refresh_changed_records": TaskSpec(
        "maintenance", 1800, "Refreshing TMDB-changed records"
    ),
    "app.tmdb_refresh.refresh_in_production_tv": TaskSpec(
        "maintenance", 3600, "Refreshing in-production TV series"
    ),
    "app.streaming.refresh_availability": TaskSpec(
        "maintenance", 3600, "Refreshing streaming availability"
    ),
    "app.availability_alerts.notify_watchlist_availability": TaskSpec(
        "maintenance", 1800, "Checking watchlisted films for new availability"
    ),
    "app.videos.sqs_retrieve_task": TaskSpec(
        "maintenance", 7200, "Polling AWS SQS for files to download"
    ),
    "app.videos.plex_history_poll": TaskSpec(
        "maintenance", 900, "Polling Plex for watch history"
    ),
    "app.plex_titles.sync_plex_episode_titles": TaskSpec(
        "maintenance", 1800, "Syncing episode titles into Plex"
    ),
    "app.plex_library.refresh_plex_libraries": TaskSpec(
        "maintenance", 600, "Refreshing Plex libraries"
    ),
    "app.plex_collections.sync_criterion_collection": TaskSpec(
        "maintenance", 600, "Syncing the Criterion collections into Plex"
    ),
    "app.plex_watchlist.sync_plex_watchlist": TaskSpec(
        "maintenance", 900, "Syncing the Plex watchlist"
    ),
    "app.radarr_push.reconcile_radarr_paths": TaskSpec(
        "maintenance", 900, "Checking the Radarr movie paths"
    ),
    "app.dvr.build_channel_lineups": TaskSpec(
        "maintenance", 3600, "Building virtual DVR channel lineups"
    ),
    # Other maintenance work that the pages and the tasks queue.
    "app.streaming.warm_title_availability": TaskSpec("maintenance", 1800),
    "app.criterion_now.poll_criterion_now": TaskSpec("maintenance", 300),
    "app.plex_library.analyze_plex_media": TaskSpec("maintenance", 1800),
    # The import pipeline.
    "app.videos.localization_task": TaskSpec("import", "LOCALIZATION_TASK_TIMEOUT"),
    "app.videos.mkvmerge_task": TaskSpec("import", "LOCALIZATION_TASK_TIMEOUT"),
    "app.atmos.atmos_supplement_task": TaskSpec("import", "TRANSCODE_TASK_TIMEOUT"),
    "app.videos.move_localized_file": TaskSpec("handoff", "MOVE_TASK_TIMEOUT"),
    "app.videos.aws_delete": TaskSpec("handoff", "FILE_TASK_TIMEOUT"),
    "app.atmos.atmos_check_task": TaskSpec("handoff", 600),
    "app.atmos.atmos_finish_task": TaskSpec("handoff", "TRANSCODE_TASK_TIMEOUT"),
    "app.videos.convert_localized_file": TaskSpec(
        "transcode", "TRANSCODE_TASK_TIMEOUT"
    ),
    "app.videos.remux_audio_plan_task": TaskSpec("transcode", "TRANSCODE_TASK_TIMEOUT"),
    "app.triage.generate_triage_snapshots": TaskSpec("transcode", 7200),
    "app.triage.generate_audio_comparison": TaskSpec("transcode", 7200),
    "app.frames.extract_frame_task": TaskSpec("transcode", 600),
    "app.frames.replace_frame_task": TaskSpec("transcode", 600),
    # The file operations.
    "app.videos.upload_task": TaskSpec("file", "UPLOAD_TASK_TIMEOUT"),
    "app.videos.rearchive_untouched_object": TaskSpec("file", "UPLOAD_TASK_TIMEOUT"),
    "app.videos.archive_held_task": TaskSpec("file", "UPLOAD_TASK_TIMEOUT"),
    "app.aws_storage.move_archive_to_modified_task": TaskSpec(
        "file", "UPLOAD_TASK_TIMEOUT"
    ),
    "app.videos.download_task": TaskSpec("file", "TRANSCODE_TASK_TIMEOUT"),
    "app.videos.mkvpropedit_task": TaskSpec("file", "MKVPROPEDIT_TASK_TIMEOUT"),
    "app.videos.track_metadata_scan_task": TaskSpec("file", "MKVPROPEDIT_TASK_TIMEOUT"),
    "app.series_rename.rename_tv_series_task": TaskSpec("file", "SQL_TASK_TIMEOUT"),
    "app.manual_imports.manual_import_task": TaskSpec(
        "file", "LOCALIZATION_TASK_TIMEOUT"
    ),
    # The database writes.
    "app.videos.finalize_localization": TaskSpec("sql", "SQL_TASK_TIMEOUT"),
    "app.videos.apply_tmdb_refresh": TaskSpec("sql", "SQL_TASK_TIMEOUT"),
    "app.videos.apply_plex_watch": TaskSpec("sql", "SQL_TASK_TIMEOUT"),
    "app.videos.apply_letterboxd_import": TaskSpec("sql", "SQL_TASK_TIMEOUT"),
    "app.videos.save_track_metadata": TaskSpec("sql", "SQL_TASK_TIMEOUT"),
    "app.videos.track_metadata_scan_library": TaskSpec("sql", "SQL_TASK_TIMEOUT"),
    "app.videos.review_task": TaskSpec("sql", "SQL_TASK_TIMEOUT"),
    # The work that a user or an outside service asks for.
    "app.videos.refresh_tmdb_info": TaskSpec("request", "SQL_TASK_TIMEOUT"),
    "app.videos.aws_restore": TaskSpec("request", "SQL_TASK_TIMEOUT"),
    "app.videos.letterboxd_import_task": TaskSpec(
        "request", "LOCALIZATION_TASK_TIMEOUT"
    ),
    "app.infuse_player.pair_task": TaskSpec("request", 240),
    "app.radarr_push.adopt_download_task": TaskSpec("request", 300),
    "app.radarr_push.follow_import_move_task": TaskSpec("request", 300),
    "app.radarr_push.follow_rename_task": TaskSpec("request", 300),
    "app.sonarr_push.adopt_episode_task": TaskSpec("request", 300),
}


def task_timeout(spec, config=None):
    """Return the timeout of a spec in seconds.

    A config-setting name is read from config, or from the config of
    the current app."""

    if isinstance(spec.timeout, str):
        return (config if config is not None else current_app.config)[spec.timeout]
    return spec.timeout


def resolve_queue(queue):
    """Return the queue object of a roster key. A queue object passes through."""

    if isinstance(queue, str):
        if queue not in QUEUE_NAME:
            raise KeyError(f"unknown queue {queue!r}")
        return getattr(current_app, f"{queue}_queue")
    return queue


def enqueue_task(
    name,
    *args,
    delay=None,
    at_front=False,
    queue=None,
    timeout=None,
    description=None,
    job_id=None,
    meta=None,
    result_ttl=None,
    **kwargs,
):
    """Queue the task called name with its default queue, timeout and description.

    The positional and the other keyword arguments go to the task. delay
    is a timedelta or a number of seconds. A job with a delay goes to
    the ScheduledJobRegistry. queue is a roster key or a queue object.
    Each explicit argument overrides the spec. The return value is the
    rq job."""

    spec = TASKS[name]
    target = resolve_queue(queue if queue is not None else spec.queue)
    options = {
        "args": args,
        "kwargs": kwargs,
        "job_timeout": timeout if timeout is not None else task_timeout(spec),
    }
    if description is None:
        description = spec.description
    if description is not None:
        options["description"] = description
    if job_id is not None:
        options["job_id"] = job_id
    if meta is not None:
        options["meta"] = meta
    if result_ttl is not None:
        options["result_ttl"] = result_ttl
    if at_front:
        options["at_front"] = True
    if delay is None:
        return target.enqueue(name, **options)
    if not isinstance(delay, timedelta):
        delay = timedelta(seconds=delay)
    return target.enqueue_in(delay, name, **options)
