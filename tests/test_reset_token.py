"""Test the password-reset token of the User model and the page that reads it."""

from time import time

import jwt
import pytest

from tests.conftest import MEMBER_EMAIL, MEMBER_PASSWORD, page_csrf_token

UNKNOWN_USER_ID = 999999

# The SECRET_KEY of the test config is shorter than the 32 bytes that
# PyJWT recommends for HS256. The warning about that is noise here.

pytestmark = pytest.mark.filterwarnings("ignore::jwt.warnings.InsecureKeyLengthWarning")


def member(app):
    """Return the member user. The caller must hold an app context."""

    from app.models import User

    return User.query.filter_by(email=MEMBER_EMAIL).one()


def sign_token(app, payload, key=None, algorithm="HS256"):
    """Return a token with the given payload, signed with the app key by default."""

    return jwt.encode(payload, key or app.config["SECRET_KEY"], algorithm=algorithm)


@pytest.fixture
def restore_member_password(app):
    """Set the member password back after a test that changes it.

    The user table stays for the full session. Thus, a changed password
    would leak into the other test files."""

    yield
    from app import db

    with app.app_context():
        member(app).set_password(MEMBER_PASSWORD)
        db.session.commit()


def test_token_identifies_its_user(app):
    """Test that a new token gives back the user that made it."""

    from app.models import User

    with app.app_context():
        user = member(app)
        token = user.get_reset_password_token()

        assert isinstance(token, str)
        assert User.verify_reset_password_token(token).id == user.id


def test_token_carries_the_expiry_time(app):
    """Test that the token expires after the given number of seconds."""

    with app.app_context():
        token = member(app).get_reset_password_token(expires_in=120)

    claims = jwt.decode(token, app.config["SECRET_KEY"], algorithms=["HS256"])
    assert 110 < claims["exp"] - time() <= 120


def test_expired_token_is_refused(app):
    """Test that a token past its expiry time identifies no user."""

    from app.models import User

    with app.app_context():
        token = member(app).get_reset_password_token(expires_in=-5)

        assert User.verify_reset_password_token(token) is None


def test_changed_token_is_refused(app):
    """Test that a token with a changed payload or signature identifies no user."""

    from app.models import User

    with app.app_context():
        token = member(app).get_reset_password_token()
        header, payload, signature = token.split(".")
        other_payload = sign_token(
            app, {"reset_password": UNKNOWN_USER_ID, "exp": time() + 600}
        ).split(".")[1]

        for changed in (
            f"{header}.{other_payload}.{signature}",
            f"{header}.{payload}.{signature[::-1]}",
            f"{header}.{payload}.",
        ):
            assert User.verify_reset_password_token(changed) is None


def test_token_from_a_different_key_is_refused(app):
    """Test that a token signed with a different key identifies no user."""

    from app.models import User

    with app.app_context():
        payload = {"reset_password": member(app).id, "exp": time() + 600}
        token = sign_token(app, payload, key="a-different-key-of-32-bytes-long")

        assert User.verify_reset_password_token(token) is None


def test_unsigned_token_is_refused(app):
    """Test that a token with the "none" algorithm identifies no user."""

    from app.models import User

    with app.app_context():
        payload = {"reset_password": member(app).id, "exp": time() + 600}
        token = jwt.encode(payload, None, algorithm="none")

        assert User.verify_reset_password_token(token) is None


def test_token_without_a_user_is_refused(app):
    """Test that a correctly signed token for no known user gives None."""

    from app.models import User

    with app.app_context():
        unknown = sign_token(
            app, {"reset_password": UNKNOWN_USER_ID, "exp": time() + 600}
        )
        no_claim = sign_token(app, {"exp": time() + 600})

        assert User.verify_reset_password_token(unknown) is None
        assert User.verify_reset_password_token(no_claim) is None


@pytest.mark.parametrize("token", ["", "not-a-token", "a.b.c"])
def test_malformed_token_is_refused(app, token):
    """Test that text that is not a token identifies no user."""

    from app.models import User

    with app.app_context():
        assert User.verify_reset_password_token(token) is None


def test_reset_page_shows_the_form_for_a_good_token(app, client):
    """Test that the page shows the form only when the token is good."""

    with app.app_context():
        good = member(app).get_reset_password_token()
        expired = member(app).get_reset_password_token(expires_in=-5)

    response = client.get(f"/auth/reset-password/{good}")
    assert response.status_code == 200
    assert 'name="password2"' in response.get_data(as_text=True)

    for bad in (expired, "not-a-token"):
        response = client.get(f"/auth/reset-password/{bad}")
        assert response.status_code == 302
        assert 'name="password2"' not in response.get_data(as_text=True)


def test_reset_page_sets_the_new_password(app, client, restore_member_password):
    """Test that a good token lets the user set a new password."""

    with app.app_context():
        path = f"/auth/reset-password/{member(app).get_reset_password_token()}"

    response = client.post(
        path,
        data={
            "csrf_token": page_csrf_token(client, path),
            "password": "a-new-password",
            "password2": "a-new-password",
        },
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/auth/login")

    with app.app_context():
        assert member(app).check_password("a-new-password")
        assert not member(app).check_password(MEMBER_PASSWORD)


def test_reset_page_keeps_the_password_for_a_bad_token(app, client):
    """Test that a post with an expired token does not change the password."""

    with app.app_context():
        good = member(app).get_reset_password_token()
        expired = member(app).get_reset_password_token(expires_in=-5)

    response = client.post(
        f"/auth/reset-password/{expired}",
        data={
            "csrf_token": page_csrf_token(client, f"/auth/reset-password/{good}"),
            "password": "a-new-password",
            "password2": "a-new-password",
        },
    )
    assert response.status_code == 302

    with app.app_context():
        assert member(app).check_password(MEMBER_PASSWORD)
