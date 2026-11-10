"""Sign in with a passkey (#669).

A passkey is a WebAuthn credential. The authenticator of the browser
holds the private key. Fitzflix holds the public key in the passkey
table. Each ceremony has 2 steps. The first step gives the browser its
options with a fresh challenge. The second step checks the answer of
the authenticator against that challenge. The challenge waits in
Redis, and the session holds only its random id. It serves 1 time, for
1 kind of ceremony, and for 5 minutes at most. A second answer to the
same challenge finds nothing, also with an older copy of the session
cookie (#676).

There are 3 kinds of ceremony. A "register" makes a new passkey for
the signed-in user. A "login" takes any passkey of this site, with no
email address. A "confirm" takes a passkey of the signed-in user in
place of the password on the restore forms. A confirmation is good
only on the path of the form that asked for it.
"""

import json
import secrets
from datetime import datetime
from urllib.parse import urlsplit

from flask import current_app, flash, request, session
from flask_login import current_user
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import (
    base64url_to_bytes,
    bytes_to_base64url,
    parse_authentication_credential_json,
)
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from app import db, local_path
from app.models import Passkey
from app.redis_guard import REDIS_DOWN_ERRORS, warn_redis_down

RP_NAME = "Fitzflix"
CHALLENGE_SECONDS = 300
PENDING_CEREMONY = "passkey_challenge"
CHALLENGE_KEY = "fitzflix:passkey:challenge:{ceremony_id}"

# The verify functions raise a family of exceptions. A malformed post
# also raises a plain ValueError or a KeyError from the parser.

VERIFY_ERRORS = (WebAuthnException, ValueError, KeyError, TypeError)


class PasskeyError(Exception):
    """A passkey ceremony failed. The message is safe to show to the user."""


EXPIRED = "The passkey request expired. Try again."
REDIS_FAULT = "The passkey request failed. Try again."
WRONG_PASSWORD = "Incorrect password provided!"


def relying_party_host():
    """Return the host that the passkeys of this site are bound to.

    This is the host of SERVER_NAME. Without SERVER_NAME, it is the
    host of the request. A browser accepts a passkey only on this host.
    Thus, a passkey does not work on a LAN address of the server."""

    return current_app.config.get("SERVER_NAME") or request.host


def relying_party_id():
    """Return the relying party id: the host with no port."""

    return urlsplit("//" + relying_party_host()).hostname


def expected_origin():
    """Return the origin that the browser signs into the client data.

    gunicorn sees plain http from the proxy (#454). The browser is on
    the scheme of PREFERRED_URL_SCHEME."""

    scheme = current_app.config.get("PREFERRED_URL_SCHEME") or "http"
    return f"{scheme}://{relying_party_host()}"


def _store_challenge(kind, challenge, user_id=None, path=None):
    """Put the challenge of a new ceremony in Redis. It replaces an older one.

    The session holds only the random id of the Redis key. Thus, a new
    id replaces the older one, and the older key expires unused. Each
    key expires after CHALLENGE_SECONDS. path is the form path that a
    confirmation is good for. A Redis fault raises PasskeyError."""

    ceremony_id = secrets.token_hex(16)
    record = {
        "kind": kind,
        "challenge": bytes_to_base64url(challenge),
        "user_id": user_id,
        "path": path,
    }
    try:
        current_app.redis.set(
            CHALLENGE_KEY.format(ceremony_id=ceremony_id),
            json.dumps(record),
            ex=CHALLENGE_SECONDS,
        )
    except REDIS_DOWN_ERRORS as error:
        warn_redis_down("The passkey ceremony", error)
        raise PasskeyError(REDIS_FAULT)
    session[PENDING_CEREMONY] = ceremony_id


def _take_challenge(kind, path=None):
    """Return the challenge and the user id of the pending ceremony, and forget it.

    GETDEL reads and deletes the Redis key in 1 step. Thus, a second
    answer to the same challenge finds nothing. The ceremony must be of
    the given kind. A confirmation must also come on the path that
    asked for it."""

    ceremony_id = session.pop(PENDING_CEREMONY, None)
    if not isinstance(ceremony_id, str):
        raise PasskeyError(EXPIRED)
    try:
        raw = current_app.redis.getdel(CHALLENGE_KEY.format(ceremony_id=ceremony_id))
    except REDIS_DOWN_ERRORS as error:
        warn_redis_down("The passkey ceremony", error)
        raise PasskeyError(REDIS_FAULT)
    pending = json.loads(raw) if raw else None
    if not pending or pending.get("kind") != kind or pending.get("path") != path:
        raise PasskeyError(EXPIRED)
    return base64url_to_bytes(pending["challenge"]), pending.get("user_id")


def _descriptors(user):
    """Return the credential descriptors of the passkeys of a user."""

    return [PublicKeyCredentialDescriptor(id=p.credential_id) for p in user.passkeys]


