import secrets

from flask import request

from app.models import User

# Compared when no user matches the request, so a missing account takes the
# same time to reject as a wrong key

_DUMMY_KEY = secrets.token_hex(16)


def authenticate_api_request():
    """Return the user authenticated by the request's Basic auth header, else None.

    The password field must hold the user's API key, shown on the admin page.
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
