"""Test the passkey sign-in, the passkey-only account, and the confirmation (#669).

A software authenticator stands in for the browser. It holds an ES256
key pair, answers a registration with a "none" attestation, and signs
a sign-in. The JSON fields are base64url, as the browser sends them.
"""

import hashlib
import json
import struct

import cbor2
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from webauthn.helpers import bytes_to_base64url

from tests.conftest import (
    ADMIN_EMAIL,
    ADMIN_PASSWORD,
    MEMBER_EMAIL,
    MEMBER_PASSWORD,
    _signed_in_client,
    csrf_token_from,
    page_csrf_token,
)

ORIGIN = "http://localhost"
RP_ID = "localhost"


class SoftAuthenticator:
    """A software authenticator with 1 ES256 credential and a sign counter.

    A synced passkey reports a count of 0 each time. Then the library
    does not check the count, and only the challenge stops a replay."""

    def __init__(self, credential_id=b"soft-credential-0001", synced=False):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = credential_id
        self.counter = 0
        self.step = 0 if synced else 1

    def _cose_key(self):
        numbers = self.key.public_key().public_numbers()
        return cbor2.dumps(
            {
                1: 2,
                3: -7,
                -1: 1,
                -2: numbers.x.to_bytes(32, "big"),
                -3: numbers.y.to_bytes(32, "big"),
            }
        )

    @staticmethod
    def _client_data(kind, challenge):
        return json.dumps(
            {
                "type": kind,
                "challenge": challenge,
                "origin": ORIGIN,
                "crossOrigin": False,
            }
        ).encode()

    def register(self, options):
        """Answer a registration. The flags are user present, verified, attested."""

        client_data = self._client_data("webauthn.create", options["challenge"])
        auth_data = (
            hashlib.sha256(RP_ID.encode()).digest()
            + bytes([0x45])
            + struct.pack(">I", 0)
            + bytes(16)
            + struct.pack(">H", len(self.credential_id))
            + self.credential_id
            + self._cose_key()
        )
        attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        encoded = bytes_to_base64url(self.credential_id)
        return {
            "id": encoded,
            "rawId": encoded,
            "type": "public-key",
            "clientExtensionResults": {},
            "authenticatorAttachment": "platform",
            "response": {
                "clientDataJSON": bytes_to_base64url(client_data),
                "attestationObject": bytes_to_base64url(attestation),
                "transports": ["internal", "hybrid"],
            },
        }

    def sign(self, options, tamper=False, origin=ORIGIN):
        """Answer a sign-in. The counter grows by 1 each time, or stays 0 if synced."""

        self.counter += self.step
        client_data = json.dumps(
            {
                "type": "webauthn.get",
                "challenge": options["challenge"],
                "origin": origin,
                "crossOrigin": False,
            }
        ).encode()
        auth_data = (
            hashlib.sha256(RP_ID.encode()).digest()
            + bytes([0x05])
            + struct.pack(">I", self.counter)
        )
        signature = self.key.sign(
            auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256())
        )
        if tamper:
            signature = signature[:-1] + bytes([signature[-1] ^ 1])
        encoded = bytes_to_base64url(self.credential_id)
        return {
            "id": encoded,
            "rawId": encoded,
            "type": "public-key",
            "clientExtensionResults": {},
            "response": {
                "clientDataJSON": bytes_to_base64url(client_data),
                "authenticatorData": bytes_to_base64url(auth_data),
                "signature": bytes_to_base64url(signature),
                "userHandle": None,
            },
        }


@pytest.fixture
def member_password_restored(app):
    """Put the password of the member back after a test that removes it."""

    yield
    from app import db
    from app.models import User

    with app.app_context():
        user = User.query.filter_by(email=MEMBER_EMAIL).one()
        user.set_password(MEMBER_PASSWORD)
        db.session.commit()


def proof(client, token, password=None, prover=None):
    """Return the proof of identity for a Profile post (#675).

    With prover, the proof is a passkey confirmation that the Profile
    page asked for. Otherwise, it is the password."""

    if prover is None:
        return {"password": password or ""}
    options = confirm_options(client, token, "/profile")
    return {"passkey_assertion": json.dumps(prover.sign(options))}


def register_options(client, token, password=MEMBER_PASSWORD, prover=None):
    """Post the registration options request with a proof. Return the answer."""

    return client.post(
        "/auth/passkey/register/options",
        json=proof(client, token, password, prover),
        headers={"X-CSRFToken": token},
    )


def register_passkey(
    client, authenticator, name="Phone", password=MEMBER_PASSWORD, prover=None
):
    """Run the 2 registration posts for a signed-in client. Return the second answer."""

    token = page_csrf_token(client, "/profile")
    options = register_options(client, token, password, prover).get_json()
    return client.post(
        "/auth/passkey/register",
        json={"credential": authenticator.register(options), "name": name},
        headers={"X-CSRFToken": token},
    )


def passkey_sign_in(app, authenticator, **kwargs):
    """Run the 2 sign-in posts from a fresh client. Return the client and the answer."""

    client = app.test_client()
    token = page_csrf_token(client, "/auth/login")
    options = client.post(
        "/auth/passkey/login/options", headers={"X-CSRFToken": token}
    ).get_json()
    response = client.post(
        "/auth/passkey/login",
        json={"credential": authenticator.sign(options, **kwargs)},
        headers={"X-CSRFToken": token},
    )
    return client, response