def registration_options(user):
    """Return the JSON options for the browser to make a passkey for the user.

    The passkey is discoverable. Thus, a sign-in needs no email
    address. The passkeys that the user has are excluded. Thus, the
    browser does not make a second passkey on the same authenticator."""

    options = generate_registration_options(
        rp_id=relying_party_id(),
        rp_name=RP_NAME,
        user_id=str(user.id).encode(),
        user_name=user.email,
        user_display_name=user.email,
        exclude_credentials=_descriptors(user),
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
    )
    _store_challenge("register", options.challenge, user.id)
    return options_to_json(options)


def finish_registration(user, credential, name=None):
    """Check the answer of the authenticator and store the new passkey. Return it.

    credential is the JSON object that the browser made from the
    PublicKeyCredential. A failure raises PasskeyError."""

    challenge, user_id = _take_challenge("register")
    if user_id != user.id:
        raise PasskeyError("The passkey request was for another account.")
    try:
        verified = verify_registration_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=relying_party_id(),
            expected_origin=expected_origin(),
        )
    except VERIFY_ERRORS as error:
        current_app.logger.warning(
            f"Passkey registration for {user.email} failed: {error}"
        )
        raise PasskeyError("The browser answer was not valid. Try again.")

    transports = None
    if isinstance(credential, dict):
        listed = (credential.get("response") or {}).get("transports") or []
        transports = ",".join(str(t) for t in listed)[:128] or None
    passkey = Passkey(
        user_id=user.id,
        credential_id=verified.credential_id,
        public_key=verified.credential_public_key,
        sign_count=verified.sign_count,
        transports=transports,
        name=(name or "").strip()[:64] or None,
    )
    db.session.add(passkey)
    db.session.commit()
    current_app.logger.info(
        f"Passkey added: {user.email} added '{passkey.display_name}'"
    )
    return passkey


def authentication_options(user=None, path=None):
    """Return the JSON options for a sign-in, or for a confirmation by the user.

    With no user, this is a sign-in. The browser offers each passkey of
    this site. With a user, this is a confirmation. Only the passkeys
    of that user are allowed, and the answer is good only on path. A
    path that is not a local path raises PasskeyError."""

    if user is not None and not (isinstance(path, str) and local_path(path)):
        raise PasskeyError("The passkey request was not for a form of this site.")
    options = generate_authentication_options(
        rp_id=relying_party_id(),
        allow_credentials=_descriptors(user) if user else [],
        user_verification=UserVerificationRequirement.PREFERRED,
    )
    if user is None:
        _store_challenge("login", options.challenge)
    else:
        _store_challenge("confirm", options.challenge, user.id, path)
    return options_to_json(options)


def finish_authentication(credential, kind="login", user=None, path=None):
    """Check a sign-in or a confirmation answer. Return the passkey that signed it.

    The passkey must exist, and for a confirmation it must belong to
    the user. A confirmation must come on the path that asked for it.
    The sign count must grow, unless the authenticator reports none. A
    failure raises PasskeyError."""

    challenge, _ = _take_challenge(kind, path)
    try:
        parsed = parse_authentication_credential_json(credential)
    except VERIFY_ERRORS:
        raise PasskeyError("The browser answer was not valid. Try again.")
    passkey = Passkey.query.filter_by(credential_id=parsed.raw_id).first()
    if passkey is None or (user is not None and passkey.user_id != user.id):
        raise PasskeyError("Fitzflix does not know this passkey.")
    try:
        verified = verify_authentication_response(
            credential=parsed,
            expected_challenge=challenge,
            expected_rp_id=relying_party_id(),
            expected_origin=expected_origin(),
            credential_public_key=passkey.public_key,
            credential_current_sign_count=passkey.sign_count,
        )
    except VERIFY_ERRORS as error:
        current_app.logger.warning(
            f"Passkey {kind} for {passkey.user.email} failed: {error}"
        )
        raise PasskeyError("The passkey check failed. Try again.")
    passkey.sign_count = verified.new_sign_count
    passkey.date_last_used = datetime.utcnow()
    db.session.commit()
    return passkey


def check_identity(password, assertion, path):
    """Raise PasskeyError unless the proof is good for the signed-in user.

    The proof is the password of the account, or a passkey assertion
    that a confirmation on path asked for. The restore forms, the S3
    sync form, the Profile forms and the passkey registration share
    this check (#675)."""

    if assertion:
        finish_authentication(assertion, kind="confirm", user=current_user, path=path)
    elif not (isinstance(password, str) and current_user.check_password(password)):
        raise PasskeyError(WRONG_PASSWORD)


def identity_confirmed(form):
    """Return True if the post proves the identity of the signed-in user.

    A restore costs real money, and a change of the sign-in methods
    can take the account. Thus, these forms ask for proof. The proof is
    the password of the form, or the passkey assertion that the page
    put in the form. A failure flashes its message here (#669)."""

    try:
        check_identity(
            form.password.data, request.form.get("passkey_assertion"), request.path
        )
    except PasskeyError as error:
        flash(str(error), "danger")
        return False
    return True
