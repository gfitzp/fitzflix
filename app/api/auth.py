import secrets

from flask import request

from app.models import User

# Fitzflix compares this key when no user matches the request. Thus, a
# missing account takes the same time to reject as a wrong key.

_DUMMY_KEY = secrets.token_hex(16)


def authenticate_api_request():
    """Return the user that the Basic auth header identifies, or None.

    The password field must hold the API key of the user. The admin page
    shows this key.
    """

    auth = request.authorization
    if not auth or not auth.get("username") or not auth.get("password"):
        return None

    user = User.query.filter_by(email=auth.get("username")).first()
    presented = auth.get("password")

    expected = user.api_key if user and user.api_key else _DUMMY_KEY
    if secrets.compare_digest(expected, presented) and user:
        return user

    return None