def confirm_options(client, token, path):
    """Ask for the options of a confirmation on the form at path (#676)."""

    return client.post(
        "/auth/passkey/confirm/options",
        json={"path": path},
        headers={"X-CSRFToken": token},
    ).get_json()


def member_with_passkey(app):
    """Return a signed-in member client and the authenticator of its new passkey."""

    client = _signed_in_client(app, MEMBER_EMAIL)
    authenticator = SoftAuthenticator()
    assert register_passkey(client, authenticator).status_code == 200
    return client, authenticator


def test_the_sign_in_page_offers_a_passkey(app):
    page = app.test_client().get("/auth/login").get_data(as_text=True)
    assert "Sign in with a passkey" in page
    assert "/auth/passkey/login/options" in page


def test_a_user_adds_a_passkey_and_the_profile_lists_it(app):
    from app.models import Passkey, User

    client = _signed_in_client(app, MEMBER_EMAIL)
    authenticator = SoftAuthenticator()
    response = register_passkey(client, authenticator, name="Work laptop")
    assert response.status_code == 200
    assert response.get_json()["redirect"] == "/profile"
    with app.app_context():
        member = User.query.filter_by(email=MEMBER_EMAIL).one()
        passkey = Passkey.query.filter_by(user_id=member.id).one()
        assert passkey.credential_id == authenticator.credential_id
        assert passkey.name == "Work laptop"
        assert passkey.transports == "internal,hybrid"
    page = client.get("/profile").get_data(as_text=True)
    assert "Work laptop" in page
    assert "Remove my password" in page


def test_a_registration_needs_the_csrf_header(app):
    client = _signed_in_client(app, MEMBER_EMAIL)
    response = client.post("/auth/passkey/register/options")
    assert response.status_code == 400
    assert "out of date" in response.get_json()["error"]


def test_a_passkey_signs_the_user_in(app):
    _, authenticator = member_with_passkey(app)
    client, response = passkey_sign_in(app, authenticator)
    assert response.status_code == 200
    assert response.get_json()["redirect"] == "/index"
    page = client.get("/profile")
    assert page.status_code == 200
    assert MEMBER_EMAIL in page.get_data(as_text=True)


def test_a_tampered_signature_is_refused(app):
    _, authenticator = member_with_passkey(app)
    client, response = passkey_sign_in(app, authenticator, tamper=True)
    assert response.status_code == 401
    assert "passkey check failed" in response.get_json()["error"]
    assert client.get("/profile").status_code == 302


def test_an_answer_from_another_origin_is_refused(app):
    _, authenticator = member_with_passkey(app)
    _, response = passkey_sign_in(app, authenticator, origin="https://evil.test")
    assert response.status_code == 401


def test_an_unknown_passkey_is_refused(app):
    _, response = passkey_sign_in(app, SoftAuthenticator(b"never-registered"))
    assert response.status_code == 401
    assert "does not know" in response.get_json()["error"]


def test_a_challenge_serves_one_time(app):
    _, authenticator = member_with_passkey(app)
    client = app.test_client()
    token = page_csrf_token(client, "/auth/login")
    options = client.post(
        "/auth/passkey/login/options", headers={"X-CSRFToken": token}
    ).get_json()
    first = client.post(
        "/auth/passkey/login",
        json={"credential": authenticator.sign(options)},
        headers={"X-CSRFToken": token},
    )
    assert first.status_code == 200
    client.get("/auth/logout")
    second = client.post(
        "/auth/passkey/login",
        json={"credential": authenticator.sign(options)},
        headers={"X-CSRFToken": token},
    )
    assert second.status_code == 401
    assert "expired" in second.get_json()["error"]


def test_a_sign_count_that_does_not_grow_is_refused(app):
    """A cloned authenticator repeats a counter. The library refuses it."""

    _, authenticator = member_with_passkey(app)
    _, response = passkey_sign_in(app, authenticator)
    assert response.status_code == 200
    authenticator.counter = 0
    _, response = passkey_sign_in(app, authenticator)
    assert response.status_code == 401


def test_a_password_remove_needs_a_passkey(app):
    from app.models import User

    client = _signed_in_client(app, MEMBER_EMAIL)
    token = page_csrf_token(client, "/profile")
    response = client.post(
        "/profile",
        data={
            "csrf_token": token,
            "password": MEMBER_PASSWORD,
            "remove_password_submit": "Remove my password",
        },
        follow_redirects=True,
    )
    assert "Add a passkey before you remove your password." in response.get_data(
        as_text=True
    )
    with app.app_context():
        assert User.query.filter_by(email=MEMBER_EMAIL).one().has_password


