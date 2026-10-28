"""Test that the account forms refuse values longer than their columns (#329).

MariaDB runs with STRICT_TRANS_TABLES. A value that is too long for its
VARCHAR column makes the commit fail with a 500. SQLite does not check
the length. Thus, these tests check the form validation and the message."""

import re

from tests.conftest import MEMBER_EMAIL, _signed_in_client


def _token(page):
    return re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page).group(1)


def test_profile_refuses_a_letterboxd_username_longer_than_64(app):
    from app.models import User

    client = _signed_in_client(app, MEMBER_EMAIL)
    token = _token(client.get("/profile").get_data(as_text=True))
    response = client.post(
        "/profile",
        data={
            "csrf_token": token,
            "letterboxd_username": "x" * 65,
            "letterboxd_submit": "Update Letterboxd Sync",
        },
    )
    assert response.status_code == 200
    assert "cannot be longer than 64 characters" in response.get_data(as_text=True)
    with app.app_context():
        assert (
            User.query.filter_by(email=MEMBER_EMAIL).one().letterboxd_username is None
        )


def test_profile_refuses_a_letterboxd_username_with_a_slash(app):
    """Test that a name outside letters, digits, and underscores is refused (#400)."""

    from app.models import User

    client = _signed_in_client(app, MEMBER_EMAIL)
    for value in ("alice/following", "alice?x=1", "al ice"):
        token = _token(client.get("/profile").get_data(as_text=True))
        response = client.post(
            "/profile",
            data={
                "csrf_token": token,
                "letterboxd_username": value,
                "letterboxd_submit": "Update Letterboxd Sync",
            },
        )
        assert response.status_code == 200
        assert "Use only letters, digits, and underscores." in response.get_data(
            as_text=True
        )
    with app.app_context():
        assert (
            User.query.filter_by(email=MEMBER_EMAIL).one().letterboxd_username is None
        )


def test_letterboxd_feed_url_quotes_the_username(app, monkeypatch):
    from app import letterboxd

    urls = []

    class Answer:
        text = "<rss/>"

        def raise_for_status(self):
            pass

    monkeypatch.setattr(
        letterboxd.requests, "get", lambda url, **kwargs: urls.append(url) or Answer()
    )
    with app.app_context():
        assert letterboxd.fetch_letterboxd_feed("alice/following?x=1") == "<rss/>"
    assert urls == ["https://letterboxd.com/alice%2Ffollowing%3Fx%3D1/rss/"]


def test_profile_refuses_a_plex_username_longer_than_64(app):
    from app.models import User

    client = _signed_in_client(app, MEMBER_EMAIL)
    with app.app_context():
        before = User.query.filter_by(email=MEMBER_EMAIL).one().plex_username
    token = _token(client.get("/profile").get_data(as_text=True))
    response = client.post(
        "/profile",
        data={
            "csrf_token": token,
            "plex_username": "p" * 65,
            "plex_submit": "Update Plex Mapping",
        },
    )
    assert "cannot be longer than 64 characters" in response.get_data(as_text=True)
    with app.app_context():
        assert User.query.filter_by(email=MEMBER_EMAIL).one().plex_username == before


def test_registration_refuses_an_email_longer_than_120(app, client, monkeypatch):
    from app.models import User

    monkeypatch.setitem(app.config, "PREVENT_ACCOUNT_CREATION", False)
    email = "a" * 110 + "@example.com"
    token = _token(client.get("/auth/register").get_data(as_text=True))
    response = client.post(
        "/auth/register",
        data={
            "csrf_token": token,
            "email": email,
            "password": "pw",
            "password2": "pw",
            "submit": "Register",
        },
    )
    assert response.status_code == 200
    assert "cannot be longer than 120 characters" in response.get_data(as_text=True)
    with app.app_context():
        assert User.query.filter_by(email=email).first() is None


def test_a_concurrent_duplicate_registration_gets_the_message(app, client, monkeypatch):
    """The second of 2 posts with one email gets the message, not a 500 (#507).

    The form check passes for both posts. The unique index refuses the
    second row."""

    from app.auth.forms import RegistrationForm
    from app.models import User

    from app import db

    monkeypatch.setitem(app.config, "PREVENT_ACCOUNT_CREATION", False)
    email = "first@example.com"
    with app.app_context():
        db.session.add(User(email=email, api_key="d" * 32))
        db.session.commit()
    try:
        monkeypatch.setattr(
            RegistrationForm, "validate_email", lambda self, field: None
        )
        token = _token(client.get("/auth/register").get_data(as_text=True))
        response = client.post(
            "/auth/register",
            data={
                "csrf_token": token,
                "email": email,
                "password": "pw",
                "password2": "pw",
                "submit": "Register",
            },
        )
        assert response.status_code == 200
        assert "Please use a different email address." in response.get_data(
            as_text=True
        )
        with app.app_context():
            assert User.query.filter_by(email=email).count() == 1
    finally:
        # The user table survives between tests
        with app.app_context():
            User.query.filter_by(email=email).delete()
            db.session.commit()
