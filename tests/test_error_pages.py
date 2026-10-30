"""Test that the 500 page renders during a Redis or database outage (#346)."""

from tests.conftest import ADMIN_EMAIL


def _render_500_as_admin(app):
    from flask_login import login_user

    from app.errors.handlers import internal_error
    from app.models import User

    with app.test_request_context("/"):
        login_user(User.query.filter_by(email=ADMIN_EMAIL).one())
        body, status = internal_error(None)
    return body, status


def test_the_500_page_renders_with_the_site_layout(app):
    body, status = _render_500_as_admin(app)
    assert status == 500
    assert "An unexpected error occurred" in body
    assert "navbar" in body


def test_the_500_page_renders_when_redis_is_away(app, monkeypatch):
    """The layout reads the queue details from Redis. A Redis outage makes
    the full page fail. Then the plain page answers."""

    from app.models import User

    def redis_away(self, *args, **kwargs):
        raise ConnectionError("Redis is away")

    monkeypatch.setattr(User, "get_queue_details", redis_away)
    body, status = _render_500_as_admin(app)
    assert status == 500
    assert "An unexpected error occurred" in body
    assert "navbar" not in body
