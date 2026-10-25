"""Provide the shared fixtures: an isolated app on SQLite, Redis DB 9, temp dirs.

The suite never touches the production database, Redis DB 0, the mail
server, or an external service. TMDB, Sonarr, and Radarr point at an
unroutable local port. Thus, the calls fail fast. AWS is not configured.
The reference data (qualities and feature types) mirrors the production
rows exactly.
"""

import os
import shutil
import tempfile
from functools import partial

import pytest

from config import Config

_TMP = tempfile.mkdtemp(prefix="fitzflix-tests-")

ADMIN_EMAIL = "admin@example.test"
ADMIN_PASSWORD = "test-password"
ADMIN_API_KEY = "0123456789abcdef0123456789abcdef"

MEMBER_EMAIL = "member@example.test"
MEMBER_PASSWORD = "member-password"
MEMBER_API_KEY = "fedcba9876543210fedcba9876543210"

# quality_title, preference, physical_media. This mirrors the production table

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
    STAGING_DIR = os.path.join(_TMP, "staging")
    HELD_DIR = os.path.join(MEDIA_LOCATION, "held")
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
    AWS_DOWNLOAD_VIA_CDN = False
    CDN_DOMAIN = None
    CDN_KEY_PAIR_ID = None
    CDN_PRIVATE_KEY = None

    # This port is unroutable. Thus, external calls fail immediately. They
    # do not reach a real service and do not hang on a timeout

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
    # The base config computes these from the real library. A test must
    # never write there.
    RADARR_ROOT_FOLDERS = [MOVIE_LIBRARY]
    SONARR_ROOT_FOLDERS = [TV_LIBRARY]
    RADARR_PROXY_URL = "http://127.0.0.1:1"
    ARR_DOWNLOAD_FOLDERS = [os.path.join(_TMP, "downloads")]
    WIKIDATA_SPARQL_URL = "http://127.0.0.1:1"
    HANDBRAKE_PRESET_FILE = None
    # The developer .env must not lock a setting of the Settings page
    ENV_LOCKED = frozenset()


def _register_mysql_compat_functions(engine):
    """Provide SQLite versions of the MariaDB functions that the app queries use.

    This registers them on each new DBAPI connection. The route queries
    use adddate and regexp_replace. The models use utc_timestamp as the
    insert default.
    """

    import math as _math
    import re as _re
    import sqlite3 as _sqlite3
    from datetime import date as _date, datetime as _datetime, timedelta as _timedelta

    # Show the real exception when a compat function fails. Do not show
    # the generic sqlite message "user-defined function raised exception"

    _sqlite3.enable_callback_tracebacks(True)

    from sqlalchemy import event

    def _adddate(value, days):
        parsed = _date.fromisoformat(str(value)[:10])
        return (parsed + _timedelta(days=days)).isoformat()

    def _regexp_replace(value, pattern, replacement):
        return _re.sub(pattern, replacement, value) if value is not None else None

    def _floor(value):
        # The SQLite dialect of SQLAlchemy registers an unguarded math.floor.
        # In MariaDB, floor(NULL) is NULL. This is important because review
        # ratings are nullable now
        return _math.floor(value) if value is not None else None

    def _substring_index(value, delimiter, count):
        # The app uses only the count=-1 form (the text after the last
        # delimiter). But this mirrors the general semantics of MySQL
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
        TestConfig.STAGING_DIR,
        TestConfig.HELD_DIR,
        TestConfig.DB_BACKUP_DIR,
        os.path.dirname(TestConfig.LOG_FILE),
        os.path.join(_TMP, "incoming"),
    ):
        os.makedirs(path, exist_ok=True)

    from app import create_app, db
    from app.models import RefFeatureType, RefQuality, User
    import app.models as models

    # The default hash (scrypt) costs about 0.1 s per password. The suite
    # sets about 20 passwords. A cheap pbkdf2 is enough for a test. The
    # check reads the method from the hash. Thus, each login still works

    patches = pytest.MonkeyPatch()
    patches.setattr(
        models,
        "generate_password_hash",
        partial(models.generate_password_hash, method="pbkdf2:sha256:1000"),
    )

    # watch_import_dir: the watchdog test puts files into IMPORT_DIR and
    # expects Fitzflix to notice them. Thus, the test app opts into the
    # observer. In production, only the import workers run the observer

    application = create_app(TestConfig, watch_import_dir=True)

    # A busy lock makes the task defer. In production, Redlock tries 3
    # times with 0.2 s between the tries. A test that holds a lock
    # paid for the waits (about 5 s over the suite). One try is enough

    application.lock_manager.retry_count = 1
    application.lock_manager.retry_delay = 0

    # Refuse to run against a store that is not isolated

    assert application.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite")
    assert application.redis.connection_pool.connection_kwargs.get("db") == 9

    # The first app created in the process becomes the singleton of
    # get_app(). The task modules (app.videos, app.atmos, ...) capture it
    # at import time. A test module that imports one of them at TOP LEVEL
    # runs during pytest collection, before this fixture. That creates a
    # real-config app. It silently points each task function in the
    # suite at production. Import the task modules inside the test
    # functions instead.

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
    patches.undo()


