"""Tests for the cache rules of the service worker (#334).

The tests run app/static/sw.js in Node with a fake Cache Storage, a fake
fetch, and fake fetch events. They skip when Node is not installed."""

import json
import os
import shutil
import subprocess

import pytest

SW_PATH = os.path.join(os.path.dirname(__file__), "..", "app", "static", "sw.js")

# The harness loads the worker, sends each request through its fetch
# handler, waits for the cache writes, and prints the kept URLs

HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync(process.argv[2], "utf8");
const requests = JSON.parse(process.argv[3]);

const stores = {};
function store(name) {
    if (!stores[name]) {
        const map = new Map();
        stores[name] = {
            put: (req, res) => { map.set(req.url, res); return Promise.resolve(); },
            match: (req) => Promise.resolve(map.get(req.url)),
            keys: () => Promise.resolve([...map.keys()].map((url) => ({ url }))),
            delete: (key) => Promise.resolve(map.delete(key.url || key)),
            urls: () => [...map.keys()],
        };
    }
    return stores[name];
}
const caches = {
    open: (name) => Promise.resolve(store(name)),
    keys: () => Promise.resolve(Object.keys(stores)),
    delete: (name) => Promise.resolve(delete stores[name]),
    match: (req) => Promise.resolve(undefined),
};

class FakeResponse {
    constructor(body, init) {
        init = init || {};
        this.body = body;
        this.status = init.status || 200;
        this.ok = this.status >= 200 && this.status < 300;
        this.type = "basic";
        this.redirected = !!init.redirected;
        const headers = init.headers || {};
        this.headers = { get: (name) => headers[name] || null };
    }
    clone() { return this; }
    static error() { return new FakeResponse(null, { status: 0 }); }
}

let respond = null;
function fakeFetch(request) {
    const spec = respond(request);
    return Promise.resolve(new FakeResponse("body", spec));
}

const handlers = {};
const self = {
    addEventListener: (type, fn) => { handlers[type] = fn; },
    skipWaiting: () => {},
    clients: { claim: () => Promise.resolve() },
};
vm.runInNewContext(source, {
    self, caches, fetch: fakeFetch, Response: FakeResponse, URL,
    location: { origin: "https://fitzflix.test" }, Promise, console, Math,
});

(async () => {
    for (const item of requests) {
        respond = () => item.response || { headers: { "Content-Type": "text/html; charset=utf-8" } };
        const request = { url: "https://fitzflix.test" + item.path, method: item.method || "GET", mode: item.mode || "navigate" };
        const waits = [];
        handlers.fetch({
            request,
            respondWith: (p) => waits.push(p),
            waitUntil: (p) => waits.push(p),
        });
        await Promise.all(waits);
        await new Promise((r) => setTimeout(r, 0));
    }
    const out = {};
    for (const name of Object.keys(stores)) out[name] = stores[name].urls();
    console.log(JSON.stringify(out));
})();
"""


def run_worker(tmp_path, requests):
    if shutil.which("node") is None:
        pytest.skip("Node is not installed")
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS)
    result = subprocess.run(
        ["node", str(harness), SW_PATH, json.dumps(requests)],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    kept = json.loads(result.stdout)
    return sorted(url for urls in kept.values() for url in urls)


def test_worker_keeps_only_the_shell_pages(app, tmp_path):
    kept = run_worker(
        tmp_path,
        [
            {"path": "/"},
            {"path": "/shopping-list/movie"},
            {"path": "/profile"},
            {"path": "/watchlist"},
            {"path": "/history"},
            {"path": "/api/queue-details", "mode": "cors"},
            {"path": "/movie_states?movie_ids=1,2", "mode": "cors"},
            {"path": "/search.json?q=star", "mode": "cors"},
            {"path": "/game/frame/abc?stage=1", "mode": "no-cors"},
        ],
    )
    assert kept == [
        "https://fitzflix.test/",
        "https://fitzflix.test/shopping-list/movie",
    ]


def test_worker_skips_redirects_and_no_store_pages(app, tmp_path):
    kept = run_worker(
        tmp_path,
        [
            {
                "path": "/",
                "response": {
                    "redirected": True,
                    "headers": {"Content-Type": "text/html"},
                },
            },
            {
                "path": "/shopping-list/tv",
                "response": {
                    "headers": {
                        "Content-Type": "text/html",
                        "Cache-Control": "no-store",
                    }
                },
            },
            {"path": "/shopping-list/movie?sort=title"},
        ],
    )
    assert kept == []


def test_worker_caps_the_kept_search_pages(app, tmp_path):
    kept = run_worker(tmp_path, [{"path": f"/search?q=film{n}"} for n in range(15)])
    assert len(kept) == 10
    assert "https://fitzflix.test/search?q=film14" in kept
    assert "https://fitzflix.test/search?q=film0" not in kept


def test_logout_deletes_the_kept_pages(app, tmp_path):
    kept = run_worker(
        tmp_path,
        [
            {"path": "/"},
            {"path": "/shopping-list/movie"},
            {"path": "/auth/logout", "method": "POST"},
        ],
    )
    assert kept == []


def test_a_get_of_the_logout_page_keeps_the_pages(app, tmp_path):
    # A logout is a POST (#401). A prefetch GET is not a logout.
    kept = run_worker(
        tmp_path,
        [{"path": "/shopping-list/movie"}, {"path": "/auth/logout"}],
    )
    assert kept
