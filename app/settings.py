"""Read and change the settings that the Admin Settings page controls.

Each setting has 3 possible sources. A setting that .env sets is locked,
because .env wins. Then the value of the page applies, if an admin set
one. Then the config.py default applies. get_setting gives the result.
Each read site of these settings uses it, not app.config.

The page values are rows of the app_setting table. Each process keeps
them for CACHE_SECONDS. Thus, a change on the page gets to each web and
worker process in some seconds, without a restart. The read uses its
own connection, not the session. A long worker transaction thus sees
each change, and a failed read does not touch the session of the task.
"""

import json
import os
import re
import threading
import time

from dataclasses import dataclass

from flask import current_app, has_app_context
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

CACHE_SECONDS = 5

# The bitrates (bps) that MediaConvert accepts for E-AC-3 Atmos.

EAC3_ATMOS_BITRATES = (384000, 448000, 576000, 640000, 768000, 1024000)

AWS_UPLOAD_SETTINGS = ("AWS_BUCKET", "AWS_ACCESS_KEY", "AWS_SECRET_KEY")


@dataclass(frozen=True)
class Setting:
    """The type, the limits, and the page text of a setting."""

    name: str
    label: str
    help: str
    kind: str  # flag, int, choice, text, or regex
    minimum: int = None
    maximum: int = None
    choices: tuple = ()
    allow_empty: bool = False


SETTINGS = {
    setting.name: setting
    for setting in (
        Setting(
            "PREVENT_ACCOUNT_CREATION",
            "Prevent account creation",
            "Disables the registration page when an admin account exists.",
            "flag",
        ),
        Setting(
            "ARCHIVE_ORIGINAL_MEDIA",
            "Archive original media",
            "Uploads each imported original file to AWS S3 for archival. "
            "This needs AWS_BUCKET, AWS_ACCESS_KEY, and AWS_SECRET_KEY in .env. "
            "It is on by default when they are there.",
            "flag",
        ),
        Setting(
            "AWS_DOWNLOAD_VIA_CDN",
            "Download restores through CloudFront",
            "Restore downloads come through CloudFront, not directly from S3. "
            "This needs CDN_DOMAIN, CDN_KEY_PAIR_ID, and CDN_PRIVATE_KEY in .env.",
            "flag",
        ),
        Setting(
            "CREATE_FLAC_TWINS",
            "Create FLAC twins",
            "The import adds a FLAC twin to each lossless track, for players "
            "that cannot play the original. Turn this off with a passthrough "
            "player.",
            "flag",
        ),
        Setting(
            "CREATE_ATMOS_TWINS",
            "Create E-AC-3 Atmos twins",
            "The import adds an E-AC-3 Atmos twin to each TrueHD Atmos track, "
            "for players that cannot play TrueHD. Each twin costs some "
            "MediaConvert time. Turn this off with a passthrough player.",
            "flag",
        ),
        Setting(
            "DISK_ALERT_FREE_GB",
            "Disk alert free space (GB)",
            "The health check sends an alert when a volume has less free space.",
            "int",
            minimum=0,
            maximum=100000,
        ),
        Setting(
            "LOG_DIGEST_THRESHOLD",
            "Log digest threshold",
            "The log digest flags a message that occurs this number of times "
            "or more in 24 hours.",
            "int",
            minimum=1,
            maximum=100000,
        ),
        Setting(
            "LOG_DIGEST_IGNORE",
            "Log digest ignore pattern",
            "A regular expression. The log digest does not count a message "
            "that it finds. Empty counts all messages.",
            "regex",
            allow_empty=True,
        ),
        Setting(
            "EAC3_ATMOS_BITRATE",
            "E-AC-3 Atmos bitrate (bps)",
            "The bitrate of each new E-AC-3 Atmos twin from MediaConvert.",
            "choice",
            choices=EAC3_ATMOS_BITRATES,
        ),
        Setting(
            "DVR_GENRE_CHANNELS",
            "DVR movie genre channels",
            "The number of movie genre channels that the first DVR build adds.",
            "int",
            minimum=0,
            maximum=50,
        ),
        Setting(
            "DVR_TV_CHANNELS",
            "DVR TV genre channels",
            "The number of TV genre channels that the first DVR build adds.",
            "int",
            minimum=0,
            maximum=50,
        ),
        Setting(
            "DVR_CHANNEL_FILMS",
            "DVR films for each channel",
            "The maximum number of films in the lineup of a channel.",
            "int",
            minimum=1,
            maximum=1000,
        ),
        Setting(
            "DVR_CHANNEL_EPISODES",
            "DVR episodes for each channel",
            "The maximum number of episodes in the lineup of a channel.",
            "int",
            minimum=1,
            maximum=1000,
        ),
        Setting(
            "HANDBRAKE_PRESET",
            "HandBrake preset",
            "The name of the transcode preset. With HANDBRAKE_PRESET_FILE, "
            "the name must be in that file.",
            "text",
        ),
    )
}

_cache = {"values": {}, "loaded": None}
_cache_lock = threading.Lock()


