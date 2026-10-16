"""The Admin Settings page and the get_setting precedence.

A setting that .env sets is locked, and the page cannot change it. A
setting that .env does not set takes the page value, if one exists.
Otherwise it takes the config.py default. The page refuses archival
without the AWS settings and the CloudFront path without the CDN
settings.
"""

import logging

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
    monkeypatch.setenv("SKIP_FLAC_TWINS", "")
    monkeypatch.setenv("DISK_ALERT_FREE_GB", "")
    monkeypatch.setenv("HANDBRAKE_PRESET", "Fast 1080p30")

    assert config._env_locked() == {"SKIP_FLAC_TWINS", "HANDBRAKE_PRESET"}


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


def test_cache_holds_for_some_seconds(app, monkeypatch):
    """Each process reads the table at most one time in CACHE_SECONDS."""

    from app import settings

    clock = [1000.0]
    monkeypatch.setattr(settings.time, "monotonic", lambda: clock[0])
    with app.app_context():
        assert settings.get_setting("SKIP_FLAC_TWINS") is False
        _store(app, "SKIP_FLAC_TWINS", "1", expire=False)
        assert settings.get_setting("SKIP_FLAC_TWINS") is False
        clock[0] += settings.CACHE_SECONDS
        assert settings.get_setting("SKIP_FLAC_TWINS") is True


def _row(page, name):
    """Return the HTML of the row of a setting."""

    row = page[page.index(f'id="setting-{name}"') :]
    return row[: row.index("</div>\n\t\t</div>\n")]


def test_page_shows_sources_and_locks_env_fields(app, admin_client, monkeypatch):
    monkeypatch.setitem(app.config, "ENV_LOCKED", frozenset({"HANDBRAKE_PRESET"}))
    _store(app, "LOG_DIGEST_THRESHOLD", "30")

    page = admin_client.get("/settings").get_data(as_text=True)

    preset = _row(page, "HANDBRAKE_PRESET")
    assert "disabled" in preset
    assert "Set in .env" in preset
    assert 'name="HANDBRAKE_PRESET"' not in preset

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
    data = _form(app, admin_client, SKIP_ATMOS_TWINS=True, DVR_CHANNEL_FILMS=25)

    response = admin_client.post("/settings", data=data)

    assert response.status_code == 302
    assert _stored(app, "SKIP_ATMOS_TWINS") == "1"
    assert _stored(app, "DVR_CHANNEL_FILMS") == "25"
    assert _stored(app, "SKIP_FLAC_TWINS") is None
    assert _stored(app, "DISK_ALERT_FREE_GB") is None
    messages = [r.getMessage() for r in log_capture if r.levelno == logging.INFO]
    assert any(
        ADMIN_EMAIL in m and "SKIP_ATMOS_TWINS" in m and "True" in m for m in messages
    )

    from app.settings import get_setting

    with app.app_context():
        assert get_setting("SKIP_ATMOS_TWINS") is True
        assert get_setting("DVR_CHANNEL_FILMS") == 25


def test_post_that_changes_a_locked_setting_is_refused(app, admin_client, monkeypatch):
    monkeypatch.setitem(app.config, "ENV_LOCKED", frozenset({"SKIP_FLAC_TWINS"}))
    data = _form(app, admin_client, SKIP_ATMOS_TWINS=True)
    data["SKIP_FLAC_TWINS"] = "y"

    response = admin_client.post("/settings", data=data, follow_redirects=True)

    assert "is set in .env" in response.get_data(as_text=True)
    assert _stored(app, "SKIP_FLAC_TWINS") is None
    assert _stored(app, "SKIP_ATMOS_TWINS") is None


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


def test_preset_must_be_in_the_preset_file(app, admin_client, monkeypatch, tmp_path):
    preset_file = tmp_path / "presets.json"
    preset_file.write_text('{"PresetList": [{"PresetName": "Fitzflix 1080p"}]}')
    monkeypatch.setitem(app.config, "HANDBRAKE_PRESET_FILE", str(preset_file))

    data = _form(app, admin_client, HANDBRAKE_PRESET="Other preset")
    response = admin_client.post("/settings", data=data)
    assert "does not contain this preset" in response.get_data(as_text=True)
    assert _stored(app, "HANDBRAKE_PRESET") is None

    data = _form(app, admin_client, HANDBRAKE_PRESET="Fitzflix 1080p")
    assert admin_client.post("/settings", data=data).status_code == 302
    assert _stored(app, "HANDBRAKE_PRESET") == "Fitzflix 1080p"


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
    data = _form(app, admin_client, SKIP_FLAC_TWINS=True)
    del data["csrf_token"]

    response = admin_client.post("/settings", data=data, follow_redirects=True)

    assert "The form is not valid" in response.get_data(as_text=True)
    assert _stored(app, "SKIP_FLAC_TWINS") is None


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
