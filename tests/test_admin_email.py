"""Test the one admin email path, notify_admin in app/email.py.

importing.py sent 6 admin emails by hand. Each site had its own admin
lookup and no guard for a database without an admin. Each site now
calls notify_admin. A site whose send must wait for a commit calls
admin_email."""

import pytest


def _collect(app, monkeypatch):
    import app.email as email_module

    sent = []
    monkeypatch.setattr(
        email_module, "send_email", lambda *args, **kwargs: sent.append((args, kwargs))
    )
    return sent


def test_notify_admin_sends_to_the_first_admin(app, monkeypatch):
    from app.email import notify_admin
    from tests.conftest import ADMIN_EMAIL

    sent = _collect(app, monkeypatch)
    with app.test_request_context():
        notify_admin(
            "Received an inferior-quality file",
            "inferior_warning",
            basename="Inferior (2001) - [DVD].mkv",
            better_versions=[],
            rejects_directory="/rejects",
        )
    assert len(sent) == 1
    args, kwargs = sent[0]
    assert args[0] == "Fitzflix - Received an inferior-quality file"
    assert kwargs["recipients"] == [ADMIN_EMAIL]
    assert "Inferior (2001) - [DVD].mkv" in kwargs["text_body"]


def test_notify_admin_without_an_admin_sends_nothing(app, monkeypatch):
    import app.email as email_module

    sent = _collect(app, monkeypatch)
    monkeypatch.setattr(email_module, "admin_address", lambda: None)
    with app.test_request_context():
        email_module.notify_admin(
            "A conversion is not possible",
            "conversion_rejected",
            basename="x.mkv",
            reason="no encoder",
            rejects_directory="/rejects",
        )
        assert email_module.admin_email("x", "conversion_rejected") is None
    assert sent == []


def test_notify_admin_swallows_a_send_error(app, monkeypatch):
    import app.email as email_module

    def explode(*args, **kwargs):
        raise RuntimeError("mail server down")

    monkeypatch.setattr(email_module, "send_email", explode)
    with app.test_request_context():
        email_module.notify_admin(
            "A conversion is not possible",
            "conversion_rejected",
            basename="x.mkv",
            reason="no encoder",
            rejects_directory="/rejects",
        )


def test_admin_email_renders_now_and_sends_later(app, monkeypatch):
    """The bodies must show the records as they were before the commit."""

    from app.email import admin_email

    sent = _collect(app, monkeypatch)
    context = {
        "basename": "Before (2001).mkv",
        "reason": "r",
        "rejects_directory": "/r",
    }
    with app.test_request_context():
        send = admin_email(
            "A conversion is not possible", "conversion_rejected", **context
        )
        context["basename"] = "After (2001).mkv"
        assert sent == []
        send()
    assert "Before (2001).mkv" in sent[0][1]["text_body"]


def test_importing_has_no_inline_admin_lookup():
    """Pin the consolidation: no site in importing.py looks up the admin itself."""

    import inspect

    import app.importing as importing

    source = inspect.getsource(importing)
    assert "User.admin" not in source
    assert "recipients=[admin_user" not in source


def test_notify_admin_text_sends_the_given_bodies(app, monkeypatch):
    from app.email import notify_admin_text
    from tests.conftest import ADMIN_EMAIL

    sent = _collect(app, monkeypatch)
    with app.app_context():
        notify_admin_text("Rename collision needs triage", "plain", "<p>html</p>")
    args, kwargs = sent[0]
    assert args[0] == "Fitzflix - Rename collision needs triage"
    assert kwargs["recipients"] == [ADMIN_EMAIL]
    assert (kwargs["text_body"], kwargs["html_body"]) == ("plain", "<p>html</p>")


def test_no_module_outside_email_looks_up_the_admin():
    """Pin the consolidation: only app/email.py finds the admin row.

    Each of the 10 sites that queried the admin row itself dereferenced
    a row that can be None. The alias send_email_async is gone too."""

    import pathlib

    offenders = []
    for path in sorted(pathlib.Path("app").rglob("*.py")):
        if path.name == "email.py" and path.parent.name == "app":
            continue
        source = path.read_text()
        if "User.admin == True" in source or "send_email_async" in source:
            offenders.append(str(path))
    assert offenders == []


@pytest.fixture
def no_admin(app, monkeypatch):
    """Make the admin lookup find nobody. Record what notify_admin would send."""

    import app.email as email_module

    sent = _collect(app, monkeypatch)
    monkeypatch.setattr(email_module, "admin_address", lambda: None)
    return sent


def test_tmdb_refresh_without_an_admin_still_returns(app, no_admin):
    from app import db
    from app.videos import apply_tmdb_refresh
    from tests.factories import make_movie

    with app.app_context():
        movie_id = make_movie("Unmatched Film", 1999).id
        db.session.commit()

    assert apply_tmdb_refresh("Movies", movie_id, notify_if_missing=True) is True
    assert no_admin == []


def test_aws_reports_without_an_admin_still_return(app, no_admin):
    from types import SimpleNamespace

    from app import aws_storage, db
    from tests.factories import make_movie, make_movie_file

    with app.app_context():
        file = make_movie_file(make_movie("Claimed Film", 1999), "DVD")
        file.aws_key = "untouched/Claimed Film (1999) - [DVD].mkv"
        db.session.commit()
        assert aws_storage._report_dangling_claims(SimpleNamespace(keys=set())) is None
        assert aws_storage._mail_reports(["untouched/stray.mkv"], [(1, "x")]) is None
    assert no_admin == []


def test_incomplete_download_without_an_admin_still_returns(
    app, no_admin, monkeypatch, tmp_path
):
    from app.api import arr as arr_module

    monkeypatch.setattr(arr_module, "mark_grab_failed", lambda *args: False)
    junk = tmp_path / "Junk (1999) - [WEBDL-1080p].mkv"
    junk.write_bytes(b"partial")
    with app.app_context():
        assert (
            arr_module.reject_incomplete_download(
                "Radarr", {"downloadId": "abc"}, str(junk), None
            )
            is False
        )
    assert junk.exists()
    assert no_admin == []


def test_manual_import_email_without_an_admin_still_returns(app, no_admin, monkeypatch):
    from app.manual_imports import email_manual_import_needed

    monkeypatch.setitem(app.config, "MAIL_SERVER", "smtp.test")
    with app.test_request_context():
        assert email_manual_import_needed("Radarr", "A Title", "a reason") is None
    assert no_admin == []
