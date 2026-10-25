"""Test the one admin email path, notify_admin in app/email.py.

importing.py sent 6 admin emails by hand. Each site had its own admin
lookup and no guard for a database without an admin. Each site now
calls notify_admin. A site whose send must wait for a commit calls
admin_email."""


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
