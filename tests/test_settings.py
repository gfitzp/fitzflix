"""The Admin Settings page and the get_setting precedence.

A setting that .env sets is locked, and the page cannot change it. A
setting that .env does not set takes the page value, if one exists.
Otherwise it takes the config.py default. The page refuses archival
without the AWS settings and the CloudFront path without the CDN
settings.
"""

import logging

import pytest

from tests.conftest import ADMIN_EMAIL, page_csrf_token


def _store(app, name, text, expire=True):
    """Write a page value directly, as another process would.

    expire empties the cache, as the end of CACHE_SECONDS would."""

    from app import db
    from app.models import AppSetting
    from app.settings import clear_cache

    with app.app_context():
        db.session.merge(AppSetting(name=name, value=text))
        db.session.commit()
    if expire:
        clear_cache()


def _stored(app, name):
    """Return the stored text of a page value, or None."""

    from app import db
    from app.models import AppSetting

    with app.app_context():
        row = db.session.get(AppSetting, name)
        return row.value if row else None


def test_page_names_match_config():
    """Each name in config.py has a page entry, and each entry has a name."""

    from app.settings import SETTINGS
    from config import PAGE_FLAGS, PAGE_TUNABLES

    assert set(SETTINGS) == set(PAGE_FLAGS) | set(PAGE_TUNABLES)


def test_env_locked_reads_presence_for_flags_and_values_for_tunables(monkeypatch):
    import config

    for name in config.PAGE_FLAGS + config.PAGE_TUNABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CREATE_FLAC_TWINS", "")
    monkeypatch.setenv("DISK_ALERT_FREE_GB", "")
    monkeypatch.setenv("EAC3_ATMOS_BITRATE", "768000")

    assert config._env_locked() == {"CREATE_FLAC_TWINS", "EAC3_ATMOS_BITRATE"}


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, True),
        ("", True),
        ("1", True),
        ("yes", True),
        ("0", False),
        ("false", False),
        (" Off ", False),
        ("NO", False),
    ],
)
def test_on_by_default_flags_turn_off_only_for_an_off_value(monkeypatch, raw, expected):
    import config

    if raw is None:
        monkeypatch.delenv("CREATE_FLAC_TWINS", raising=False)
    else:
        monkeypatch.setenv("CREATE_FLAC_TWINS", raw)

    assert config._on_by_default("CREATE_FLAC_TWINS") is expected


@pytest.mark.parametrize("aws", [True, False])
def test_archive_default_follows_the_aws_settings(monkeypatch, aws):
    """Archival is on by default only when the uploads can succeed."""

    import config

    monkeypatch.delenv("ARCHIVE_ORIGINAL_MEDIA", raising=False)
    monkeypatch.setenv("AWS_BUCKET", "bucket" if aws else "")
    monkeypatch.setenv("AWS_ACCESS_KEY", "key")
    monkeypatch.setenv("AWS_SECRET_KEY", "secret")
    default = config._aws_uploads_configured()

    assert default is aws
    assert config._on_by_default("ARCHIVE_ORIGINAL_MEDIA", default) is aws
    monkeypatch.setenv("ARCHIVE_ORIGINAL_MEDIA", "0")
    assert config._on_by_default("ARCHIVE_ORIGINAL_MEDIA", default) is False


def test_precedence_env_then_page_then_default(app, monkeypatch):
    from app.settings import get_setting, resolve

    monkeypatch.setitem(app.config, "DISK_ALERT_FREE_GB", 100)
    with app.app_context():
        assert resolve("DISK_ALERT_FREE_GB") == (100, "default")

    _store(app, "DISK_ALERT_FREE_GB", "250")
    with app.app_context():
        assert resolve("DISK_ALERT_FREE_GB") == (250, "page")

    monkeypatch.setitem(app.config, "ENV_LOCKED", frozenset({"DISK_ALERT_FREE_GB"}))
    with app.app_context():
        assert resolve("DISK_ALERT_FREE_GB") == (100, "env")
        assert get_setting("DISK_ALERT_FREE_GB") == 100


def test_flag_page_value_reaches_a_read_site(app):
    from app.aws_storage import cdn_download_enabled

    with app.app_context():
        assert cdn_download_enabled() is False
    _store(app, "AWS_DOWNLOAD_VIA_CDN", "1")
    with app.app_context():
        assert cdn_download_enabled() is True


def test_page_value_that_does_not_parse_uses_the_default(app, monkeypatch):
    from app.settings import resolve

    monkeypatch.setitem(app.config, "DVR_CHANNEL_FILMS", 40)
    _store(app, "DVR_CHANNEL_FILMS", "forty")
    with app.app_context():
        assert resolve("DVR_CHANNEL_FILMS") == (40, "default")


