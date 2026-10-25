"""Test tmdb_json and tmdb_json_or, the one JSON read of the TMDB API."""

import pytest
import requests


class _Answer:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            response = requests.Response()
            response.status_code = self.status_code
            raise requests.exceptions.HTTPError(
                f"{self.status_code} error", response=response
            )

    def json(self):
        return self.payload


def test_tmdb_json_adds_the_url_and_the_key(app, monkeypatch):
    from app.tmdb_apply import tmdb_json

    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return _Answer({"id": 603})

    monkeypatch.setitem(app.config, "TMDB_API_KEY", "key")
    monkeypatch.setattr("app.tmdb_apply.tmdb_get", fake_get)
    with app.app_context():
        assert tmdb_json("/movie/603", {"append_to_response": "credits"}) == {"id": 603}
    url, kwargs = calls[0]
    assert url == app.config["TMDB_API_URL"] + "/movie/603"
    assert kwargs == {
        "params": {"api_key": "key", "append_to_response": "credits"},
        "timeout": 10,
    }


def test_tmdb_json_raises_the_http_error(app, monkeypatch):
    """A caller can tell a 404 from an outage."""

    from app.tmdb_apply import tmdb_json

    monkeypatch.setattr("app.tmdb_apply.tmdb_get", lambda url, **k: _Answer({}, 404))
    with app.app_context(), pytest.raises(requests.exceptions.HTTPError) as error:
        tmdb_json("/movie/1")
    assert error.value.response.status_code == 404


def test_tmdb_json_or_logs_and_returns_the_default(app, monkeypatch, log_capture):
    from app.tmdb_apply import tmdb_json_or

    monkeypatch.setattr("app.tmdb_apply.tmdb_get", lambda url, **k: _Answer({}, 503))
    with app.app_context():
        assert tmdb_json_or("/movie/1", None, "fallback") == "fallback"
    assert any("503 error" in record.getMessage() for record in log_capture)
