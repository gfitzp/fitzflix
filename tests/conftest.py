"""Shared fixtures: an isolated app wired to SQLite, Redis DB 9, and temp dirs.

The suite never touches the production database, Redis DB 0, the mail server,
or any external service: TMDB/Sonarr/Radarr point at an unroutable local port
so calls fail fast, and AWS is unconfigured. Reference data (qualities and
feature types) mirrors the production rows exactly.
"""

import os
import tempfile

import pytest

from config import Config

_TMP = tempfile.mkdtemp(prefix="fitzflix-tests-")

ADMIN_EMAIL = "admin@example.test"
ADMIN_PASSWORD = "test-password"
ADMIN_API_KEY = "0123456789abcdef0123456789abcdef"

MEMBER_EMAIL = "member@example.test"
MEMBER_PASSWORD = "member-password"
MEMBER_API_KEY = "fedcba9876543210fedcba9876543210"

# quality_title, preference, physical_media — mirrors the production table

QUALITIES = [
    ("Not in library", 0, False),
    ("Unknown", 1, False),
    ("SDTV", 2, False),
    ("HDTV-720p", 3, False),
    ("HDTV-1080p", 4, False),
    ("HDTV-2160p", 5, False),
    ("Raw-HD", 6, False),
    ("WEBRip-480p", 7, False),
    ("WEBDL-480p", 8, False),
    ("DVD", 9, True),
    ("Bluray-480p", 10, True),
    ("WEBRip-720p", 11, False),
    ("WEBDL-720p", 12, False),
    ("Bluray-720p", 13, True),
    ("WEBRip-1080p", 14, False),
    ("WEBDL-1080p", 15, False),
    ("WEBRip-2160p", 16, False),
    ("WEBDL-2160p", 17, False),
    ("Bluray-1080p", 18, True),
    ("Bluray-1080p Remux", 19, True),
    ("Bluray-2160p", 20, True),
    ("Bluray-2160p Remux", 21, True),
]

FEATURE_TYPES = [
    "Behind The Scenes",
    "Deleted Scenes",
    "Featurettes",
    "Interviews",
    "Scenes",
    "Shorts",
    "Trailers",
    "Other",
]


class TestConfig(Config):
    TESTING = True
    SECRET_KEY = "test-secret-key"
    SERVER_NAME = None
    PREFERRED_URL_SCHEME = "http"
    SESSION_COOKIE_SECURE = False
    REMEMBER_COOKIE_SECURE = False
    PREVENT_ACCOUNT_CREATION = False

    SQLALCHEMY_DATABASE_URI = f"sqlite:///{_TMP}/fitzflix-test.db"
    REDIS_URL = "redis://localhost:6379/9"

    MEDIA_LOCATION = os.path.join(_TMP, "media")
    IMPORT_DIR = os.path.join(MEDIA_LOCATION, "import")
    LIBRARY_DIR = os.path.join(MEDIA_LOCATION, "library")
    MOVIE_LIBRARY = os.path.join(LIBRARY_DIR, "Movies")
    TV_LIBRARY = os.path.join(LIBRARY_DIR, "TV Shows")
    REJECTS_DIR = os.path.join(MEDIA_LOCATION, "rejects")
    TRANSCODES_DIR = os.path.join(MEDIA_LOCATION, "transcoded")
    STAGING_DIR = os.path.join(_TMP, "staging")
    TRIAGE_SNAPSHOT_DIR = os.path.join(_TMP, "triage")
    FRAME_POOL_DIR = os.path.join(_TMP, "frame_pool")
    MOUNT_URLS = {}

    LOG_FILE = os.path.join(_TMP, "logs", "fitzflix.log")
    DB_BACKUP_DIR = os.path.join(_TMP, "backups")
    BACKUP_PASSPHRASE = None
    ENV_FILE = os.path.join(_TMP, "dotenv-for-tests")
    CUSTOM_ARTWORK_DIR = os.path.join(_TMP, "custom-artwork")

    MAIL_SERVER = None
    MAIL_USERNAME = None
    MAIL_PASSWORD = None
    SERVER_EMAIL = None
    ADMIN_EMAIL = None
    TODO_EMAIL = None

    ARCHIVE_ORIGINAL_MEDIA = False
    AWS_BUCKET = None
    AWS_ACCESS_KEY = None
    AWS_SECRET_KEY = None
    AWS_SQS_URL = None

    # Unroutable port: external calls fail immediately instead of reaching
    # a real service or hanging on a timeout

    TMDB_API_KEY = None
    TMDB_API_URL = "http://127.0.0.1:1"
    PLEX_URL = None
    PLEX_TOKEN = None
    PLEX_WEBHOOK_TOKEN = "plex-test-webhook-token"
    DVR_TOKEN = "dvr-test-token"
    SONARR_URL = "http://127.0.0.1:1"
    SONARR_API_KEY = "sonarr-test-key"
    RADARR_URL = "http://127.0.0.1:1"
    RADARR_API_KEY = "radarr-test-key"
    RADARR_PROXY_URL = "http://127.0.0.1:1"
    WIKIDATA_SPARQL_URL = "http://127.0.0.1:1"
    HANDBRAKE_PRESET_FILE = None