def test_cache_holds_while_the_version_agrees(app, monkeypatch):
    """Each process reads the table again when the version changes, or after a minute."""

    from app import settings

    clock = [1000.0]
    monkeypatch.setattr(settings.time, "monotonic", lambda: clock[0])
    with app.app_context():
        assert settings.get_setting("CREATE_FLAC_TWINS") is True
        _store(app, "CREATE_FLAC_TWINS", "0", expire=False)
        clock[0] += settings.CACHE_SECONDS
        assert settings.get_setting("CREATE_FLAC_TWINS") is True
        clock[0] += settings.VERSIONED_CACHE_SECONDS
        assert settings.get_setting("CREATE_FLAC_TWINS") is False


def test_cache_holds_for_some_seconds_without_redis(app, monkeypatch):
    """Without Redis, each process reads the table at most 1 time in CACHE_SECONDS."""

    from app import settings

    clock = [1000.0]
    monkeypatch.setattr(settings.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(settings, "_settings_version", lambda: None)
    with app.app_context():
        assert settings.get_setting("CREATE_FLAC_TWINS") is True
        _store(app, "CREATE_FLAC_TWINS", "0", expire=False)
        assert settings.get_setting("CREATE_FLAC_TWINS") is True
        clock[0] += settings.CACHE_SECONDS
        assert settings.get_setting("CREATE_FLAC_TWINS") is False


def test_a_save_in_another_worker_reaches_this_cache_at_once(app, monkeypatch):
    """The save of another web worker bumps the Redis version (#440).

    Before, this worker showed the old value for CACHE_SECONDS. A save
    from that render could store the old value again."""

    from app import settings

    clock = [1000.0]
    monkeypatch.setattr(settings.time, "monotonic", lambda: clock[0])
    with app.app_context():
        assert settings.get_setting("CREATE_FLAC_TWINS") is True

        # The other worker stores the row and bumps the version. It
        # clears only its own cache, not this one.
        real_clear = settings.clear_cache
        monkeypatch.setattr(settings, "clear_cache", lambda: None)
        settings.set_setting("CREATE_FLAC_TWINS", False, "other-worker")
        monkeypatch.setattr(settings, "clear_cache", real_clear)

        assert settings.get_setting("CREATE_FLAC_TWINS") is False
        settings.clear_setting("CREATE_FLAC_TWINS", "other-worker")
        assert app.redis.get(settings.SETTINGS_VERSION_KEY) == b"2"


def _row(page, name):
    """Return the HTML of the row of a setting."""

    row = page[page.index(f'id="setting-{name}"') :]
    return row[: row.index("</div>\n\t\t</div>\n")]


def test_page_shows_sources_and_locks_env_fields(app, admin_client, monkeypatch):
    monkeypatch.setitem(app.config, "ENV_LOCKED", frozenset({"EAC3_ATMOS_BITRATE"}))
    _store(app, "LOG_DIGEST_THRESHOLD", "30")

    page = admin_client.get("/settings").get_data(as_text=True)

    bitrate = _row(page, "EAC3_ATMOS_BITRATE")
    assert "disabled" in bitrate
    assert "Set in .env" in bitrate
    assert 'name="EAC3_ATMOS_BITRATE"' not in bitrate

    threshold = _row(page, "LOG_DIGEST_THRESHOLD")
    assert "Set on this page" in threshold
    assert 'value="30"' in threshold
    assert "Reset to default" in threshold
    assert 'id="reset-LOG_DIGEST_THRESHOLD"' in page


def test_member_cannot_open_the_page(user_client):
    response = user_client.get("/settings")
    assert response.status_code == 302


def _form(app, client, **changes):
    """Return the form data of the page with the current values and the changes."""

    from app.settings import SETTINGS, resolve

    data = {"csrf_token": page_csrf_token(client, "/settings"), "save_submit": "Save"}
    with app.app_context():
        for name, setting in SETTINGS.items():
            if name in app.config["ENV_LOCKED"]:
                continue
            value = changes.get(name, resolve(name)[0])
            if setting.kind == "flag":
                if value:
                    data[name] = "y"
            else:
                data[name] = str(value)
    return data


def test_save_stores_only_the_changes_and_logs_them(app, admin_client, log_capture):
    data = _form(app, admin_client, CREATE_ATMOS_TWINS=False, DVR_CHANNEL_FILMS=25)

    response = admin_client.post("/settings", data=data)

    assert response.status_code == 302
    assert _stored(app, "CREATE_ATMOS_TWINS") == "0"
    assert _stored(app, "DVR_CHANNEL_FILMS") == "25"
    assert _stored(app, "CREATE_FLAC_TWINS") is None
    assert _stored(app, "DISK_ALERT_FREE_GB") is None
    messages = [r.getMessage() for r in log_capture if r.levelno == logging.INFO]
    assert any(
        ADMIN_EMAIL in m and "CREATE_ATMOS_TWINS" in m and "False" in m
        for m in messages
    )

    from app.settings import get_setting

    with app.app_context():
        assert get_setting("CREATE_ATMOS_TWINS") is False
        assert get_setting("DVR_CHANNEL_FILMS") == 25


def test_a_stored_value_above_a_new_limit_does_not_block_a_save(
    app, admin_client, log_capture
):
    """Test that a value stored before the DVR cap does not block the page (#397).

    The field shows the cap. A save of a different setting goes through,
    and it stores the cap."""

    import re

    _store(app, "DVR_GENRE_CHANNELS", "50")
    _store(app, "DVR_TV_CHANNELS", "50")

    page = admin_client.get("/settings").get_data(as_text=True)
    shown = {
        name: re.search(rf'name="{name}"[^>]*value="(\d+)"', page).group(1)
        for name in ("DVR_GENRE_CHANNELS", "DVR_TV_CHANNELS")
    }
    assert shown == {"DVR_GENRE_CHANNELS": "39", "DVR_TV_CHANNELS": "40"}
    assert any("outside the page limits" in r.getMessage() for r in log_capture)

    data = _form(app, admin_client, DVR_CHANNEL_FILMS=25)
    data.update(shown)
    response = admin_client.post("/settings", data=data)

    assert response.status_code == 302
    assert _stored(app, "DVR_CHANNEL_FILMS") == "25"
    assert _stored(app, "DVR_GENRE_CHANNELS") == "39"
    assert _stored(app, "DVR_TV_CHANNELS") == "40"


def test_post_that_changes_a_locked_setting_is_refused(app, admin_client, monkeypatch):
    monkeypatch.setitem(app.config, "ENV_LOCKED", frozenset({"CREATE_FLAC_TWINS"}))
    data = _form(app, admin_client, CREATE_ATMOS_TWINS=False)
    data["CREATE_FLAC_TWINS"] = "y"

    response = admin_client.post("/settings", data=data, follow_redirects=True)

    assert "is set in .env" in response.get_data(as_text=True)
    assert _stored(app, "CREATE_FLAC_TWINS") is None
    assert _stored(app, "CREATE_ATMOS_TWINS") is None


def test_archive_needs_the_aws_settings(app, admin_client, monkeypatch):
    for key in ("AWS_BUCKET", "AWS_ACCESS_KEY", "AWS_SECRET_KEY"):
        monkeypatch.setitem(app.config, key, None)
    data = _form(app, admin_client, ARCHIVE_ORIGINAL_MEDIA=True, DISK_ALERT_FREE_GB=5)

    response = admin_client.post("/settings", data=data)

    page = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "AWS_BUCKET" in page and "Nothing is saved" in page
    assert _stored(app, "ARCHIVE_ORIGINAL_MEDIA") is None
    assert _stored(app, "DISK_ALERT_FREE_GB") is None

    monkeypatch.setitem(app.config, "AWS_BUCKET", "bucket")
    monkeypatch.setitem(app.config, "AWS_ACCESS_KEY", "access")
    monkeypatch.setitem(app.config, "AWS_SECRET_KEY", "secret")
    response = admin_client.post("/settings", data=data)

    assert response.status_code == 302
    assert _stored(app, "ARCHIVE_ORIGINAL_MEDIA") == "1"


def test_cdn_path_needs_the_cdn_settings(app, admin_client, monkeypatch):
    for key in ("CDN_DOMAIN", "CDN_KEY_PAIR_ID", "CDN_PRIVATE_KEY"):
        monkeypatch.setitem(app.config, key, None)
    data = _form(app, admin_client, AWS_DOWNLOAD_VIA_CDN=True)

    response = admin_client.post("/settings", data=data)

    assert response.status_code == 200
    assert "CDN_DOMAIN" in response.get_data(as_text=True)
    assert _stored(app, "AWS_DOWNLOAD_VIA_CDN") is None


def test_turning_a_guarded_setting_off_needs_no_guard(app, admin_client, monkeypatch):
    monkeypatch.setitem(app.config, "AWS_BUCKET", None)
    _store(app, "ARCHIVE_ORIGINAL_MEDIA", "1")
    data = _form(app, admin_client, ARCHIVE_ORIGINAL_MEDIA=False)

    assert admin_client.post("/settings", data=data).status_code == 302
    assert _stored(app, "ARCHIVE_ORIGINAL_MEDIA") == "0"


def test_bad_regex_and_bad_range_are_refused(app, admin_client):
    data = _form(app, admin_client, LOG_DIGEST_IGNORE="(", LOG_DIGEST_THRESHOLD=0)

    response = admin_client.post("/settings", data=data)

    assert response.status_code == 200
    assert _stored(app, "LOG_DIGEST_IGNORE") is None
    assert _stored(app, "LOG_DIGEST_THRESHOLD") is None

    data = _form(app, admin_client, LOG_DIGEST_IGNORE="(")
    response = admin_client.post("/settings", data=data)

    assert "not a valid regular expression" in response.get_data(as_text=True)
    assert _stored(app, "LOG_DIGEST_IGNORE") is None


_FOLDER_EXPORT = {
    "PresetList": [
        {"PresetName": "Fitzflix 1080p"},
        {
            "Folder": True,
            "PresetName": "My Presets",
            "ChildrenArray": [{"PresetName": "Fitzflix 720p"}],
        },
    ]
}


def test_preset_names_include_the_presets_in_folders():
    from app.settings import preset_names

    assert preset_names(_FOLDER_EXPORT) == ["Fitzflix 1080p", "Fitzflix 720p"]
    assert preset_names({}) == []
    assert preset_names([]) == []


def test_reset_removes_the_page_value(app, admin_client, log_capture):
    _store(app, "EAC3_ATMOS_BITRATE", "768000")
    data = {
        "csrf_token": page_csrf_token(admin_client, "/settings"),
        "setting_name": "EAC3_ATMOS_BITRATE",
        "reset_submit": "Reset to default",
    }

    response = admin_client.post("/settings", data=data)

    assert response.status_code == 302
    assert _stored(app, "EAC3_ATMOS_BITRATE") is None
    assert any(
        "reset EAC3_ATMOS_BITRATE" in r.getMessage() and ADMIN_EMAIL in r.getMessage()
        for r in log_capture
    )


def test_reset_of_a_locked_setting_is_refused(app, admin_client, monkeypatch):
    monkeypatch.setitem(app.config, "ENV_LOCKED", frozenset({"EAC3_ATMOS_BITRATE"}))
    _store(app, "EAC3_ATMOS_BITRATE", "768000")
    data = {
        "csrf_token": page_csrf_token(admin_client, "/settings"),
        "setting_name": "EAC3_ATMOS_BITRATE",
        "reset_submit": "Reset to default",
    }

    response = admin_client.post("/settings", data=data, follow_redirects=True)

    assert "is set in .env" in response.get_data(as_text=True)
    assert _stored(app, "EAC3_ATMOS_BITRATE") == "768000"


def test_post_without_csrf_is_refused(app, admin_client):
    data = _form(app, admin_client, CREATE_FLAC_TWINS=False)
    del data["csrf_token"]

    response = admin_client.post("/settings", data=data, follow_redirects=True)

    assert "The form is not valid" in response.get_data(as_text=True)
    assert _stored(app, "CREATE_FLAC_TWINS") is None


def test_registration_follows_the_page_value(app, client, monkeypatch):
    """PREVENT_ACCOUNT_CREATION on the page closes registration at once."""

    monkeypatch.setitem(app.config, "PREVENT_ACCOUNT_CREATION", False)
    assert client.get("/auth/register").status_code == 200

    _store(app, "PREVENT_ACCOUNT_CREATION", "1")
    response = client.get("/auth/register")
    assert response.status_code == 302


def test_get_setting_works_outside_an_app_context(app):
    """An rq worker runs a job without an app context."""

    from app.settings import get_setting

    _store(app, "DVR_CHANNEL_EPISODES", "12")
    assert get_setting("DVR_CHANNEL_EPISODES") == 12


def test_the_startup_check_reads_archival_from_the_page(app, monkeypatch, caplog):
    """The page can turn archival on. The startup check must see it.

    Before, the check read app.config and missed the page value."""

    from app import check_config

    monkeypatch.setitem(app.config, "ARCHIVE_ORIGINAL_MEDIA", False)
    monkeypatch.setitem(app.config, "AWS_BUCKET", None)
    monkeypatch.setitem(app.config, "ENV_LOCKED", frozenset())
    with caplog.at_level(logging.WARNING):
        check_config(app)
    assert "ARCHIVE_ORIGINAL_MEDIA is set" not in caplog.text

    _store(app, "ARCHIVE_ORIGINAL_MEDIA", "1")
    with caplog.at_level(logging.WARNING):
        check_config(app)
    assert "ARCHIVE_ORIGINAL_MEDIA is set, but uploads will fail" in caplog.text
