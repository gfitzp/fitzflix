"""Test the three-state cache helper of app/cache.py and its callers.

A cached lookup is MISS (ask), JSON null (asked, nothing there) or a
value. A transient error caches nothing.
"""

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_cached_json_tells_miss_from_nothing(app):
    from app.cache import MISS, cache_json, cached_json

    key = "fitzflix:test:cache:states"
    with app.app_context():
        app.redis.delete(key)
        assert cached_json(key) is MISS
        cache_json(key, None, 60)
        assert cached_json(key) is None
        cache_json(key, {"a": 1}, 60)
        assert cached_json(key) == {"a": 1}

        # A value of an older format is not JSON. It reads as MISS.

        app.redis.set(key, "raw-token", ex=60)
        assert cached_json(key) is MISS
        app.redis.delete(key)


def test_cached_fetch_caches_nothing_after_a_transient_error(app):
    from app.cache import MISS, cached_fetch, cached_json

    key = "fitzflix:test:cache:fetch"
    calls = []

    def failing():
        calls.append("failing")
        return None, False

    def empty():
        calls.append("empty")
        return None, True

    with app.app_context():
        app.redis.delete(key)
        assert cached_fetch(key, 600, failing) is None
        assert cached_json(key) is MISS

        assert cached_fetch(key, 600, empty, miss_ttl=30) is None
        assert cached_json(key) is None
        assert 0 < app.redis.ttl(key) <= 30

        # The cached "nothing" answers. The fetch does not run again.

        assert cached_fetch(key, 600, empty) is None
        assert calls == ["failing", "empty"]
        app.redis.delete(key)


def test_enriched_movie_states(app, monkeypatch):
    """A film is a hit, gone (TMDB 404, cached), or unknown (not cached)."""

    from requests.exceptions import HTTPError

    from app import streaming_rail

    class Response:
        def __init__(self, status):
            self.status_code = status

        def raise_for_status(self):
            if self.status_code >= 400:
                error = HTTPError()
                error.response = self
                raise error

        def json(self):
            return {"id": 991, "title": "Hit", "release_date": "2001-01-01"}

    statuses = {991: 200, 992: 404, 993: 500}
    monkeypatch.setitem(app.config, "TMDB_API_KEY", "key")
    monkeypatch.setattr(
        streaming_rail,
        "tmdb_get",
        lambda url, **kwargs: Response(statuses[int(url.rsplit("/", 1)[1])]),
    )
    keys = [streaming_rail.ENRICHED_KEY.format(tmdb_id=tmdb_id) for tmdb_id in statuses]
    with app.app_context():
        app.redis.delete(*keys)
        try:
            assert streaming_rail.movie_gone(992) is False
            state, payload = streaming_rail.enriched_movie_state(991)
            assert state == "hit" and payload["title"] == "Hit"
            assert streaming_rail.enriched_movie_state(992) == ("gone", None)
            assert streaming_rail.movie_gone(992) is True
            assert streaming_rail.enriched_movie_state(993) == ("unknown", None)
            assert app.redis.get(keys[2]) is None
            assert streaming_rail.enriched_movie(992) is None
        finally:
            app.redis.delete(*keys)


def test_a_legacy_plex_tmdb_entry_is_read_again(app, monkeypatch):
    """The old cache wrote "" for no guid. It now reads as MISS and refetches."""

    from app import diary

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"MediaContainer": {"Metadata": [{"Guid": [{"id": "tmdb://77"}]}]}}

    monkeypatch.setitem(app.config, "PLEX_URL", "http://plex.test")
    monkeypatch.setattr(diary.requests, "get", lambda *a, **k: Response())
    key = diary.PLEX_TMDB_KEY.format(rating_key="555")
    with app.app_context():
        app.redis.set(key, "", ex=60)
        try:
            assert diary._plex_tmdb_id({"ratingKey": "555"}, {}) == 77
            assert app.redis.get(key) == b"77"
        finally:
            app.redis.delete(key)


def test_request_warm_queues_one_warm_per_scope(app):
    from app.streaming import WARM_KEY, request_warm

    key = WARM_KEY.format(scope="test-scope")
    with app.app_context():
        app.redis.delete(key)
        try:
            assert request_warm("test-scope", []) is False
            assert request_warm("test-scope", [11, 12]) is True
            assert request_warm("test-scope", [13]) is False
            jobs = [
                job
                for job in app.maintenance_queue.jobs
                if job.func_name == "app.streaming.warm_title_availability"
            ]
            assert [job.args for job in jobs] == [([11, 12],)]
        finally:
            app.redis.delete(key)
            for job in jobs:
                job.delete()


def test_no_module_builds_a_redis_key_inline():
    """Each fitzflix: key is a module constant, not an inline f-string.

    An inline key at 2 sites can drift apart. One constant cannot."""

    inline = []
    for path in sorted((ROOT / "app").rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.JoinedStr)
                and node.values
                and isinstance(node.values[0], ast.Constant)
                and str(node.values[0].value).startswith("fitzflix:")
            ):
                inline.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not inline, inline