def _register_mysql_compat_functions(engine):
    """Provide SQLite versions of the MariaDB functions the app's queries use.

    Registered on every new DBAPI connection: adddate and regexp_replace
    appear in route queries, and utc_timestamp is the models' insert default.
    """

    import math as _math
    import re as _re
    import sqlite3 as _sqlite3
    from datetime import date as _date, datetime as _datetime, timedelta as _timedelta

    # Surface the real exception when a compat function fails, instead of
    # sqlite's generic "user-defined function raised exception"

    _sqlite3.enable_callback_tracebacks(True)

    from sqlalchemy import event

    def _adddate(value, days):
        parsed = _date.fromisoformat(str(value)[:10])
        return (parsed + _timedelta(days=days)).isoformat()

    def _regexp_replace(value, pattern, replacement):
        return _re.sub(pattern, replacement, value) if value is not None else None

    def _floor(value):
        # SQLAlchemy's SQLite dialect registers an unguarded math.floor;
        # MariaDB's floor(NULL) is NULL, which matters now that review
        # ratings are nullable
        return _math.floor(value) if value is not None else None

    def _substring_index(value, delimiter, count):
        # Only the count=-1 form (text after the last delimiter) is used,
        # but mirror MySQL's general semantics anyway
        if value is None or delimiter is None:
            return None
        parts = str(value).split(delimiter)
        if count > 0:
            return delimiter.join(parts[:count])
        if count < 0:
            return delimiter.join(parts[count:])
        return ""

    def _utc_timestamp():
        return _datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    @event.listens_for(engine, "connect")
    def _register(dbapi_connection, _record):
        dbapi_connection.create_function("adddate", 2, _adddate)
        dbapi_connection.create_function("floor", 1, _floor)
        dbapi_connection.create_function("regexp_replace", 3, _regexp_replace)
        dbapi_connection.create_function("substring_index", 3, _substring_index)
        dbapi_connection.create_function("utc_timestamp", 0, _utc_timestamp)