def pytest_sessionfinish(session, exitstatus):
    """Remove the temp directory of the session (#363).

    Each session made a new directory under TMPDIR and left it. On
    2026-10-25, 558 of them held 347 MB."""

    shutil.rmtree(_TMP, ignore_errors=True)


@pytest.fixture(autouse=True)
def clean_state(app):
    """Start each test with an empty Redis. Delete its DB rows afterwards."""

    from app.settings import clear_cache

    app.redis.flushdb()
    clear_cache()
    yield
    clear_cache()
    from app import db

    keep = {"ref_quality", "ref_feature_type", "user"}
    with app.app_context():
        db.session.rollback()
        for table in reversed(db.metadata.sorted_tables):
            if table.name not in keep:
                db.session.execute(table.delete())
        db.session.commit()
    reset_user_state(app)
    clear_rejects(app)
    clear_artifacts(app)


def clear_rejects(app):
    """Remove the files that a test left in the shared rejects directory.

    The rejects directory lives for the whole session. A test that
    rejects a file and does not remove it leaves the file for the next
    test. Then a test that asserts an empty rejects directory fails when
    it runs after that test, and passes when it runs alone. The reason
    subdirectories stay. Only the files go."""

    for root, _, files in os.walk(app.config["REJECTS_DIR"]):
        for name in files:
            try:
                os.remove(os.path.join(root, name))
            except FileNotFoundError:
                pass


def _empty_directory(path):
    """Remove the contents of a directory. Keep the directory."""

    if not os.path.isdir(path):
        return
    for name in os.listdir(path):
        child = os.path.join(path, name)
        if os.path.isdir(child) and not os.path.islink(child):
            shutil.rmtree(child, ignore_errors=True)
        else:
            try:
                os.remove(child)
            except FileNotFoundError:
                pass


def clear_artifacts(app):
    """Empty the shared directories that a test can fill (#363).

    The 2 library roots, the held directory and the custom artwork
    directory live for the whole session. The log rotation leaves
    archives next to the log file. A test that asserts an empty
    directory, or counts the archives, failed when it ran after a test
    that left files there. The log file itself stays: the app holds it
    open."""

    for key in ("MOVIE_LIBRARY", "TV_LIBRARY", "HELD_DIR", "CUSTOM_ARTWORK_DIR"):
        _empty_directory(app.config[key])
    log_file = app.config["LOG_FILE"]
    log_dir = os.path.dirname(log_file)
    for name in os.listdir(log_dir):
        if name != os.path.basename(log_file):
            try:
                os.remove(os.path.join(log_dir, name))
            except FileNotFoundError:
                pass


def reset_user_state(app):
    """Set the user columns that tests change back to the seed values (#333).

    The user table stays between tests. test_letterboxd.py sets
    letterboxd_username and does not reset it. Thus, a later test that
    needs the field empty could fail, or pass for the wrong reason."""

    from app import db
    from app.models import User

    with app.app_context():
        User.query.update({User.letterboxd_username: None})
        db.session.commit()