def test_a_passkey_only_account(app, member_password_restored):
    """Remove the password, stay signed in, and sign in with the passkey only."""

    from app.models import User

    client, authenticator = member_with_passkey(app)
    token = page_csrf_token(client, "/profile")
    response = client.post(
        "/profile",
        data={
            "csrf_token": token,
            "password": MEMBER_PASSWORD,
            "remove_password_submit": "Remove my password",
        },
        follow_redirects=True,
    )
    page = response.get_data(as_text=True)
    assert "You sign in with a passkey now." in page
    assert "This account has no password." in page
    assert "Email me a link to set a password" in page

    # The session that removed the password stays signed in.
    assert client.get("/profile").status_code == 200

    with app.app_context():
        member = User.query.filter_by(email=MEMBER_EMAIL).one()
        assert not member.has_password
        assert member.check_password(MEMBER_PASSWORD) is False
        assert member.check_password("") is False

    # The password form fails as a plain wrong password.
    fresh = app.test_client()
    token = page_csrf_token(fresh, "/auth/login")
    response = fresh.post(
        "/auth/login",
        data={"csrf_token": token, "email": MEMBER_EMAIL, "password": MEMBER_PASSWORD},
    )
    assert response.status_code == 302
    assert "/auth/login" in response.headers["Location"]

    # The passkey signs in.
    signed, response = passkey_sign_in(app, authenticator)
    assert response.status_code == 200
    assert signed.get("/profile").status_code == 200


def test_the_last_passkey_of_a_passkey_only_account_stays(
    app, member_password_restored
):
    from app.models import Passkey

    client, authenticator = member_with_passkey(app)
    token = page_csrf_token(client, "/profile")
    client.post(
        "/profile",
        data={
            "csrf_token": token,
            "password": MEMBER_PASSWORD,
            "remove_password_submit": "Remove my password",
        },
    )
    with app.app_context():
        passkey_id = Passkey.query.one().id
    token = page_csrf_token(client, "/profile")
    response = client.post(
        "/profile",
        data={
            "csrf_token": token,
            "passkey_id": str(passkey_id),
            "passkey_delete_submit": "Remove",
            **proof(client, token, prover=authenticator),
        },
        follow_redirects=True,
    )
    assert "your only way to sign in" in response.get_data(as_text=True)
    with app.app_context():
        assert Passkey.query.count() == 1


def test_a_passkey_delete_with_a_password_works(app):
    from app.models import Passkey

    client, _ = member_with_passkey(app)
    with app.app_context():
        passkey_id = Passkey.query.one().id
    token = page_csrf_token(client, "/profile")
    response = client.post(
        "/profile",
        data={
            "csrf_token": token,
            "passkey_id": str(passkey_id),
            "password": MEMBER_PASSWORD,
            "passkey_delete_submit": "Remove",
        },
        follow_redirects=True,
    )
    assert "Removed the passkey" in response.get_data(as_text=True)
    with app.app_context():
        assert Passkey.query.count() == 0


def test_a_user_cannot_delete_the_passkey_of_another_user(app):
    from app.models import Passkey

    member_with_passkey(app)
    with app.app_context():
        passkey_id = Passkey.query.one().id
    admin = _signed_in_client(app, ADMIN_EMAIL)
    token = page_csrf_token(admin, "/profile")
    response = admin.post(
        "/profile",
        data={
            "csrf_token": token,
            "passkey_id": str(passkey_id),
            "password": ADMIN_PASSWORD,
            "passkey_delete_submit": "Remove",
        },
        follow_redirects=True,
    )
    assert "not on your account" in response.get_data(as_text=True)
    with app.app_context():
        assert Passkey.query.count() == 1


def test_the_reset_link_sets_a_password_on_a_passkey_only_account(
    app, member_password_restored
):
    from app.models import User

    client, _ = member_with_passkey(app)
    token = page_csrf_token(client, "/profile")
    client.post(
        "/profile",
        data={
            "csrf_token": token,
            "password": MEMBER_PASSWORD,
            "remove_password_submit": "Remove my password",
        },
    )
    with app.app_context():
        member = User.query.filter_by(email=MEMBER_EMAIL).one()
        reset_token = member.get_reset_password_token()

    # The signed-in user follows the link of the email.
    page = client.get(f"/auth/reset-password/{reset_token}")
    assert page.status_code == 200
    response = client.post(
        f"/auth/reset-password/{reset_token}",
        data={
            "csrf_token": csrf_token_from(page.get_data(as_text=True)),
            "password": "a-new-password",
            "password2": "a-new-password",
        },
    )
    assert response.status_code == 302
    with app.app_context():
        member = User.query.filter_by(email=MEMBER_EMAIL).one()
        assert member.check_password("a-new-password")


def test_the_sync_form_takes_a_passkey_in_place_of_the_password(app, monkeypatch):
    """The 6 forms that cost money share identity_confirmed (#669)."""

    from app.main import admin

    calls = []
    monkeypatch.setattr(admin, "enqueue_task", lambda name, *a, **k: calls.append(name))
    monkeypatch.setattr(admin, "start_bulk_run", lambda *a, **k: True)
    client = _signed_in_client(app, ADMIN_EMAIL)
    authenticator = SoftAuthenticator(b"admin-credential")
    assert (
        register_passkey(client, authenticator, password=ADMIN_PASSWORD).status_code
        == 200
    )

    page = client.get("/maintenance").get_data(as_text=True)
    assert 'name="passkey_assertion"' in page
    assert 'data-options-url="/auth/passkey/confirm/options"' in page
    assert "Confirm with passkey" not in page
    token = csrf_token_from(page)
    options = confirm_options(client, token, "/maintenance")
    assert options["allowCredentials"][0]["id"] == bytes_to_base64url(
        b"admin-credential"
    )
    response = client.post(
        "/maintenance",
        data={
            "csrf_token": token,
            "passkey_assertion": json.dumps(authenticator.sign(options)),
            "sync_submit": "Sync",
        },
        follow_redirects=True,
    )
    assert "Incorrect password" not in response.get_data(as_text=True)
    assert calls == ["app.videos.sync_aws_s3_storage_task"]