def clear_cache():
    """Make the next read get the page values from the database again."""

    with _cache_lock:
        _cache["values"] = {}
        _cache["loaded"] = None


def _page_values():
    """Return {name: stored text} for the page values, from the cache if new.

    A failed read keeps the last values for CACHE_SECONDS. Thus, a
    database fault does not stop the imports, and the warning does not
    repeat on each read."""

    from app import db
    from app.models import AppSetting

    now = time.monotonic()
    with _cache_lock:
        if _cache["loaded"] is not None and now - _cache["loaded"] < CACHE_SECONDS:
            return _cache["values"]
    try:
        with db.engine.connect() as connection:
            rows = connection.execute(select(AppSetting.name, AppSetting.value)).all()
    except SQLAlchemyError as error:
        current_app.logger.warning(
            f"Cannot read the page settings, using the last values: {error}"
        )
        with _cache_lock:
            _cache["loaded"] = now
            return _cache["values"]
    values = {name: value for name, value in rows}
    with _cache_lock:
        _cache["values"] = values
        _cache["loaded"] = now
    return values


def parse_value(setting, text):
    """Return the typed value of a stored text."""

    if setting.kind == "flag":
        return text == "1"
    if setting.kind in ("int", "choice"):
        return int(text)
    return text


def store_text(setting, value):
    """Return the text that the table stores for a typed value."""

    if setting.kind == "flag":
        return "1" if value else "0"
    return str(value)


def locked(name, config=None):
    """Return True if .env sets the setting. Then the page cannot change it."""

    config = current_app.config if config is None else config
    return name in config.get("ENV_LOCKED", ())


def resolve(name, config=None):
    """Return (value, source) for a setting. The source is env, page, or default."""

    config = current_app.config if config is None else config
    if locked(name, config):
        return config[name], "env"
    text = _page_values().get(name)
    if text is not None:
        try:
            return parse_value(SETTINGS[name], text), "page"
        except ValueError:
            current_app.logger.warning(
                f"The page value of {name} ({text!r}) does not parse, "
                f"using the default"
            )
    return config[name], "default"


def get_setting(name, config=None):
    """Return the value of a setting: .env, then the page, then the default.

    The config argument is for the callers that get a config mapping.
    The default is the config of the current app. Outside an app
    context, the app of the process (get_app) applies. Thus, a worker
    can also run this function as an rq job."""

    if not has_app_context():
        from app import get_app

        with get_app().app_context():
            return resolve(name, config)[0]
    return resolve(name, config)[0]


def handbrake_presets(config=None):
    """Return the preset names in HANDBRAKE_PRESET_FILE, or None without a file."""

    config = current_app.config if config is None else config
    path = config.get("HANDBRAKE_PRESET_FILE")
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path) as preset_file:
            return [
                preset.get("PresetName")
                for preset in json.load(preset_file).get("PresetList", [])
            ]
    except (OSError, ValueError):
        return None


def value_problem(name, value, config=None):
    """Return why the page cannot set the value, or None if it can.

    The guards stop a setting that would fail at its next use. Archival
    needs the AWS settings. The CloudFront path needs the CDN settings
    and a private key that this process can read."""

    config = current_app.config if config is None else config
    if name == "ARCHIVE_ORIGINAL_MEDIA" and value:
        missing = [key for key in AWS_UPLOAD_SETTINGS if not config.get(key)]
        if missing:
            return (
                f"Set {', '.join(missing)} in .env first. "
                f"Without them, each upload fails."
            )
    if name == "AWS_DOWNLOAD_VIA_CDN" and value:
        from app.aws_storage import cdn_settings_problem

        problem = cdn_settings_problem()
        if problem:
            return f"Correct the CloudFront settings in .env first: {problem}."
    if name == "LOG_DIGEST_IGNORE" and value:
        try:
            re.compile(value)
        except re.error as error:
            return f"This is not a valid regular expression: {error}."
    if name == "HANDBRAKE_PRESET":
        if not value.strip():
            return "Enter a preset name."
        presets = handbrake_presets(config)
        if presets is not None and value not in presets:
            return f"HANDBRAKE_PRESET_FILE does not contain this preset: {presets}."
    return None


def set_setting(name, value, username):
    """Store a page value. Log the change with the name of the admin."""

    from app import db
    from app.models import AppSetting

    setting = SETTINGS[name]
    old = get_setting(name)
    row = db.session.get(AppSetting, name) or AppSetting(name=name)
    row.value = store_text(setting, value)
    row.updated_by = username
    db.session.add(row)
    db.session.commit()
    clear_cache()
    current_app.logger.info(
        f"Settings: {username} set {name} to {value!r} (was {old!r})"
    )


def clear_setting(name, username):
    """Remove a page value. The setting goes back to its default.

    Return False if the page had no value for the setting."""

    from app import db
    from app.models import AppSetting

    row = db.session.get(AppSetting, name)
    if row is None:
        return False
    old = get_setting(name)
    db.session.delete(row)
    db.session.commit()
    clear_cache()
    current_app.logger.info(
        f"Settings: {username} reset {name} to the default "
        f"{current_app.config[name]!r} (was {old!r})"
    )
    return True
