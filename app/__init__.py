import json
import logging
import os
import re
import threading
import time
import traceback

from logging.handlers import SMTPHandler, WatchedFileHandler
from urllib.parse import quote_plus

import rq

from redis import Redis
from redlock import Redlock
from rq.exceptions import NoSuchJobError
from rq.job import Job
from rq.registry import StartedJobRegistry
from watchdog.events import (
    FileCreatedEvent,
    FileMovedEvent,
    FileSystemEventHandler,
)
from watchdog.observers.polling import PollingObserver
from werkzeug.middleware.proxy_fix import ProxyFix

from config import Config
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_login import LoginManager
from flask_mail import Mail
from flask_moment import Moment

db = SQLAlchemy()
migrate = Migrate(compare_type=True)
login = LoginManager()
login.login_view = "auth.login"
mail = Mail()
moment = Moment()

_app = None


def safe_job_id(value):
    """Flatten a string into rq 2's allowed job-id charset.

    Job ids carry the dedup and retry-replacement semantics (file basenames,
    deterministic retry:task:target ids), so disallowed characters are
    mapped to underscores rather than the ids being abandoned.
    """

    return re.sub(r"[^A-Za-z0-9_-]", "_", value)


def importable_basename(basename):
    """Whether an import-directory name is a finished file worth sweeping.

    Hidden names and transfer tools' intermediate artifacts must never
    become localization jobs (#244: a transient `.mkv.staged` copy was
    swept into a job of its own, and the restart that killed it left a
    phantom File Activity row) — the artifact's promotion to its real
    name fires its own filesystem event when the transfer completes.
    """

    if basename.startswith("."):
        return False
    return not basename.lower().endswith(
        (".staged", ".partial", ".part", ".tmp", ".filepart", ".crdownload")
    )


def retry_job_id(task, target, *attempt):
    """Name a retry job after the attempt it schedules, not just its target.

    rq re-saves a job's payload when the job returns, so a retry scheduled
    under the id of the job scheduling it is overwritten with that job's own
    stale kwargs the moment it finishes: the attempt counters never advance
    and the chain retries forever instead of giving up. Folding the counters
    into the id keeps replacement semantics for a re-deferred attempt while
    giving each new attempt an id of its own.
    """

    counters = ":".join(str(count) for count in attempt)
    return safe_job_id(f"retry:{task}:{target}:{counters}")


def enqueue_import_scan(
    queue, description="Scanning import directory for files", at_front=False
):
    """Queue a sweep of the import directory for files to import."""

    queue.enqueue(
        "app.videos.manual_import_task",
        args=(),
        job_timeout="1h",
        description=description,
        at_front=at_front,
    )