def test_each_archived_restore_form_takes_a_passkey(app, monkeypatch):
    """The Held imports page has 1 restore form for each archived original."""

    from app import db
    from app.main import admin
    from app.models import ArchivedOriginal

    calls = []
    monkeypatch.setattr(admin, "request_restore", lambda key, **k: calls.append(key))
    client = _signed_in_client(app, ADMIN_EMAIL)
    authenticator = SoftAuthenticator(b"admin-held-credential")
    assert (
        register_passkey(client, authenticator, password=ADMIN_PASSWORD).status_code
        == 200
    )
    with app.app_context():
        rows = [
            ArchivedOriginal(
                media_library="Movies",
                quality_title="Bluray-2160p",
                untouched_basename=f"Passkey Test {number} (1984) - [Bluray-2160p].mkv",
                aws_key=f"untouched/Passkey Test {number} (1984) - [Bluray-2160p].mkv",
                reason="archive only",
            )
            for number in (1, 2)
        ]
        db.session.add_all(rows)
        db.session.commit()
        ids = [row.id for row in rows]
    try:
        page = client.get("/maintenance/held-imports").get_data(as_text=True)
        forms = page.count('name="restore_submit"')
        assert forms >= 2
        assert page.count('name="passkey_assertion"') == forms
        assert page.count('name="password"') == forms
        assert 'id="password"' not in page
        assert "required" not in page.split('name="password"')[1].split(">")[0]
        token = csrf_token_from(page)
        options = confirm_options(client, token, "/maintenance/held-imports")
        response = client.post(
            "/maintenance/held-imports",
            data={
                "csrf_token": token,
                "archived_id": str(ids[0]),
                "passkey_assertion": json.dumps(authenticator.sign(options)),
                "restore_submit": "Restore",
            },
            follow_redirects=True,
        )
        assert "Requesting a restore" in response.get_data(as_text=True)
        assert calls == ["untouched/Passkey Test 1 (1984) - [Bluray-2160p].mkv"]
    finally:
        with app.app_context():
            ArchivedOriginal.query.filter(ArchivedOriginal.id.in_(ids)).delete(
                synchronize_session=False
            )
            db.session.commit()


def test_a_confirmation_with_the_passkey_of_another_user_fails(app, monkeypatch):
    from app.main import admin

    calls = []
    monkeypatch.setattr(admin, "enqueue_task", lambda name, *a, **k: calls.append(name))
    _, member_authenticator = member_with_passkey(app)
    client = _signed_in_client(app, ADMIN_EMAIL)
    page = client.get("/maintenance").get_data(as_text=True)
    token = csrf_token_from(page)
    options = confirm_options(client, token, "/maintenance")
    assert options["allowCredentials"] == []
    response = client.post(
        "/maintenance",
        data={
            "csrf_token": token,
            "passkey_assertion": json.dumps(member_authenticator.sign(options)),
            "sync_submit": "Sync",
        },
        follow_redirects=True,
    )
    assert "does not know this passkey" in response.get_data(as_text=True)
    assert calls == []


def test_a_blank_post_to_a_restore_form_is_a_wrong_password(app, admin_client):
    page = admin_client.get("/maintenance").get_data(as_text=True)
    response = admin_client.post(
        "/maintenance",
        data={"csrf_token": csrf_token_from(page), "sync_submit": "Sync"},
        follow_redirects=True,
    )
    assert "Incorrect password provided!" in response.get_data(as_text=True)


# The challenge waits in Redis and serves 1 time (#676).


def _sync_post(client, token, assertion):
    """Post the S3 sync form of the Maintenance page with a passkey assertion."""

    return client.post(
        "/maintenance",
        data={
            "csrf_token": token,
            "passkey_assertion": json.dumps(assertion),
            "sync_submit": "Sync",
        },
        follow_redirects=True,
    )


@pytest.fixture
def admin_passkey(app, monkeypatch):
    """Return a signed-in admin client, its authenticator, and the queued sync names."""

    from app.main import admin

    calls = []
    monkeypatch.setattr(admin, "enqueue_task", lambda name, *a, **k: calls.append(name))
    monkeypatch.setattr(admin, "start_bulk_run", lambda *a, **k: True)
    client = _signed_in_client(app, ADMIN_EMAIL)
    authenticator = SoftAuthenticator(b"admin-replay-credential", synced=True)
    assert (
        register_passkey(client, authenticator, password=ADMIN_PASSWORD).status_code
        == 200
    )
    return client, authenticator, calls


def test_the_session_cookie_holds_no_challenge(app):
    from app.auth.passkeys import CHALLENGE_KEY

    client = app.test_client()
    token = page_csrf_token(client, "/auth/login")
    options = client.post(
        "/auth/passkey/login/options", headers={"X-CSRFToken": token}
    ).get_json()
    with client.session_transaction() as session:
        ceremony_id = session["passkey_challenge"]
    assert options["challenge"] not in str(ceremony_id)
    key = CHALLENGE_KEY.format(ceremony_id=ceremony_id)
    assert options["challenge"] in app.redis.get(key).decode()
    assert 0 < app.redis.ttl(key) <= 300