@pytest.fixture(autouse=True)
def no_growth_wait(app, monkeypatch):
    """Remove the wait between the 2 size readings of an incoming file.

    The wait is 5 seconds in production. Each localization test paid for
    it. A test of a growing file sets its own short wait."""

    from app import importing

    monkeypatch.setattr(importing, "GROWTH_CHECK_SECONDS", 0)


@pytest.fixture
def client(app):
    return app.test_client()


def _signed_in_client(app, email):
    """Return a test client with a session that is logged in as the given user."""

    from app.models import User

    with app.app_context():
        session_id = User.query.filter_by(email=email).one().get_id()

    test_client = app.test_client()
    serializer = app.session_interface.get_signing_serializer(app)
    test_client.set_cookie(
        app.config.get("SESSION_COOKIE_NAME", "session"),
        serializer.dumps({"_user_id": session_id, "_fresh": True}),
        domain="localhost",
    )
    return test_client


@pytest.fixture
def admin_client(app):
    """Return a test client that is logged in as the admin user."""

    return _signed_in_client(app, ADMIN_EMAIL)


@pytest.fixture
def user_client(app):
    """Return a test client that is logged in as a regular (non-admin) user."""

    return _signed_in_client(app, MEMBER_EMAIL)


@pytest.fixture
def held_lock(app):
    """Hold a redlock for the duration of a test."""

    locks = []

    def hold(resource, ttl_ms=60000):
        lock = app.lock_manager.lock(resource, ttl_ms)
        assert lock, f"could not take test lock on {resource}"
        locks.append(lock)
        return lock

    yield hold
    for lock in locks:
        app.lock_manager.unlock(lock)


@pytest.fixture(scope="session")
def sample_mkv(app):
    """Build a 1-second Matroska file with an English audio track."""

    from tests.media import make_sample_mkv

    return make_sample_mkv(os.path.join(_TMP, "sample.mkv"))


@pytest.fixture(scope="session")
def sample_mp4(app):
    """Build a 1-second MP4 file, a container that the import converts."""

    from tests.media import make_sample_mp4

    return make_sample_mp4(os.path.join(_TMP, "sample-base.mp4"))


@pytest.fixture
def incoming_dir():
    """Return a staging directory outside IMPORT_DIR, hidden from the watchdog."""

    return os.path.join(_TMP, "incoming")


@pytest.fixture
def fake_tmdb(monkeypatch):
    """Replace the unroutable TMDB with one that answers with no matches.

    Use this in tests that assert on the log output. There, the
    connection-refused warning is noise."""

    class FakeResponse:
        def json(self):
            return {"results": []}

        def raise_for_status(self):
            pass

    # The requests module that tmdb_get uses is in app.tmdb_apply. app.videos
    # stopped importing requests when its network-facing parts moved out

    import app.tmdb_apply

    monkeypatch.setattr(
        app.tmdb_apply.requests, "get", lambda *args, **kwargs: FakeResponse()
    )


@pytest.fixture
def log_capture(app):
    """Capture the log records that the application logger emits."""

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
    """Return the lineup rebuilds that wait on the maintenance queue.

    The DVR editor and the Criterion scrapers enqueue these jobs."""

    return [
        job
        for job in app.maintenance_queue.jobs
        if job.func_name == "app.dvr.build_channel_lineups"
    ]


def csrf_token_from(page_html):
    """Return the csrf token that a rendered form carries."""

    import re

    match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page_html)
    assert match, "no csrf token found in page"
    return match.group(1)


def page_csrf_token(client, path="/profile"):
    """Return the csrf token that a page renders for a signed-in client.

    A hand-built POST to a route that checks the form must carry this
    token."""

    return csrf_token_from(client.get(path).get_data(as_text=True))


@pytest.fixture
def admin_id(app):
    """Return the id of the seeded admin user."""

    from app.models import User

    with app.app_context():
        return User.query.filter_by(admin=True).first().id