def cron_table(config):
    """The recurring-jobs table: every scheduled task as a plain
    row, config-dependent entries included only when configured. The
    scheduler.py process registers these with rq's native CronScheduler;
    nothing else registers cron jobs, so the table is authoritative on
    every scheduler start.
    """

    table = [
        # Rotate the application log daily at midnight
        (
            "0 0 * * *",
            "app.maintenance.rotate_logs",
            54000,
            "Rotating application logs",
        ),
        # Back up the database nightly: the media files are archived at
        # AWS, but the database itself exists only on this machine, so
        # each dump is also copied to the S3 bucket
        (
            "30 0 * * *",
            "app.maintenance.backup_database",
            3600,
            "Backing up the database",
        ),
        # Sweep the import directory hourly as a safety net in case the
        # filesystem observer misses an arrival
        (
            "0 * * * *",
            "app.videos.manual_import_task",
            3600,
            "Scanning import directory for files",
        ),
        # Monthly restore drill: prove the newest offsite dump restores
        (
            "0 4 1 * *",
            "app.maintenance.restore_drill",
            3600,
            "Verifying the offsite database backup restores",
        ),
        # Refresh Criterion spine numbers from Wikidata monthly; the 18th
        # leaves time for Wikidata to catch up with the mid-month reveal
        (
            "0 3 18 * *",
            "app.videos.refresh_criterion_collection_info",
            3600,
            "Refreshing Criterion Collection information",
        ),
        # Sweep orphaned partial files weekly, after the backup window
        (
            "0 1 * * 0",
            "app.maintenance.cleanup_orphaned_files",
            3600,
            "Cleaning up orphaned partial files",
        ),
        # Ask every library file whether the NAS still holds its handle.
        # The state is invisible until an upload's close fails, so the
        # sweep asks nightly instead of waiting to be surprised. 5am is
        # after the night's other maintenance and far outside viewing
        # hours: it is ~21k opens over SMB, a minute's work, and Plex
        # reads the same shares.
        (
            "0 5 * * *",
            "app.maintenance.smb_handle_sweep",
            3600,
            "Probing library files for lost SMB handles",
        ),
        ("*/10 * * * *", "app.maintenance.health_probe", 600, "Probing system health"),
        # Sync Letterboxd diaries from each user's RSS feed; the
        # task no-ops for users without a configured username
        (
            "20,50 * * * *",
            "app.letterboxd.sync_letterboxd_feeds",
            900,
            "Syncing Letterboxd diaries",
        ),
        # Discover provider-catalog films for the recommendation
        # universe (#250): enumerate subscribed providers' catalogs,
        # diff against the ever-seen sets, and turn a bounded batch of
        # verified, well-scoring arrivals into file-less records. At
        # 0:45 so the records' TMDB refreshes land before the 1:45
        # recompute scores them
        (
            "45 0 * * *",
            "app.provider_catalog.refresh_provider_catalogs",
            3600,
            "Discovering provider-catalog films for recommendations",
        ),
        # Recompute per-user film recommendations nightly, after the log
        # rotation and backup windows
        (
            "45 1 * * *",
            "app.recommendations.recompute_recommendations",
            3600,
            "Recomputing film recommendations",
        ),
        # Criterion24/7 now-playing heartbeat: the poller is
        # self-scheduling (it re-enqueues at each film's end under a
        # deterministic job id), so the cron checks the chain's pulse
        # and scrapes only when the chain has died — never rescanning
        # the currently-showing film on the half-hour
        (
            "7,37 * * * *",
            "app.criterion_now.heartbeat_criterion_now",
            300,
            "Checking the Criterion24/7 poller's pulse",
        ),
        # Refresh the leaving-Criterion set monthly
        (
            "30 3 1 * *",
            "app.leaving_criterion.refresh_leaving_criterion",
            3600,
            "Refreshing the leaving-Criterion film set",
        ),
        # Scrape and diff each provider's newly-added feed daily
        # (#246) — the snapshot diff stamps the first-seen dates the
        # discovery shelves and "added" badges read, so the cadence
        # is what makes "newly added" mean something. In the 4:30
        # refresh's shadow so the TMDB-heavy window stays contiguous
        (
            "0 5 * * *",
            "app.newly_added.refresh_newly_added",
            1800,
            "Refreshing the newly-added streaming feeds",
        ),
        # Refresh film awards from Wikidata weekly, early Monday
        (
            "15 4 * * 1",
            "app.awards.refresh_awards",
            7200,
            "Refreshing film awards from Wikidata",
        ),
        # Rebuild the streaming rail nightly, after the 1:45 profiles
        (
            "15 2 * * *",
            "app.streaming_rail.recompute_streaming_rail",
            3600,
            "Recomputing the streaming rail",
        ),
        # Pre-warm estimate payloads nightly, after the recompute has
        # dropped the overlays and the rail's enrichments are cached —
        # affinity people's careers plus the TMDB charts, pre-scored
        # into the tmdb overlay so tiles paint without waiting
        (
            "45 2 * * *",
            "app.estimate_warm.warm_estimates",
            3600,
            "Pre-warming estimate payloads",
        ),
        # Top up and rotate the Name That Frame pool nightly;
        # the coordinator queues per-film extractions on the serial
        # transcode lane, so a big backfill can't crowd anything out
        (
            "5 3 * * *",
            "app.frames.refresh_frame_pool_task",
            3600,
            "Refreshing the Name That Frame pool",
        ),
        # Sweep TMDB's change feeds and refresh just the library records
        # that were edited since the last sweep — TMDB stores no
        # last-updated stamp, so the changes lists are how edits reach
        # the library without a bulk re-fetch. Adjacent to the 3:45
        # in-production sweep (whose coverage it deliberately excludes)
        # so the TMDB-heavy window stays contiguous
        (
            "35 3 * * *",
            "app.tmdb_changes.refresh_changed_records",
            1800,
            "Refreshing TMDB-changed records",
        ),
        # Re-fetch episode data for in-production TV series nightly,
        # clear of the 3:05 frame pool and 3:25 Plex title windows
        (
            "45 3 * * *",
            "app.tmdb_refresh.refresh_in_production_tv",
            3600,
            "Refreshing in-production TV series",
        ),
        # Refresh every film's streaming availability nightly, last in
        # the TMDB-heavy window: the watchlist, Criterion catalog, and
        # filmography pages render from this cache and never fetch
        # inline, so it has to be full before the day starts
        (
            "30 4 * * *",
            "app.streaming.refresh_availability",
            3600,
            "Refreshing streaming availability",
        ),
        # Diff watchlisted films' availability against last night's
        # snapshot and alert the watchers (#156/#230): badge records
        # for everyone, one digest email per opted-in user. After the
        # 4:30 refresh so the diff reads tonight's cache, not
        # yesterday's
        (
            "30 5 * * *",
            "app.availability_alerts.notify_watchlist_availability",
            1800,
            "Checking watchlisted films for new availability",
        ),
    ]

    # Download files restored from Glacier: poll SQS hourly, offset from
    # the import sweep so the maintenance worker isn't handed both at once

    if config.get("AWS_SQS_URL"):
        table.append(
            (
                "30 * * * *",
                "app.videos.sqs_retrieve_task",
                7200,
                "Polling AWS SQS for files to download",
            )
        )

    # Poll Plex watch history every 15 minutes as the self-healing
    # backstop to the real-time webhook

    if config.get("PLEX_URL") and config.get("PLEX_TOKEN"):
        table.append(
            (
                "*/15 * * * *",
                "app.videos.plex_history_poll",
                900,
                "Polling Plex for watch history",
            )
        )

        # Title Plex episodes from filename-carried titles —
        # replaces the external write-Plex's-SQLite-directly cron

        table.append(
            (
                "25 3 * * *",
                "app.plex_titles.sync_plex_episode_titles",
                1800,
                "Syncing episode titles into Plex",
            )
        )

        # Scan Plex libraries and empty their trashes, guarded per
        # section on every declared location being mounted — replaces
        # the external curl cron (a scan against a dropped mount plus
        # emptyTrash rebuilds the library from scratch)

        table.append(
            (
                "3,18,33,48 * * * *",
                "app.plex_library.refresh_plex_libraries",
                600,
                "Refreshing Plex libraries",
            )
        )

        # Re-verify TMDB episode titles against Plex's agent titles
        # nightly, after the 3:45 in-production TV refresh

        table.append(
            (
                "5 4 * * *",
                "app.tv_validation.validate_tv_titles",
                1800,
                "Validating TV episode titles against Plex",
            )
        )

    # Sync episode data from Sonarr's TVDB metadata for every series
    # it manages (#162) — Sonarr refreshes itself on a ~12-hour
    # cadence, so a nightly read stays current

    if config.get("SONARR_URL") and config.get("SONARR_API_KEY"):
        table.append(
            (
                "35 4 * * *",
                "app.sonarr_episodes.sync_sonarr_episodes",
                1800,
                "Syncing episode data from Sonarr",
            )
        )

    # Reconcile the Plex and Fitzflix watchlists both ways; the
    # account-level discover API needs only the token, not the server

    if config.get("PLEX_TOKEN"):
        table.append(
            (
                "10,40 * * * *",
                "app.plex_watchlist.sync_plex_watchlist",
                900,
                "Syncing the Plex watchlist",
            )
        )

    return [
        {
            "cron": cron_string,
            "func": func,
            "queue": "fitzflix-maintenance",
            "timeout": timeout,
            "description": description,
        }
        for cron_string, func, timeout, description in table
    ]