def test_a_replayed_cookie_does_not_repeat_a_passkey_sign_in(app):
    client = _signed_in_client(app, MEMBER_EMAIL)
    authenticator = SoftAuthenticator(synced=True)
    assert register_passkey(client, authenticator).status_code == 200
    client = app.test_client()
    token = page_csrf_token(client, "/auth/login")
    options = client.post(
        "/auth/passkey/login/options", headers={"X-CSRFToken": token}
    ).get_json()
    cookie = client.get_cookie("session").value
    answer = authenticator.sign(options)
    first = client.post(
        "/auth/passkey/login",
        json={"credential": answer},
        headers={"X-CSRFToken": token},
    )
    assert first.status_code == 200

    # A second browser has the cookie from before the answer and the body.
    replay = app.test_client()
    replay.set_cookie("session", cookie, domain="localhost")
    second = replay.post(
        "/auth/passkey/login",
        json={"credential": answer},
        headers={"X-CSRFToken": token},
    )
    assert second.status_code == 401
    assert "expired" in second.get_json()["error"]
    assert replay.get("/profile").status_code == 302


def test_a_replayed_cookie_does_not_repeat_a_confirmation(app, admin_passkey):
    client, authenticator, calls = admin_passkey
    token = csrf_token_from(client.get("/maintenance").get_data(as_text=True))
    options = confirm_options(client, token, "/maintenance")
    cookie = client.get_cookie("session").value
    answer = authenticator.sign(options)
    _sync_post(client, token, answer)
    assert calls == ["app.videos.sync_aws_s3_storage_task"]

    client.set_cookie("session", cookie, domain="localhost")
    response = _sync_post(client, token, answer)
    assert "The passkey request expired." in response.get_data(as_text=True)
    assert calls == ["app.videos.sync_aws_s3_storage_task"]


def test_a_confirmation_for_one_form_fails_on_another(app, admin_passkey, monkeypatch):
    from app.main import admin

    restores = []
    monkeypatch.setattr(admin, "request_restore", lambda key, **k: restores.append(key))
    client, authenticator, calls = admin_passkey
    token = csrf_token_from(client.get("/maintenance").get_data(as_text=True))
    options = confirm_options(client, token, "/maintenance/held-imports")
    response = _sync_post(client, token, authenticator.sign(options))
    assert "The passkey request expired." in response.get_data(as_text=True)
    assert calls == []


def test_a_confirmation_needs_a_local_path(app, admin_passkey):
    client, _, _ = admin_passkey
    token = csrf_token_from(client.get("/maintenance").get_data(as_text=True))
    for body in ({}, {"path": "https://evil.test/maintenance"}, {"path": ["/x"]}):
        response = client.post(
            "/auth/passkey/confirm/options",
            json=body,
            headers={"X-CSRFToken": token},
        )
        assert response.status_code == 400
        assert "not for a form of this site" in response.get_json()["error"]


def test_an_expired_challenge_is_refused(app):
    import time

    _, authenticator = member_with_passkey(app)
    client = app.test_client()
    token = page_csrf_token(client, "/auth/login")
    options = client.post(
        "/auth/passkey/login/options", headers={"X-CSRFToken": token}
    ).get_json()
    for key in app.redis.scan_iter("fitzflix:passkey:challenge:*"):
        app.redis.pexpire(key, 1)
    time.sleep(0.01)
    response = client.post(
        "/auth/passkey/login",
        json={"credential": authenticator.sign(options)},
        headers={"X-CSRFToken": token},
    )
    assert response.status_code == 401
    assert "expired" in response.get_json()["error"]


def test_a_login_challenge_does_not_serve_a_confirmation(app, admin_passkey):
    client, authenticator, calls = admin_passkey
    token = csrf_token_from(client.get("/maintenance").get_data(as_text=True))
    options = client.post(
        "/auth/passkey/login/options", headers={"X-CSRFToken": token}
    ).get_json()
    response = _sync_post(client, token, authenticator.sign(options))
    assert "The passkey request expired." in response.get_data(as_text=True)
    assert calls == []


def test_a_redis_fault_refuses_the_passkey_request(app, monkeypatch):
    import redis

    _, authenticator = member_with_passkey(app)
    client = app.test_client()
    token = page_csrf_token(client, "/auth/login")
    options = client.post(
        "/auth/passkey/login/options", headers={"X-CSRFToken": token}
    ).get_json()

    def down(*args, **kwargs):
        raise redis.exceptions.ConnectionError("Redis is down")

    monkeypatch.setattr(app.redis, "set", down)
    monkeypatch.setattr(app.redis, "getdel", down)
    response = client.post(
        "/auth/passkey/login/options", headers={"X-CSRFToken": token}
    )
    assert response.status_code == 400
    assert response.get_json()["error"] == "The passkey request failed. Try again."
    response = client.post(
        "/auth/passkey/login",
        json={"credential": authenticator.sign(options)},
        headers={"X-CSRFToken": token},
    )
    assert response.status_code == 401
    assert response.get_json()["error"] == "The passkey request failed. Try again."


# A change of the sign-in methods ends each other session (#673).


def _remove_password(client, password=MEMBER_PASSWORD):
    """Post the Remove my password form of the Profile page. Check that it worked."""

    token = page_csrf_token(client, "/profile")
    response = client.post(
        "/profile",
        data={
            "csrf_token": token,
            "password": password,
            "remove_password_submit": "Remove my password",
        },
        follow_redirects=True,
    )
    assert "You sign in with a passkey now." in response.get_data(as_text=True)
    return response


