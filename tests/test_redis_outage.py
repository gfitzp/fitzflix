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
            assert user.get_queue_details() == {}
    warnings = [r for r in caplog.records if "Redis is not reachable" in r.message]
    assert len(warnings) == 1


def test_the_json_endpoints_answer_while_redis_is_down(app, user_client, redis_down):
    response = user_client.get("/api/queue-details")
    assert response.status_code == 200
    assert response.get_json() == {"count": 0, "running": [], "all": []}

    response = user_client.get("/movie_states?movie_ids=1,2")
    assert response.status_code == 200
    assert response.get_json() == {"movies": {}, "tmdb": {}}


def test_a_page_renders_without_the_queue_badge(app, user_client, redis_down):
    response = user_client.get("/library/tv")
    assert response.status_code == 200
    assert 'id="running-tasks"' in response.get_data(as_text=True)


def test_the_queue_page_renders_without_rows(app, user_client, redis_down):
    """The Queue page shows an empty table while Redis is down (#476)."""

    response = user_client.get("/queue")
    assert response.status_code == 200


def test_the_rows_poll_logs_no_traceback_while_redis_is_down(
    app, user_client, redis_down, caplog
):
    """The rows fetch of an open Queue tab reads no trails in an outage.

    The details are empty then. A trail read would log a traceback at
    each 5-second poll (follow-up #639)."""

    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            assert user_client.get("/queue/rows").status_code == 200
    assert "Traceback" not in caplog.text


class DownRedis:
    """A Redis client whose each command raises ConnectionError (#699)."""

    def __getattr__(self, name):
        def down(*args, **kwargs):
            raise redis.exceptions.ConnectionError("Redis is down (test)")

        return down


@pytest.fixture
def redis_raises(app, monkeypatch):
    """Replace app.redis and the connection of every queue with DownRedis."""

    from app import redis_guard
    from app.queues import roster_queues

    down = DownRedis()
    monkeypatch.setattr(app, "redis", down)
    for queue in roster_queues(app):
        monkeypatch.setattr(queue, "connection", down)
    monkeypatch.setitem(redis_guard._last_warning, "at", None)
    return down


def _member_with_a_watchlist(app):
    """Give the member a watchlist, liked anchors, shelf picks and 2 services.

    The anchors share a genre and a keyword with 5 owned picks. Thus,
    the Recommendations page has shelves to build. The services include
    the Criterion Channel. Thus, the Criterion shelves and the card run."""

    from app import db
    from app.leaving_criterion import CRITERION_PROVIDER_ID
    from app.models import TMDBKeyword, User, UserStreamingProvider, UserWatchlist
    from tests.conftest import MEMBER_EMAIL
    from tests.factories import make_movie, make_movie_file
    from tests.test_elicitation import make_candidate
    from tests.test_recommendations import genre, log_watch

    with app.app_context():
        user = User.query.filter_by(email=MEMBER_EMAIL).one()
        western = genre(37, "Western")
        heist = TMDBKeyword(id=9717, name="heist")
        db.session.add(heist)
        films = [
            make_candidate(f"Outage Anchor {n}", 1961 + n, genre_row=western)
            for n in range(2)
        ] + [
            make_candidate(f"Outage Pick {n}", 1975 + n, genre_row=western)
            for n in range(5)
        ]
        for film in films:
            film.keywords.append(heist)
        log_watch(user.id, films[0], rating=5, liked=True)
        log_watch(user.id, films[1], rating=4, liked=True)
        wanted = make_movie("Outage Wanted", 1994, tmdb_id=7101)
        make_movie_file(wanted, "Bluray-1080p")
        streaming = make_movie("Outage Streaming", 1995, tmdb_id=7102)
        for film in (wanted, streaming):
            db.session.add(UserWatchlist(user_id=user.id, movie_id=film.id))
        for provider_id, name in ((8, "Netflix"), (CRITERION_PROVIDER_ID, "Criterion")):
            db.session.add(
                UserStreamingProvider(
                    user_id=user.id,
                    provider_id=provider_id,
                    name=name,
                    logo_path=f"/{provider_id}.jpg",
                )
            )
        db.session.commit()


@pytest.mark.parametrize("watchlist", [True, False])
@pytest.mark.parametrize("path", ["/", "/recommendations"])
def test_the_discovery_pages_answer_while_redis_is_down(
    app, user_client, redis_raises, caplog, monkeypatch, path, watchlist
):
    """The landing page and the Recommendations page answer 200 (#699).

    A Redis fault is "nothing stored" for that render. Nothing is
    queued."""

    queued = []
    monkeypatch.setattr(
        "app.main.discover.enqueue_task", lambda *args, **kwargs: queued.append(args)
    )
    if watchlist:
        _member_with_a_watchlist(app)
    with caplog.at_level(logging.WARNING):
        response = user_client.get(path)
    assert response.status_code == 200
    assert queued == []
    if watchlist and path == "/":
        assert "Outage Wanted" in response.get_data(as_text=True)
    warnings = [r for r in caplog.records if "Redis is not reachable" in r.message]
    assert len(warnings) == 1


def test_a_task_does_not_read_a_redis_fault_as_nothing_stored(app, redis_down):
    """Outside a web request, a guarded reader raises the fault of Redis (#699).

    A page can show nothing for 1 render. A task goes on and writes what
    it read. The DVR build would delete the lineup of a channel. The
    catalog check would decide a film for good. Thus, a task fails as
    it did before the guard."""

    from app.recommendations import stored_profile
    from app.streaming import batch_title_availability

    with app.app_context():
        with pytest.raises(redis.exceptions.ConnectionError):
            stored_profile(app.redis, 1)
        with pytest.raises(redis.exceptions.ConnectionError):
            batch_title_availability([603])
        with app.test_request_context("/"):
            assert stored_profile(app.redis, 1) is None