def get_app(watch_import_dir=False):
    """Return this process's application instance, creating it if needed.

    Task modules resolve their app through this instead of calling
    create_app() at import time, so importing them from a process that
    already has an application (e.g. the web process importing app.videos)
    doesn't build a second one. watch_import_dir only matters on the call
    that actually creates the instance (supervisor.py's eager startup one).
    """

    global _app
    if _app is None:
        _app = create_app(watch_import_dir=watch_import_dir)
    return _app


def check_config(app):
    """Warn at startup about config values that would make tasks fail later.

    Warnings only: a misconfigured optional feature shouldn't stop the app
    from serving the rest of the library.
    """

    for key in (
        "ATOMICPARSLEY_BIN",
        "HANDBRAKE_BIN",
        "MKVMERGE_BIN",
        "MKVPROPEDIT_BIN",
        "FFMPEG_BIN",
        "MYSQLDUMP_BIN",
        "SUPERVISORCTL_BIN",
    ):
        path = app.config[key]
        if not (os.path.isfile(path) and os.access(path, os.X_OK)):
            app.logger.warning(f"{key} '{path}' is not an executable file")

    for key in (
        "MEDIA_LOCATION",
        "LIBRARY_DIR",
        "MOVIE_LIBRARY",
        "TV_LIBRARY",
        "REJECTS_DIR",
        "TRANSCODES_DIR",
    ):
        path = app.config[key]
        if not os.path.isdir(path):
            app.logger.warning(f"{key} '{path}' is not an existing directory")

    preset_file = app.config["HANDBRAKE_PRESET_FILE"]
    if preset_file:
        if not os.path.isfile(preset_file):
            app.logger.warning(
                f"HANDBRAKE_PRESET_FILE '{preset_file}' does not exist, "
                f"transcodes will not use the custom preset"
            )
        else:
            try:
                with open(preset_file) as f:
                    presets = [
                        preset.get("PresetName")
                        for preset in json.load(f).get("PresetList", [])
                    ]
            except (OSError, ValueError):
                app.logger.warning(
                    f"HANDBRAKE_PRESET_FILE '{preset_file}' is not a valid "
                    f"HandBrake preset export"
                )
            else:
                if app.config["HANDBRAKE_PRESET"] not in presets:
                    app.logger.warning(
                        f"HANDBRAKE_PRESET '{app.config['HANDBRAKE_PRESET']}' "
                        f"is not in HANDBRAKE_PRESET_FILE (contains {presets})"
                    )

    aws_missing = [
        key
        for key in ("AWS_BUCKET", "AWS_ACCESS_KEY", "AWS_SECRET_KEY")
        if not app.config[key]
    ]
    if aws_missing and len(aws_missing) < 3:
        app.logger.warning(
            f"AWS is partially configured, uploads will fail: missing {aws_missing}"
        )
    if app.config["ARCHIVE_ORIGINAL_MEDIA"] and aws_missing:
        app.logger.warning(
            f"ARCHIVE_ORIGINAL_MEDIA is set, but uploads will fail: "
            f"missing {aws_missing}"
        )

    for service in ("SONARR", "RADARR"):
        if bool(app.config[f"{service}_URL"]) != bool(app.config[f"{service}_API_KEY"]):
            app.logger.warning(
                f"{service}_URL and {service}_API_KEY must both be set "
                f"for {service.capitalize()} requests to succeed"
            )

    if app.config["MAIL_SERVER"] and not (
        app.config["SERVER_EMAIL"] and app.config["ADMIN_EMAIL"]
    ):
        app.logger.warning(
            "MAIL_SERVER is set, but sending will fail without "
            "SERVER_EMAIL and ADMIN_EMAIL (or MAIL_USERNAME as their fallback)"
        )

    if not app.config["TMDB_API_KEY"]:
        app.logger.warning(
            "TMDB_API_KEY is not set, TMDB metadata and poster lookups will be skipped"
        )