def _delete_passkey(client, passkey_id):
    """Post the Remove form of 1 passkey on the Profile page."""

    token = page_csrf_token(client, "/profile")
    return client.post(
        "/profile",
        data={
            "csrf_token": token,
            "passkey_id": str(passkey_id),
            "password": MEMBER_PASSWORD,
            "passkey_delete_submit": "Remove",
        },
        follow_redirects=True,
    )


def _reset_password(app, password):
    """Set a new password for the member with the link of the reset email."""

    from app.models import User

    with app.app_context():
        reset_token = (
            User.query.filter_by(email=MEMBER_EMAIL).one().get_reset_password_token()
        )
    client = app.test_client()
    page = client.get(f"/auth/reset-password/{reset_token}")
    assert page.status_code == 200
    response = client.post(
        f"/auth/reset-password/{reset_token}",
        data={
            "csrf_token": csrf_token_from(page.get_data(as_text=True)),
            "password": password,
            "password2": password,
        },
    )
    assert response.headers["Location"] == "/auth/login"
    return reset_token


def test_removing_a_passkey_ends_the_other_sessions(app):
    from app.models import Passkey

    client, _ = member_with_passkey(app)
    other = _signed_in_client(app, MEMBER_EMAIL)
    assert other.get("/profile").status_code == 200
    with app.app_context():
        passkey_id = Passkey.query.one().id
    response = _delete_passkey(client, passkey_id)
    assert "Removed the passkey" in response.get_data(as_text=True)
    assert client.get("/profile").status_code == 200
    assert other.get("/profile").status_code == 302


def test_removing_the_password_ends_the_other_sessions(app, member_password_restored):
    client, _ = member_with_passkey(app)
    other = _signed_in_client(app, MEMBER_EMAIL)
    _remove_password(client)
    assert client.get("/profile").status_code == 200
    assert other.get("/profile").status_code == 302


def test_a_new_email_address_ends_the_other_sessions(app, member_email_restored):
    """The reset email goes to the address. Thus, a new address ends each other session."""

    client = _signed_in_client(app, MEMBER_EMAIL)
    other = _signed_in_client(app, MEMBER_EMAIL)
    token = page_csrf_token(client)
    done, _ = _profile_action(
        app, "email", client, token, {"password": MEMBER_PASSWORD}
    )
    assert done
    assert client.get("/profile").status_code == 200
    assert other.get("/profile").status_code == 302


@pytest.mark.parametrize(
    "route",
    (
        "/auth/passkey/register/options",
        "/auth/passkey/register",
        "/auth/passkey/confirm/options",
        "/auth/passkey/login",
    ),
)
@pytest.mark.parametrize("body", ([1], 5, "text"))
def test_a_json_body_that_is_no_object_is_a_plain_refusal(app, route, body):
    client = _signed_in_client(app, MEMBER_EMAIL)
    token = page_csrf_token(client)
    response = client.post(route, json=body, headers={"X-CSRFToken": token})
    assert response.status_code in (400, 401)


def test_a_session_of_an_earlier_passkey_only_period_stays_ended(
    app, member_password_restored
):
    client, _ = member_with_passkey(app)
    _remove_password(client)
    old_session = _signed_in_client(app, MEMBER_EMAIL)
    cookie = old_session.get_cookie("session").value
    assert old_session.get("/profile").status_code == 200

    _reset_password(app, "a-new-password")
    assert old_session.get("/profile").status_code == 302

    _remove_password(_signed_in_client(app, MEMBER_EMAIL), "a-new-password")
    replay = app.test_client()
    replay.set_cookie("session", cookie, domain="localhost")
    assert replay.get("/profile").status_code == 302


def test_a_remembered_browser_keeps_a_new_remember_cookie(
    app, member_password_restored
):
    """The acting browser gets a new remember cookie. The old cookie stops loading."""

    client = _signed_in_client(app, MEMBER_EMAIL)
    authenticator = SoftAuthenticator()
    assert register_passkey(client, authenticator).status_code == 200
    signed = app.test_client()
    token = page_csrf_token(signed, "/auth/login")
    options = signed.post(
        "/auth/passkey/login/options", headers={"X-CSRFToken": token}
    ).get_json()
    response = signed.post(
        "/auth/passkey/login",
        json={"credential": authenticator.sign(options), "remember": True},
        headers={"X-CSRFToken": token},
    )
    assert response.status_code == 200
    old_remember = signed.get_cookie("remember_token").value

    _remove_password(signed)
    new_remember = signed.get_cookie("remember_token").value
    assert new_remember != old_remember
    for value, status in ((new_remember, 200), (old_remember, 302)):
        browser = app.test_client()
        browser.set_cookie("remember_token", value, domain="localhost")
        assert browser.get("/profile").status_code == status


def test_a_session_without_a_remember_cookie_gets_none(app, member_password_restored):
    client, _ = member_with_passkey(app)
    _remove_password(client)
    assert client.get_cookie("remember_token") is None
    assert client.get("/profile").status_code == 200


def test_a_reset_link_works_one_time(app, member_password_restored):
    from app.models import User

    reset_token = _reset_password(app, "a-new-password")
    with app.app_context():
        assert (
            User.query.filter_by(email=MEMBER_EMAIL)
            .one()
            .check_password("a-new-password")
        )
        assert User.verify_reset_password_token(reset_token) is None
    response = app.test_client().get(f"/auth/reset-password/{reset_token}")
    assert response.status_code == 302


