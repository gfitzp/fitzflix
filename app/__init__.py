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
    """Flatten a string into the job-id character set that rq 2 permits.

    Job ids carry the dedup and retry-replacement semantics. Examples are
    file basenames and deterministic retry:task:target ids. Thus, this
    function maps the disallowed characters to underscores. It does not
    discard the ids.
    """

    return re.sub(r"[^A-Za-z0-9_-]", "_", value)


def importable_basename(basename):
    """Return True if an import-directory name is a finished file worth a sweep.

    Hidden names and the intermediate artifacts of transfer tools must
    never become localization jobs (#244: a sweep put a transient
    `.mkv.staged` copy into a job of its own, and the restart that
    killed the job left a phantom File Activity row). When the transfer
    completes, the promotion of the artifact to its real name causes its
    own filesystem event.
    """

    if basename.startswith("."):
        return False
    return not basename.lower().endswith(
        (".staged", ".partial", ".part", ".tmp", ".filepart", ".crdownload")
    )


def retry_job_id(task, target, *attempt):
    """Name a retry job after the attempt that it schedules, not only its target.

    rq saves the payload of a job again when the job returns. Thus, a
    retry scheduled under the id of the job that schedules it is
    overwritten with the stale kwargs of that job when the job
    completes. The attempt counters never advance. The chain retries
    forever and never gives up. This function puts the counters into
    the id. Thus, a deferred attempt keeps the replacement semantics,
    and each new attempt gets an id of its own.
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
    """Return the table of recurring jobs, with every scheduled task as a row.

    The table includes the config-dependent entries only when they are
    configured. The scheduler.py process registers these rows with the
    native CronScheduler of rq. Nothing else registers cron jobs. Thus,
    the table is authoritative on every scheduler start.
    """

    table = [
        # Rotate the application log daily at midnight.
        (
            "0 0 * * *",
            "app.maintenance.rotate_logs",
            54000,
            "Rotating application logs",
        ),
        # Back up the database nightly. The media files are archived at
        # AWS. But the database itself exists only on this machine. Thus,
        # the task also copies each dump to the S3 bucket.
        (
            "30 0 * * *",
            "app.maintenance.backup_database",
            3600,
            "Backing up the database",
        ),
        # Sweep the import directory hourly as a safety net. The
        # filesystem observer can miss an arrival.
        (
            "0 * * * *",
            "app.videos.manual_import_task",
            3600,
            "Scanning import directory for files",
        ),
        # Monthly restore drill: prove that the newest offsite dump restores.
        (
            "0 4 1 * *",
            "app.maintenance.restore_drill",
            3600,
            "Verifying the offsite database backup restores",
        ),
        # Refresh the Criterion spine numbers from Wikidata monthly. The
        # 18th gives Wikidata time to catch up with the mid-month reveal.
        (
            "0 3 18 * *",
            "app.videos.refresh_criterion_collection_info",
            3600,
            "Refreshing Criterion Collection information",
        ),
        # Sweep orphaned partial files weekly, after the backup window.
        (
            "0 1 * * 0",
            "app.maintenance.cleanup_orphaned_files",
            3600,
            "Cleaning up orphaned partial files",
        ),
        # Ask every library file if the NAS still holds its handle. The
        # state is invisible until the close of an upload fails. Thus,
        # the sweep asks nightly instead of waiting for a surprise. 05:00
        # is after the other maintenance of the night and far outside the
        # viewing hours. It is approximately 21k opens over SMB, about 1
        # minute of work, and Plex reads the same shares.
        (
            "0 5 * * *",
            "app.maintenance.smb_handle_sweep",
            3600,
            "Probing library files for lost SMB handles",
        ),
        ("*/10 * * * *", "app.maintenance.health_probe", 600, "Probing system health"),
        # Sync the Letterboxd diaries from the RSS feed of each user. The
        # task does nothing for the users without a configured username.
        (
            "20,50 * * * *",
            "app.letterboxd.sync_letterboxd_feeds",
            900,
            "Syncing Letterboxd diaries",
        ),
        # Discover provider-catalog films for the recommendation universe
        # (#250). The task lists the catalogs of the subscribed providers.
        # It compares them with the ever-seen sets. It turns a bounded
        # batch of verified, well-scoring arrivals into file-less
        # records. It runs at 00:45. Thus, the TMDB refreshes of the
        # records complete before the 01:45 recompute scores them.
        (
            "45 0 * * *",
            "app.provider_catalog.refresh_provider_catalogs",
            3600,
            "Discovering provider-catalog films for recommendations",
        ),
        # Recompute the per-user film recommendations nightly, after the
        # log rotation and backup windows.
        (
            "45 1 * * *",
            "app.recommendations.recompute_recommendations",
            3600,
            "Recomputing film recommendations",
        ),
        # Criterion24/7 now-playing heartbeat. The poller schedules
        # itself. It enqueues itself again at the end of each film under
        # a deterministic job id. Thus, the cron checks the pulse of the
        # chain and scrapes only when the chain has died. It never scans
        # the film that shows now again on the half hour.
        (
            "7,37 * * * *",
            "app.criterion_now.heartbeat_criterion_now",
            300,
            "Checking the Criterion24/7 poller's pulse",
        ),
        # Refresh the leaving-Criterion set. The task tries daily. But
        # it does nothing while the departure of the stored set is still
        # ahead. Thus, this is really a monthly scrape that retries each
        # morning until the page of the new month appears. It survives a
        # down server or an unpublished page. The page was not up on the
        # night before 2026-09-01. Thus, a single attempt in the small
        # hours of the 1st would empty the shelf for the whole month.
        (
            "0 6 * * *",
            "app.leaving_criterion.refresh_leaving_criterion",
            3600,
            "Refreshing the leaving-Criterion film set",
        ),
        # Scrape and compare the newly-added feed of each provider daily
        # (#246). The snapshot comparison stamps the first-seen dates
        # that the discovery shelves and the "added" badges read. Thus,
        # the daily cadence is what gives "newly added" a meaning. It
        # runs next to the 04:30 refresh. Thus, the TMDB-heavy window
        # stays contiguous.
        (
            "0 5 * * *",
            "app.newly_added.refresh_newly_added",
            1800,
            "Refreshing the newly-added streaming feeds",
        ),
        # Month-start catch-up for both Criterion feeds. The Channel
        # publishes the pages of the new month at some time on the 1st.
        # On 2026-09-01, that was after the 05:00 and 06:00 morning runs.
        # Thus, check again at noon instead of the next morning. The
        # newly-added comparison runs again without harm. The currency
        # guard of the leaving task makes its noon pass free when the
        # 06:00 run succeeded.
        (
            "0 12 1 * *",
            "app.newly_added.refresh_newly_added",
            1800,
            "Month-start check of the newly-added streaming feeds",
        ),
        (
            "0 12 1 * *",
            "app.leaving_criterion.refresh_leaving_criterion",
            3600,
            "Month-start check of the leaving-Criterion film set",
        ),
        # Refresh the film awards from Wikidata weekly, early on Monday.
        (
            "15 4 * * 1",
            "app.awards.refresh_awards",
            7200,
            "Refreshing film awards from Wikidata",
        ),
        # Rebuild the streaming rail nightly, after the 01:45 profiles.
        (
            "15 2 * * *",
            "app.streaming_rail.recompute_streaming_rail",
            3600,
            "Recomputing the streaming rail",
        ),
        # Pre-warm the estimate payloads nightly. This runs after the
        # recompute dropped the overlays and the enrichments of the rail
        # are cached. The payloads are the careers of the affinity people
        # plus the TMDB charts. The task scores them into the tmdb
        # overlay. Thus, the tiles paint without a wait.
        (
            "45 2 * * *",
            "app.estimate_warm.warm_estimates",
            3600,
            "Pre-warming estimate payloads",
        ),
        # Fill and rotate the Name That Frame pool nightly. The
        # coordinator queues the per-film extractions on the serial
        # transcode lane. Thus, a big backfill cannot crowd out other
        # work.
        (
            "5 3 * * *",
            "app.frames.refresh_frame_pool_task",
            3600,
            "Refreshing the Name That Frame pool",
        ),
        # Sweep the change feeds of TMDB and refresh only the library
        # records that were edited after the last sweep. TMDB stores no
        # last-updated stamp. Thus, the changes lists are how edits reach
        # the library without a bulk fetch. The task runs next to the
        # 03:45 in-production sweep. It deliberately excludes the
        # coverage of that sweep. Thus, the TMDB-heavy window stays
        # contiguous.
        (
            "35 3 * * *",
            "app.tmdb_changes.refresh_changed_records",
            1800,
            "Refreshing TMDB-changed records",
        ),
        # Fetch the episode data for in-production TV series again
        # nightly, clear of the 03:05 frame pool and 03:25 Plex title
        # windows.
        (
            "45 3 * * *",
            "app.tmdb_refresh.refresh_in_production_tv",
            3600,
            "Refreshing in-production TV series",
        ),
        # Refresh the streaming availability of every film nightly, last
        # in the TMDB-heavy window. The watchlist, Criterion catalog, and
        # filmography pages render from this cache and never fetch
        # inline. Thus, the cache must be full before the day starts.
        (
            "30 4 * * *",
            "app.streaming.refresh_availability",
            3600,
            "Refreshing streaming availability",
        ),
        # Compare the availability of the watchlisted films with the
        # snapshot of the previous night and alert the watchers
        # (#156/#230). The task writes badge records for everyone and 1
        # digest email per opted-in user. It runs after the 04:30
        # refresh. Thus, the comparison reads the cache of tonight, not
        # of yesterday.
        (
            "30 5 * * *",
            "app.availability_alerts.notify_watchlist_availability",
            1800,
            "Checking watchlisted films for new availability",
        ),
    ]

    # Download the files restored from Glacier. Poll SQS hourly, offset
    # from the import sweep. Thus, the maintenance worker does not get
    # both at one time.

    if config.get("AWS_SQS_URL"):
        table.append(
            (
                "30 * * * *",
                "app.videos.sqs_retrieve_task",
                7200,
                "Polling AWS SQS for files to download",
            )
        )

    # Poll the Plex watch history every 15 minutes as the self-healing
    # backstop to the real-time webhook.

    if config.get("PLEX_URL") and config.get("PLEX_TOKEN"):
        table.append(
            (
                "*/15 * * * *",
                "app.videos.plex_history_poll",
                900,
                "Polling Plex for watch history",
            )
        )

        # Title the Plex episodes from the titles in the filenames. This
        # replaces the external cron that wrote the SQLite of Plex
        # directly.

        table.append(
            (
                "25 3 * * *",
                "app.plex_titles.sync_plex_episode_titles",
                1800,
                "Syncing episode titles into Plex",
            )
        )

        # Scan the Plex libraries and empty their trashes. A guard per
        # section requires that every declared location is mounted. This
        # replaces the external curl cron. A scan against a dropped mount
        # plus emptyTrash rebuilds the library from nothing.

        table.append(
            (
                "3,18,33,48 * * * *",
                "app.plex_library.refresh_plex_libraries",
                600,
                "Refreshing Plex libraries",
            )
        )

    # Reconcile the Plex and Fitzflix watchlists in both directions. The
    # account-level discover API needs only the token, not the server.

    if config.get("PLEX_TOKEN"):
        table.append(
            (
                "10,40 * * * *",
                "app.plex_watchlist.sync_plex_watchlist",
                900,
                "Syncing the Plex watchlist",
            )
        )

    # Rebuild the virtual DVR channel lineups nightly (#182). The task
    # rotates them the same way as the landing-page shelves rotate. The
    # per-file duration cache makes every build after the first
    # appearance of a file almost free. The task runs after the 04:30
    # availability refresh. Thus, the Criterion channel reads the cache
    # of tonight, not of yesterday. It also runs after the 06:00
    # leaving-Criterion attempt.

    if config.get("DVR_TOKEN"):
        table.append(
            (
                "30 6 * * *",
                "app.dvr.build_channel_lineups",
                3600,
                "Building virtual DVR channel lineups",
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
    """Return the application instance of this process, and create it if necessary.

    Task modules resolve their app through this function instead of a
    call to create_app() at import time. Thus, an import of them from a
    process that already has an application does not build a second one.
    An example is the web process that imports app.videos.
    watch_import_dir is important only on the call that creates the
    instance. That is the eager startup call of supervisor.py.
    """

    global _app
    if _app is None:
        _app = create_app(watch_import_dir=watch_import_dir)
    return _app


def check_config(app):
    """Warn at startup about config values that would make tasks fail later.

    This function only warns. An optional feature with a bad
    configuration must not stop the app from serving the rest of the
    library.
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
    """Build and fully connect an app instance.

    This is the application factory. watch_import_dir starts the
    filesystem observer of the import directory. supervisor.py enables
    it for the import-program workers only.
    """

    class MyHandler(FileSystemEventHandler):
        """Hold the handlers that watchdog runs when filesystem events occur."""

        def process_new_file(self, path):
            """Queue a localization for a file that appeared in the import directory."""

            # Create a Redis lock with the filename. This prevents multiple
            # workers from getting the same file at one time.

            lock = app.lock_manager.lock(os.path.basename(path), 30000)
            if lock:
                job_queue = []
                localization_tasks_running = StartedJobRegistry(
                    "fitzflix-import", connection=app.redis
                )
                job_queue.extend(localization_tasks_running.get_job_ids())
                job_queue.extend(app.import_queue.job_ids)
                app.logger.debug(job_queue)

                # Use the file basename as the job id. Then the handler can
                # see if this file is already in the job_queue. It adds the
                # file only if the file is not there.

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
            """Process a file when it is moved in the watched directory."""

            # Process only the moved files that were invisible before.

            if (
                not importable_basename(os.path.basename(event.src_path))
                and importable_basename(os.path.basename(event.dest_path))
                and os.path.isfile(event.dest_path)
            ):
                self.process_new_file(event.dest_path)

        def on_created(self, event):
            """Process a file when it appears in the watched directory."""

            # Process only the files that are not invisible or transient.

            if importable_basename(os.path.basename(event.src_path)) and os.path.isfile(
                event.src_path
            ):
                self.process_new_file(event.src_path)

        def on_any_event(self, event):
            """Log any filesystem event."""

            app.logger.debug(event)

    app = Flask(__name__)

    # Build the application configuration from the config.py file.

    app.config.from_object(config_class)
    app.jinja_env.filters["quote_plus"] = lambda u: quote_plus(u)

    from app.richtext import review_html

    app.jinja_env.filters["review_html"] = review_html

    # The action forms of the poster tiles render their own csrf inputs.
    # They do not need a form object passed through every gallery route.
    # This is the same name that CSRFProtect would register, without its
    # enforcement.

    from flask_wtf.csrf import generate_csrf

    app.jinja_env.globals["csrf_token"] = generate_csrf

    # The built-in SECRET_KEY fallback lets anyone forge session cookies
    # and password-reset tokens. Thus, it is acceptable only in debug
    # mode.

    if app.config["SECRET_KEY"] == "fitzflix-secret" and not app.debug:
        raise RuntimeError(
            "SECRET_KEY is not set in .env; refusing to start with the "
            "built-in default outside debug mode"
        )

    # Configure the Redis connection and the queues. TrackedQueue writes
    # per-file trail entries at enqueue time. Jobs that are not pipeline
    # stages record nothing.

    from app.pipeline import TrackedQueue

    app.redis = Redis.from_url(app.config["REDIS_URL"])
    app.maintenance_queue = TrackedQueue("fitzflix-maintenance", connection=app.redis)
    app.sql_queue = TrackedQueue("fitzflix-sql", connection=app.redis)
    app.request_queue = TrackedQueue("fitzflix-user-request", connection=app.redis)
    app.import_queue = TrackedQueue("fitzflix-import", connection=app.redis)
    app.transcode_queue = TrackedQueue("fitzflix-transcode", connection=app.redis)
    app.file_queue = TrackedQueue("fitzflix-file-operation", connection=app.redis)

    # Configure the Redis redlock manager.

    app.lock_manager = Redlock([app.redis])

    # Initialize the application components.

    db.init_app(app)
    migrate.init_app(app, db)
    login.init_app(app)
    mail.init_app(app)
    moment.init_app(app)

    # This lets the web server set the X- headers that configure the
    # https protocol and related values.

    ProxyFix(app, x_proto=1, x_host=1, x_prefix=1)

    from app import models

    # Register the blueprints.

    from app.errors import bp as errors_bp

    app.register_blueprint(errors_bp)

    from app.auth import bp as auth_bp

    app.register_blueprint(auth_bp, url_prefix="/auth")

    from app.main import bp as main_bp

    app.register_blueprint(main_bp)

    from app.api import bp as api_bp

    app.register_blueprint(api_bp, url_prefix="/api")

    # Credentials never reach the log, with any handler that writes it.
    # See app.redaction. Fitzflix installs this in every mode. Thus, the
    # tests cover it too.

    from app.redaction import install as install_redaction

    install_redaction(app.logger, app.config)

    if not app.debug:
        # Configure the log handling for production mode.

        # Both Flask instances in this package share the "app" logger.
        # Thus, attach the mail and file handlers only if an earlier
        # create_app() did not.

        if app.config["MAIL_SERVER"] and not any(
            isinstance(handler, SMTPHandler) for handler in app.logger.handlers
        ):
            # Send an email for each exception.

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
            # WatchedFileHandler sees when the rotate_logs maintenance
            # task renames the log file. It opens the file again before
            # the next write.

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

    # Warn about the configuration problems that would make tasks fail
    # later.

    check_config(app)

    # Create the import directory and the local staging directory.

    os.makedirs(app.config["IMPORT_DIR"], exist_ok=True)
    try:
        os.makedirs(app.config["STAGING_DIR"], exist_ok=True)
    except OSError:
        # An unavailable staging volume must not stop the app. Then the
        # localization processes the files in place.
        app.logger.warning(
            f"STAGING_DIR '{app.config['STAGING_DIR']}' could not be created"
        )

    # Watch the import directory for file changes, but only when asked.
    # supervisor.py enables this for the import-program workers only.
    # Thus, the other workers and the web process do not each poll the
    # network-mounted directory with no benefit. The polling emitter
    # stops itself permanently on any OSError from that mount. Thus, a
    # keeper thread rebuilds the observer each time it dies. Then the
    # thread sweeps the directory for the files that arrived while the
    # observer was blind.

    if watch_import_dir:
        event_handler = MyHandler()

        def start_observer():
            observer = PollingObserver()
            # Only the created and moved file events are important to
            # the handler. Thus, the emitter filters all other events
            # before dispatch.
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
            # This is a per-process heartbeat that expires. The admin
            # health card counts these keys to show how many processes
            # watch the import directory. A dead or wedged process stops
            # the refresh.

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
                    # The failed emitter thread stays in the emitters set
                    # after it dies. Thus, check if it is alive, not if it
                    # is present.

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

        # Process the files that arrived while the application did not
        # watch.

        enqueue_import_sweep()

    # The first application created becomes the instance of this process
    # for the modules that resolve their app through get_app().

    global _app
    if _app is None:
        _app = app

    return app