def create_app(config_class=Config, watch_import_dir=False):
    """Application factory: build and fully wire an app instance.

    watch_import_dir starts the import-directory filesystem observer;
    supervisor.py enables it for the import-program workers alone.
    """

    class MyHandler(FileSystemEventHandler):
        """Handlers for watchdog to fire when filesystem events occur."""

        def process_new_file(self, path):
            """Queue a localization for a file newly visible in the import directory."""

            # Create redis lock using the filename, to prevent multiple workers
            # from grabbing the same file at once

            lock = app.lock_manager.lock(os.path.basename(path), 30000)
            if lock:
                job_queue = []
                localization_tasks_running = StartedJobRegistry(
                    "fitzflix-import", connection=app.redis
                )
                job_queue.extend(localization_tasks_running.get_job_ids())
                job_queue.extend(app.import_queue.job_ids)
                app.logger.debug(job_queue)

                # Use the file basename as the job id, so we can see if this file is
                # already in the job_queue, and only add it if it doesn't already exist

                if safe_job_id(os.path.basename(path)) not in job_queue:
                    app.logger.info(
                        f"'{os.path.basename(path)}' Found in import directory"
                    )
                    app.import_queue.enqueue(
                        "app.videos.localization_task",
                        args=(path,),
                        job_timeout=app.config["LOCALIZATION_TASK_TIMEOUT"],
                        description=f"'{os.path.basename(path)}'",
                        job_id=safe_job_id(os.path.basename(path)),
                    )

                app.lock_manager.unlock(lock)

        def on_moved(self, event):
            """Process a file when it's moved within the watched directory."""

            # Process only those moved files that were previously invisible

            if (
                not importable_basename(os.path.basename(event.src_path))
                and importable_basename(os.path.basename(event.dest_path))
                and os.path.isfile(event.dest_path)
            ):
                self.process_new_file(event.dest_path)

        def on_created(self, event):
            """Process a file when it appears in the watched directory."""

            # Process only those files that are not invisible or transient

            if importable_basename(os.path.basename(event.src_path)) and os.path.isfile(
                event.src_path
            ):
                self.process_new_file(event.src_path)

        def on_any_event(self, event):
            """Process on any filesystem event."""

            app.logger.debug(event)

    app = Flask(__name__)

    # Build the application configuration from the config.py file

    app.config.from_object(config_class)
    app.jinja_env.filters["quote_plus"] = lambda u: quote_plus(u)

    from app.richtext import review_html

    app.jinja_env.filters["review_html"] = review_html

    # The poster tiles' action forms render their own csrf inputs
    # without a form object threaded through every gallery route —
    # the same name CSRFProtect would register, minus its enforcement

    from flask_wtf.csrf import generate_csrf

    app.jinja_env.globals["csrf_token"] = generate_csrf

    # The built-in SECRET_KEY fallback lets anyone forge session cookies and
    # password-reset tokens, so it's only acceptable in debug mode

    if app.config["SECRET_KEY"] == "fitzflix-secret" and not app.debug:
        raise RuntimeError(
            "SECRET_KEY is not set in .env; refusing to start with the "
            "built-in default outside debug mode"
        )

    # Configure the Redis connection and queues. TrackedQueue leaves
    # per-file trail entries at enqueue time; jobs that aren't
    # pipeline stages record nothing

    from app.pipeline import TrackedQueue

    app.redis = Redis.from_url(app.config["REDIS_URL"])
    app.maintenance_queue = TrackedQueue("fitzflix-maintenance", connection=app.redis)
    app.sql_queue = TrackedQueue("fitzflix-sql", connection=app.redis)
    app.request_queue = TrackedQueue("fitzflix-user-request", connection=app.redis)
    app.import_queue = TrackedQueue("fitzflix-import", connection=app.redis)
    app.transcode_queue = TrackedQueue("fitzflix-transcode", connection=app.redis)
    app.file_queue = TrackedQueue("fitzflix-file-operation", connection=app.redis)

    # Configure the Redis redlock manager

    app.lock_manager = Redlock([app.redis])

    # Initialize application components

    db.init_app(app)
    migrate.init_app(app, db)
    login.init_app(app)
    mail.init_app(app)
    moment.init_app(app)

    # Needed to be able to set X- headers by web server to configure https protocol, etc.

    ProxyFix(app, x_proto=1, x_host=1, x_prefix=1)

    from app import models

    # Build blueprints

    from app.errors import bp as errors_bp

    app.register_blueprint(errors_bp)

    from app.auth import bp as auth_bp

    app.register_blueprint(auth_bp, url_prefix="/auth")

    from app.main import bp as main_bp

    app.register_blueprint(main_bp)

    from app.api import bp as api_bp

    app.register_blueprint(api_bp, url_prefix="/api")

    # Credentials never reach the log, whichever handler writes it; see
    # app.redaction. Installed in every mode so tests cover it too

    from app.redaction import install as install_redaction

    install_redaction(app.logger, app.config)

    if not app.debug:
        # Configure how to handle logs when running in production mode

        # Both Flask instances in this package share the "app" logger, so only
        # attach the mail and file handlers if an earlier create_app() hasn't

        if app.config["MAIL_SERVER"] and not any(
            isinstance(handler, SMTPHandler) for handler in app.logger.handlers
        ):
            # Email any exceptions

            auth = None
            if app.config["MAIL_USERNAME"] or app.config["MAIL_PASSWORD"]:
                auth = (app.config["MAIL_USERNAME"], app.config["MAIL_PASSWORD"])

            secure = None
            if app.config["MAIL_USE_TLS"]:
                secure = ()

            mail_handler = SMTPHandler(
                mailhost=(app.config["MAIL_SERVER"], app.config["MAIL_PORT"]),
                fromaddr=app.config["SERVER_EMAIL"],
                toaddrs=app.config["ADMIN_EMAIL"],
                subject="Fitzflix Failure",
                credentials=auth,
                secure=secure,
            )
            mail_handler.setLevel(logging.ERROR)
            app.logger.addHandler(mail_handler)

        os.makedirs(os.path.dirname(app.config["LOG_FILE"]), exist_ok=True)

        if not any(
            isinstance(handler, logging.FileHandler) for handler in app.logger.handlers
        ):
            # WatchedFileHandler notices when the rotate_logs maintenance task
            # renames the log file, and reopens it before the next write

            file_handler = WatchedFileHandler(app.config["LOG_FILE"])
            file_handler.setFormatter(
                logging.Formatter(
                    "%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]"
                )
            )
            file_handler.setLevel(logging.INFO)
            app.logger.addHandler(file_handler)

        app.logger.setLevel(logging.INFO)
        app.logger.info("Fitzflix startup")

    # Warn about configuration problems that would make tasks fail later

    check_config(app)

    # Create the import and local staging directories

    os.makedirs(app.config["IMPORT_DIR"], exist_ok=True)
    try:
        os.makedirs(app.config["STAGING_DIR"], exist_ok=True)
    except OSError:
        # An unavailable staging volume shouldn't stop the app; localization
        # falls back to processing files in place
        app.logger.warning(
            f"STAGING_DIR '{app.config['STAGING_DIR']}' could not be created"
        )

    # Watch the import directory for file changes — but only when asked:
    # supervisor.py enables this for the import-program workers alone, so
    # the other workers and the web process don't each poll the (network-
    # mounted) directory for no benefit. The polling emitter shuts itself
    # down permanently on any OSError from that mount, so a keeper thread
    # rebuilds the observer whenever it dies, then sweeps the directory for
    # anything that arrived while blind

    if watch_import_dir:
        event_handler = MyHandler()

        def start_observer():
            observer = PollingObserver()
            # Only created/moved file events matter to the handler, so
            # the emitter filters everything else before dispatch
            observer.schedule(
                event_handler,
                path=app.config["IMPORT_DIR"],
                recursive=False,
                event_filter=[FileCreatedEvent, FileMovedEvent],
            )
            observer.start()
            return observer

        def enqueue_import_sweep():
            enqueue_import_scan(app.import_queue)

        def write_observer_heartbeat():
            # Expiring per-process heartbeat: the admin health card counts
            # these keys to show how many processes are actually watching the
            # import directory, and a dead or wedged process simply stops
            # refreshing

            try:
                app.redis.set(
                    f"fitzflix:observer:{os.getpid()}", int(time.time()), ex=180
                )
            except Exception:
                pass

        def keep_observer_alive(observer):
            while True:
                time.sleep(60)
                try:
                    # The failed emitter thread stays in the emitters set after
                    # it dies, so check liveness rather than presence

                    healthy = (
                        observer.is_alive()
                        and observer.emitters
                        and all(emitter.is_alive() for emitter in observer.emitters)
                    )
                    if not healthy:
                        app.logger.warning(
                            "Import directory observer died; "
                            "rebuilding it and sweeping for missed files"
                        )
                        try:
                            observer.stop()
                        except Exception:
                            pass
                        observer = start_observer()
                        enqueue_import_sweep()
                    write_observer_heartbeat()
                except Exception:
                    app.logger.error(traceback.format_exc())

        observer = start_observer()
        write_observer_heartbeat()
        threading.Thread(
            target=keep_observer_alive,
            args=(observer,),
            daemon=True,
            name="import-observer-keeper",
        ).start()

        # Process anything that arrived while the application wasn't watching

        enqueue_import_sweep()

    # The first application created becomes this process's instance for
    # modules that resolve their app through get_app()

    global _app
    if _app is None:
        _app = app

    return app