def test_a_new_password_ends_each_session(app, member_password_restored):
    other = _signed_in_client(app, MEMBER_EMAIL)
    _reset_password(app, "a-new-password")
    assert other.get("/profile").status_code == 302


def test_a_new_user_gets_a_session_epoch(app):
    from app import db
    from app.models import User

    with app.app_context():
        user = User(email="epoch@example.test")
        db.session.add(user)
        db.session.commit()
        try:
            assert len(user.session_epoch) == 32
            int(user.session_epoch, 16)
            other = User.query.filter_by(email=MEMBER_EMAIL).one()
            assert user.session_epoch != other.session_epoch
        finally:
            db.session.delete(user)
            db.session.commit()


def test_the_migration_ends_each_older_session(app):
    """The upgrade gives each user a new random epoch. Old cookies stop loading."""

    import importlib.util
    import os

    from app import db
    from app.models import User

    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "migrations",
        "versions",
        "c8e4f1a2d305_session_epoch.py",
    )
    spec = importlib.util.spec_from_file_location("session_epoch_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.down_revision == "b7c3d9e1f204"

    member = _signed_in_client(app, MEMBER_EMAIL)
    assert member.get("/profile").status_code == 200
    with app.app_context():
        before = {u.id: u.session_epoch for u in User.query.all()}
        migration.give_each_user_an_epoch(db.session.connection())
        db.session.commit()
        db.session.expire_all()
        after = {u.id: u.session_epoch for u in User.query.all()}
    assert all(after[i] != before[i] and len(after[i]) == 32 for i in before)
    assert len(set(after.values())) == len(after)
    assert member.get("/profile").status_code == 302


# A change of the sign-in methods needs a fresh proof of identity (#675).

NEW_EMAIL = "member-new@example.com"


@pytest.fixture
def member_email_restored(app):
    """Put the email address of the member back after a test that changes it."""

    yield
    from app import db
    from app.models import User

    with app.app_context():
        user = User.query.filter_by(email=NEW_EMAIL).first()
        if user is not None:
            user.email = MEMBER_EMAIL
            db.session.commit()


def _proof_for(kind, client, token, authenticator):
    """Return the proof fields of 1 kind: none, wrong, password or passkey."""

    if kind == "none":
        return {}
    if kind == "wrong":
        return {"password": "not-the-password"}
    if kind == "password":
        return {"password": MEMBER_PASSWORD}
    return proof(client, token, prover=authenticator)


def _profile_action(app, action, client, token, fields):
    """Post 1 Profile action with the proof fields. Return (done, page text)."""

    from app.models import User

    if action == "add_passkey":
        response = client.post(
            "/auth/passkey/register/options",
            json=fields,
            headers={"X-CSRFToken": token},
        )
        body = response.get_json()
        return response.status_code == 200 and "challenge" in body, str(body)
    data = {"csrf_token": token, **fields}
    if action == "email":
        data.update(email=NEW_EMAIL, email2=NEW_EMAIL, submit="Update")
    elif action == "delete_passkey":
        with app.app_context():
            member = User.query.filter_by(email=MEMBER_EMAIL).one()
            passkey_id = member.passkeys.one().id
        data.update(passkey_id=str(passkey_id), passkey_delete_submit="Remove")
    else:
        data.update(remove_password_submit="Remove my password")
    page = client.post("/profile", data=data, follow_redirects=True)
    with app.app_context():
        changed = User.query.filter_by(email=NEW_EMAIL).first() is not None
        member = User.query.filter_by(email=NEW_EMAIL if changed else MEMBER_EMAIL)
        member = member.one()
        done = {
            "email": changed,
            "delete_passkey": member.passkeys.count() == 0,
            "remove_password": not member.has_password,
        }[action]
    return done, page.get_data(as_text=True)


ACTIONS = ("email", "delete_passkey", "remove_password", "add_passkey")


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("kind", ("none", "wrong"))
def test_a_profile_change_with_no_good_proof_is_refused(
    app, member_password_restored, member_email_restored, action, kind
):
    client, authenticator = member_with_passkey(app)
    token = page_csrf_token(client, "/profile")
    fields = _proof_for(kind, client, token, authenticator)
    done, text = _profile_action(app, action, client, token, fields)
    assert not done
    assert "Incorrect password provided!" in text


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("kind", ("password", "passkey"))
def test_a_profile_change_with_a_proof_passes(
    app, member_password_restored, member_email_restored, action, kind
):
    client, authenticator = member_with_passkey(app)
    token = page_csrf_token(client, "/profile")
    fields = _proof_for(kind, client, token, authenticator)
    done, text = _profile_action(app, action, client, token, fields)
    assert done, text
    assert "Incorrect password provided!" not in text


@pytest.mark.parametrize("action", ACTIONS)
def test_the_passkey_of_another_user_is_no_proof(
    app, member_password_restored, member_email_restored, action
):
    admin = _signed_in_client(app, ADMIN_EMAIL)
    admin_key = SoftAuthenticator(b"admin-profile-credential")
    assert (
        register_passkey(admin, admin_key, password=ADMIN_PASSWORD).status_code == 200
    )
    client, _ = member_with_passkey(app)
    token = page_csrf_token(client, "/profile")
    fields = proof(client, token, prover=admin_key)
    done, text = _profile_action(app, action, client, token, fields)
    assert not done
    assert "does not know this passkey" in text


def test_a_confirmation_for_another_form_is_no_proof_for_a_new_passkey(app):
    client, authenticator = member_with_passkey(app)
    token = page_csrf_token(client, "/profile")
    options = confirm_options(client, token, "/maintenance")
    fields = {"passkey_assertion": json.dumps(authenticator.sign(options))}
    done, text = _profile_action(app, "add_passkey", client, token, fields)
    assert not done
    assert "expired" in text


def test_a_session_cookie_alone_cannot_add_a_passkey(app):
    from app.models import Passkey

    client = _signed_in_client(app, MEMBER_EMAIL)
    token = page_csrf_token(client, "/profile")
    response = client.post(
        "/auth/passkey/register/options", headers={"X-CSRFToken": token}
    )
    assert response.status_code == 400
    assert response.get_json()["error"] == "Incorrect password provided!"
    response = client.post(
        "/auth/passkey/register",
        json={"credential": SoftAuthenticator().register({"challenge": "AAAA"})},
        headers={"X-CSRFToken": token},
    )
    assert response.status_code == 400
    with app.app_context():
        assert Passkey.query.count() == 0


def test_a_passkey_only_account_adds_a_passkey_with_a_passkey(
    app, member_password_restored
):
    from app.models import Passkey

    client, authenticator = member_with_passkey(app)
    _remove_password(client)
    second = SoftAuthenticator(b"soft-credential-0002")
    response = register_passkey(client, second, name="Tablet", prover=authenticator)
    assert response.status_code == 200
    with app.app_context():
        assert Passkey.query.count() == 2

    # The password is no proof for an account with no password.
    token = page_csrf_token(client, "/profile")
    response = register_options(client, token, MEMBER_PASSWORD)
    assert response.status_code == 400


def _forms_of(page):
    """Return the text of each form of a page."""

    return [part.split("</form>")[0] for part in page.split("<form")[1:]]


PROFILE_SUBMITS = (
    'name="submit"',
    'name="passkey_delete_submit"',
    'name="remove_password_submit"',
    "data-passkey-register",
)


def _identity_forms(page):
    """Return the Profile forms that change a way in to the account."""

    return [
        form
        for form in _forms_of(page)
        if any(submit in form for submit in PROFILE_SUBMITS)
    ]


def test_the_profile_forms_of_a_password_account_ask_for_the_password(app):
    page = _signed_in_client(app, MEMBER_EMAIL).get("/profile").get_data(as_text=True)
    forms = _identity_forms(page)
    # The email form and the add form. No passkey: no remove forms.
    assert len(forms) == 2
    for form in forms:
        assert 'name="password"' in form
        assert 'name="passkey_assertion"' not in form
    assert 'id="password"' not in page


def test_the_profile_forms_of_an_account_with_a_passkey_ask_for_it(
    app, member_password_restored
):
    client, _ = member_with_passkey(app)
    page = client.get("/profile").get_data(as_text=True)
    forms = _identity_forms(page)
    # The email form, 1 passkey remove form, the add form, the password remove form.
    assert len(forms) == 4
    for form in forms:
        assert 'name="password"' in form
        assert 'name="passkey_assertion"' in form
        assert 'data-options-url="/auth/passkey/confirm/options"' in form

    _remove_password(client)
    page = client.get("/profile").get_data(as_text=True)
    forms = _identity_forms(page)
    # The email form, the passkey remove form, the add form.
    assert len(forms) == 3
    for form in forms:
        assert 'name="password"' not in form
        assert 'name="passkey_assertion"' in form


def test_a_wrong_proof_password_counts_as_a_failed_sign_in(app):
    """The proof check has the lock of the sign-in. Thus, it is no place to guess."""

    from app.auth import throttle

    client = _signed_in_client(app, MEMBER_EMAIL)
    token = page_csrf_token(client, "/profile")
    head = {"X-CSRFToken": token}
    try:
        for _ in range(throttle.ACCOUNT_FAILURE_LIMIT):
            response = client.post(
                "/auth/passkey/register/options",
                json={"password": "a-guess"},
                headers=head,
            )
            assert response.status_code == 400
        locked = client.post(
            "/auth/passkey/register/options",
            json={"password": MEMBER_PASSWORD},
            headers=head,
        )
        assert locked.status_code == 400
        assert "challenge" not in locked.get_json()
        assert "Too many" in locked.get_json()["error"]
    finally:
        for key in app.redis.scan_iter(throttle.KEY_PREFIX + "*"):
            app.redis.delete(key)


def test_a_passkey_name_keeps_no_line_break(app):
    client = _signed_in_client(app, MEMBER_EMAIL)
    authenticator = SoftAuthenticator()
    forged = "phone\n2026-11-10 10:00:00,0 ERROR: forged"
    register_passkey(client, authenticator, name=forged)
    from app import db
    from app.models import Passkey, User

    with app.app_context():
        member = User.query.filter_by(email=MEMBER_EMAIL).one()
        try:
            names = [p.name for p in member.passkeys]
            assert names and all("\n" not in name for name in names)
        finally:
            Passkey.query.filter_by(user_id=member.id).delete()
            db.session.commit()
