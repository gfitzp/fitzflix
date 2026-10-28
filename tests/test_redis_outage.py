"""Test that a Redis outage leaves the pages up (#476).

A Redis client on a closed port raises redis.ConnectionError on each
call, as the real one does during an outage."""

import logging

import pytest
import redis


@pytest.fixture
def redis_down(app, monkeypatch):
    """Point app.redis and every queue at a closed port."""

    from app import redis_guard
    from app.queues import roster_queues

    from redis.backoff import NoBackoff
    from redis.retry import Retry

    dead = redis.Redis(
        host="127.0.0.1",
        port=1,
        socket_connect_timeout=0.2,
        retry=Retry(NoBackoff(), 0),
    )
    monkeypatch.setattr(app, "redis", dead)
    for queue in roster_queues(app):
        monkeypatch.setattr(queue, "connection", dead)
    monkeypatch.setitem(redis_guard._last_warning, "at", None)
    return dead


def test_the_queue_badge_is_empty_while_redis_is_down(app, redis_down, caplog):
    from app.models import User

    with app.app_context():
        user = User.query.first()
        with caplog.at_level(logging.WARNING):
            assert user.get_queue_details() == {}
            assert user.get_queue_count() == 0
            assert user.get_queue_details() == {}
    warnings = [r for r in caplog.records if "Redis is not reachable" in r.message]
    assert len(warnings) == 1


def test_the_json_endpoints_answer_while_redis_is_down(app, user_client, redis_down):
    response = user_client.get("/api/queue-details")
    assert response.status_code == 200
    assert response.get_json() == {"count": 0, "running": [], "all": [], "files": []}

    response = user_client.get("/movie_states?movie_ids=1,2")
    assert response.status_code == 200
    assert response.get_json() == {"movies": {}, "tmdb": {}}


def test_a_page_renders_without_the_queue_badge(app, user_client, redis_down):
    response = user_client.get("/library/tv")
    assert response.status_code == 200
    assert 'id="running-tasks"' in response.get_data(as_text=True)