@pytest.fixture(scope="session")
def app():
    for path in (
        TestConfig.IMPORT_DIR,
        TestConfig.MOVIE_LIBRARY,
        TestConfig.TV_LIBRARY,
        TestConfig.REJECTS_DIR,
        TestConfig.TRANSCODES_DIR,
        TestConfig.STAGING_DIR,
        TestConfig.DB_BACKUP_DIR,
        os.path.dirname(TestConfig.LOG_FILE),
        os.path.join(_TMP, "incoming"),
    ):
        os.makedirs(path, exist_ok=True)

    from app import create_app, db
    from app.models import RefFeatureType, RefQuality, User

    # watch_import_dir: the watchdog test drops files into IMPORT_DIR and
    # expects them noticed, so the test app opts into the observer that
    # production scopes to the import workers

    application = create_app(TestConfig, watch_import_dir=True)

    # Refuse to run against anything but the isolated stores

    assert application.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite")
    assert application.redis.connection_pool.connection_kwargs.get("db") == 9

    # The first app created in the process becomes get_app()'s singleton,
    # which task modules (app.videos, app.atmos, ...) capture at import
    # time. A test module importing one of them at TOP LEVEL runs during
    # pytest collection — before this fixture — creating a real-config
    # app and silently pointing every task function in the suite at
    # production. Import task modules inside test functions instead.

    from app import get_app

    assert get_app() is application, (
        "get_app()'s singleton predates the test app: a test module "
        "imports a task module at top level; move that import inside "
        "the test function"
    )

    with application.app_context():
        _register_mysql_compat_functions(db.engine)
        db.create_all()
        for title, preference, physical in QUALITIES:
            db.session.add(
                RefQuality(
                    quality_title=title, preference=preference, physical_media=physical
                )
            )
        for feature_type in FEATURE_TYPES:
            db.session.add(RefFeatureType(feature_type=feature_type))
        admin = User(email=ADMIN_EMAIL, admin=True, api_key=ADMIN_API_KEY)
        admin.set_password(ADMIN_PASSWORD)
        db.session.add(admin)
        member = User(email=MEMBER_EMAIL, admin=False, api_key=MEMBER_API_KEY)
        member.set_password(MEMBER_PASSWORD)
        db.session.add(member)
        db.session.commit()

    yield application


@pytest.fixture(autouse=True)
def clean_state(app):
    """Start each test with empty Redis, and drop its DB rows afterwards."""

    app.redis.flushdb()
    yield
    from app import db

    keep = {"ref_quality", "ref_feature_type", "user"}
    with app.app_context():
        db.session.rollback()
        for table in reversed(db.metadata.sorted_tables):
            if table.name not in keep:
                db.session.execute(table.delete())
        db.session.commit()


@pytest.fixture
def client(app):
    return app.test_client()


def _signed_in_client(app, email):
    """A test client whose session is logged in as the given user."""

    from app.models import User

    with app.app_context():
        user_id = User.query.filter_by(email=email).one().id

    test_client = app.test_client()
    serializer = app.session_interface.get_signing_serializer(app)
    test_client.set_cookie(
        app.config.get("SESSION_COOKIE_NAME", "session"),
        serializer.dumps({"_user_id": str(user_id), "_fresh": True}),
        domain="localhost",
    )
    return test_client


@pytest.fixture
def admin_client(app):
    """A test client logged in as the admin user."""

    return _signed_in_client(app, ADMIN_EMAIL)


@pytest.fixture
def user_client(app):
    """A test client logged in as a regular (non-admin) user."""

    return _signed_in_client(app, MEMBER_EMAIL)


@pytest.fixture
def incoming_dir():
    """A staging directory outside IMPORT_DIR, invisible to the watchdog."""

    return os.path.join(_TMP, "incoming")


@pytest.fixture
def fake_tmdb(monkeypatch):
    """A TMDB that responds successfully with no matches, instead of the
    unroutable default — for tests asserting on log output, where the
    connection-refused warning would be noise."""

    class FakeResponse:
        def json(self):
            return {"results": []}

        def raise_for_status(self):
            pass

    # tmdb_get's requests lives in app.models; app.videos stopped
    # importing requests when its network-facing slices moved out

    import app.models

    monkeypatch.setattr(
        app.models.requests, "get", lambda *args, **kwargs: FakeResponse()
    )


@pytest.fixture
def log_capture(app):
    """Capture log records emitted through the application logger."""

    import logging

    class Capture(logging.Handler):
        def __init__(self):
            super().__init__(level=logging.DEBUG)
            self.records = []

        def emit(self, record):
            self.records.append(record)

    handler = Capture()
    app.logger.addHandler(handler)
    yield handler.records
    app.logger.removeHandler(handler)


def dvr_rebuild_jobs(app):
    """The lineup rebuilds waiting on the maintenance queue — what the
    DVR editor and the Criterion scrapers enqueue."""

    return [
        job
        for job in app.maintenance_queue.jobs
        if job.func_name == "app.dvr.build_channel_lineups"
    ]
