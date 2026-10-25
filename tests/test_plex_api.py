"""Test app/plex_api.py, the client of the local Plex server."""


class _Answer:
    def __init__(self, payload=None, content=b""):
        self.payload = payload
        self.content = content

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


def test_each_call_sends_the_token_as_a_parameter(app, monkeypatch):
    from app import plex_api

    calls = []

    def fake_get(url, **kwargs):
        calls.append(("GET", url, kwargs))
        return _Answer({"MediaContainer": {}})

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return _Answer({"ok": 1}, content=b"{}") if method == "POST" else _Answer()

    monkeypatch.setitem(app.config, "PLEX_URL", "http://plex.test:32400")
    monkeypatch.setitem(app.config, "PLEX_TOKEN", "plex-token")
    monkeypatch.setattr(plex_api.requests, "get", fake_get)
    monkeypatch.setattr(plex_api.requests, "request", fake_request)
    with app.app_context():
        assert plex_api.plex_get("/library/sections", {"type": 1}) == {
            "MediaContainer": {}
        }
        assert plex_api.plex_write("POST", "/library/collections") == {"ok": 1}
        assert plex_api.plex_write("DELETE", "/library/collections/5") == {}
        assert plex_api.plex_command("PUT", "/library/sections/1/emptyTrash") is None

    assert [(method, url) for method, url, _ in calls] == [
        ("GET", "http://plex.test:32400/library/sections"),
        ("POST", "http://plex.test:32400/library/collections"),
        ("DELETE", "http://plex.test:32400/library/collections/5"),
        ("PUT", "http://plex.test:32400/library/sections/1/emptyTrash"),
    ]
    assert calls[0][2]["params"] == {"type": 1, "X-Plex-Token": "plex-token"}
    assert calls[0][2]["headers"] == {"Accept": "application/json"}
    assert calls[3][2]["params"] == {"X-Plex-Token": "plex-token"}
    assert "headers" not in calls[3][2]
