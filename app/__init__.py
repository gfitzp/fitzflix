import logging
import os
import random
import time

from logging.handlers import SMTPHandler, RotatingFileHandler

import rq

from redis import Redis
from redlock import Redlock
from rq.registry import StartedJobRegistry
from rq_scheduler import Scheduler
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

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


def create_app(config_class=Config):
    class MyHandler(FileSystemEventHandler):
        """Handlers for watchdog to fire when filesystem events occur."""

        def on_moved(self, event):
            """Process a file when it's moved within the watched directory."""

            # Process only those moved files that were previously invisible

            if os.path.basename(event.src_path).startswith(
                "."
            ) and not os.path.basename(event.dest_path).startswith("."):

                # Sleep for 1-60 seconds to prevent two workers trying to grab the same
                # file simultaneously

                time.sleep(random.randint(1, 60))

                job_queue = []
                tasks_running = StartedJobRegistry("fitzflix-tasks", connection=app.redis)
                job_queue.extend(tasks_running.get_job_ids())
                job_queue.extend(app.task_queue.job_ids)

                app.logger.debug(job_queue)

                # Use the file basename as the job id, so we can see if this file is
                # already in the job_queue, and only add it if it doesn't already exist

                if (os.path.basename(event.dest_path) not in job_queue):
                    app.logger.info(
                        f"'{os.path.basename(event.dest_path)}' Found in import directory"
                    )
                    job = app.task_queue.enqueue(
                        "app.videos.localization_task",
                        args=(event.dest_path,),
                        job_timeout=app.config["LOCALIZATION_TASK_TIMEOUT"],
                        description=f"'{os.path.basename(event.dest_path)}'",
                        job_id=os.path.basename(event.dest_path),
                    )

        def on_created(self, event):
            """Process a file when it appears in the watched directory."""

            # Process only those files that are not invisible

            if not os.path.basename(event.src_path).startswith("."):

                # Sleep for 1-60 seconds to prevent two workers trying to grab the same
                # file simultaneously

                time.sleep(random.randint(1, 60))

                job_queue = []
                tasks_running = StartedJobRegistry("fitzflix-tasks", connection=app.redis)
                job_queue.extend(tasks_running.get_job_ids())
                job_queue.extend(app.task_queue.job_ids)

                app.logger.debug(job_queue)

                # Use the file basename as the job id, so we can see if this file is
                # already in the job_queue, and only add it if it doesn't already exist

                if (os.path.basename(event.src_path) not in job_queue):
                    app.logger.info(
                        f"'{os.path.basename(event.src_path)}' Found in import directory"
                    )
                    job = app.task_queue.enqueue(
                        "app.videos.localization_task",
                        args=(event.src_path,),
                        job_timeout=app.config["LOCALIZATION_TASK_TIMEOUT"],
                        description=f"'{os.path.basename(event.src_path)}'",
                        job_id=os.path.basename(event.src_path),
                    )

        def on_any_event(self, event):
            """Process on any filesystem event."""

            app.logger.debug(event)

    app = Flask(__name__)

    # Build the application configuration from the config.py file

    app.config.from_object(config_class)

    # Configure the Redis connection and queues

    app.redis = Redis.from_url(app.config["REDIS_URL"])
    app.task_queue = rq.Queue("fitzflix-tasks", connection=app.redis)
    app.task_scheduler = Scheduler("fitzflix-tasks", connection=app.redis)
    app.transcode_queue = rq.Queue("fitzflix-transcode", connection=app.redis)
    app.transcode_scheduler = Scheduler("fitzflix-transcode", connection=app.redis)
    app.sql_queue = rq.Queue("fitzflix-sql", connection=app.redis)
    app.sql_scheduler = Scheduler("fitzflix-sql", connection=app.redis)

    # Configure the Redis redlock manager

    app.lock_manager = Redlock([app.redis])

    # Initialize application components

    db.init_app(app)
    migrate.init_app(app, db)
    login.init_app(app)
    mail.init_app(app)
    bootstrap.init_app(app)
    moment.init_app(app)

    # Build blueprints

    from app.errors import bp as errors_bp

    app.register_blueprint(errors_bp)

    from app.auth import bp as auth_bp

    app.register_blueprint(auth_bp, url_prefix="/auth")

    from app.main import bp as main_bp

    app.register_blueprint(main_bp)

    from app.api import bp as api_bp

    app.register_blueprint(api_bp, url_prefix="/api")

    from app import models, videos

    if not app.debug:

        # Configure how to handle logs when running in production mode

        if app.config["MAIL_SERVER"]:

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

        if not os.path.exists("logs"):
            os.mkdir("logs")

        file_handler = RotatingFileHandler(
            "logs/fitzflix.log", maxBytes=10485760, backupCount=10
        )
        file_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]"
            )
        )
        file_handler.setLevel(logging.INFO)
        app.logger.addHandler(file_handler)
        app.logger.setLevel(logging.INFO)
        app.logger.info("Fitzflix startup")

    # Create the import directory

    os.makedirs(app.config["IMPORT_DIR"], exist_ok=True)

    # Watch the import directory for file changes

    event_handler = MyHandler()
    observer = Observer()
    observer.schedule(event_handler, path=app.config["IMPORT_DIR"], recursive=False)
    observer.start()

    return app
