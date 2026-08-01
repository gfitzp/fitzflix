import json
import logging
import os

from logging.handlers import SMTPHandler, WatchedFileHandler
from urllib.parse import quote_plus

import rq

from redis import Redis
from redlock import Redlock
from rq.registry import StartedJobRegistry
from rq_scheduler import Scheduler
from watchdog.events import FileSystemEventHandler
from watchdog.observers.polling import PollingObserver
from werkzeug.middleware.proxy_fix import ProxyFix

from config import Config
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_login import LoginManager
from flask_mail import Mail
from flask_bootstrap import Bootstrap
from flask_moment import Moment

db = SQLAlchemy()
migrate = Migrate(compare_type=True)
login = LoginManager()
login.login_view = "auth.login"
mail = Mail()
bootstrap = Bootstrap()
moment = Moment()

_app = None


def get_app():
    """Return this process's application instance, creating it if needed.

    Task modules resolve their app through this instead of calling
    create_app() at import time, so importing them from a process that
    already has an application (e.g. the web process importing app.videos)
    doesn't build a second one.
    """

    global _app
    if _app is None:
        _app = create_app()
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
            "TMDB_API_KEY is not set, TMDb metadata and poster lookups will be skipped"
        )


def create_app(config_class=Config):
    class MyHandler(FileSystemEventHandler):
        """Handlers for watchdog to fire when filesystem events occur."""

        def on_moved(self, event):
            """Process a file when it's moved within the watched directory."""

            # Process only those moved files that were previously invisible

            if (
                os.path.basename(event.src_path).startswith(".")
                and not os.path.basename(event.dest_path).startswith(".")
                and os.path.isfile(event.dest_path)
            ):
                # Create redis lock using the filename, to prevent multiple workers
                # from grabbing the same file at once

                lock = app.lock_manager.lock(os.path.basename(event.dest_path), 1000)
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

                    if os.path.basename(event.dest_path) not in job_queue:
                        app.logger.info(
                            f"'{os.path.basename(event.dest_path)}' Found in import directory"
                        )
                        app.import_queue.enqueue(
                            "app.videos.localization_task",
                            args=(event.dest_path,),
                            job_timeout=app.config["LOCALIZATION_TASK_TIMEOUT"],
                            description=f"'{os.path.basename(event.dest_path)}'",
                            job_id=os.path.basename(event.dest_path),
                        )

                    app.lock_manager.unlock(lock)

        def on_created(self, event):
            """Process a file when it appears in the watched directory."""

            # Process only those files that are not invisible

            if not os.path.basename(event.src_path).startswith(".") and os.path.isfile(
                event.src_path
            ):
                # Create redis lock using the filename, to prevent multiple workers
                # from grabbing the same file at once

                lock = app.lock_manager.lock(os.path.basename(event.src_path), 1000)
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

                    if os.path.basename(event.src_path) not in job_queue:
                        app.logger.info(
                            f"'{os.path.basename(event.src_path)}' Found in import directory"
                        )
                        app.import_queue.enqueue(
                            "app.videos.localization_task",
                            args=(event.src_path,),
                            job_timeout=app.config["LOCALIZATION_TASK_TIMEOUT"],
                            description=f"'{os.path.basename(event.src_path)}'",
                            job_id=os.path.basename(event.src_path),
                        )

                    app.lock_manager.unlock(lock)

        def on_any_event(self, event):
            """Process on any filesystem event."""

            app.logger.debug(event)

    app = Flask(__name__)

    # Build the application configuration from the config.py file

    app.config.from_object(config_class)
    app.jinja_env.filters["quote_plus"] = lambda u: quote_plus(u)

    # Configure the Redis connection and queues

    app.redis = Redis.from_url(app.config["REDIS_URL"])
    app.maintenance_queue = rq.Queue("fitzflix-maintenance", connection=app.redis)
    app.sql_queue = rq.Queue("fitzflix-sql", connection=app.redis)
    app.request_queue = rq.Queue("fitzflix-user-request", connection=app.redis)
    app.import_queue = rq.Queue("fitzflix-import", connection=app.redis)
    app.transcode_queue = rq.Queue("fitzflix-transcode", connection=app.redis)
    app.file_queue = rq.Queue("fitzflix-file-operation", connection=app.redis)

    app.maintenance_scheduler = Scheduler("fitzflix-maintenance", connection=app.redis)
    app.sql_scheduler = Scheduler("fitzflix-sql", connection=app.redis)
    app.request_scheduler = Scheduler("fitzflix-user-request", connection=app.redis)
    app.import_scheduler = Scheduler("fitzflix-import", connection=app.redis)
    app.transcode_scheduler = Scheduler("fitzflix-transcode", connection=app.redis)
    app.file_scheduler = Scheduler("fitzflix-file-operation", connection=app.redis)

    # Rotate the application log daily at midnight; the fixed job id makes
    # re-registration from each process update the same scheduled job

    app.maintenance_scheduler.cron(
        "0 0 * * *",
        func="app.maintenance.rotate_logs",
        id="rotate-logs",
        use_local_timezone=True,
        timeout=54000,
        description="Rotating application logs",
    )

    # Configure the Redis redlock manager

    app.lock_manager = Redlock([app.redis])

    # Initialize application components

    db.init_app(app)
    migrate.init_app(app, db)
    login.init_app(app)
    mail.init_app(app)
    bootstrap.init_app(app)
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
            isinstance(handler, logging.FileHandler)
            for handler in app.logger.handlers
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

    # Create the import directory

    os.makedirs(app.config["IMPORT_DIR"], exist_ok=True)

    # Watch the import directory for file changes

    event_handler = MyHandler()
    observer = PollingObserver()
    observer.schedule(event_handler, path=app.config["IMPORT_DIR"], recursive=False)
    observer.start()

    # The first application created becomes this process's instance for
    # modules that resolve their app through get_app()

    global _app
    if _app is None:
        _app = app

    return app
