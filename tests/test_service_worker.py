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

const options = JSON.parse(process.argv[4] || "{}");
let unregistered = false;
const handlers = {};
const self = {
    addEventListener: (type, fn) => { handlers[type] = fn; },
    skipWaiting: () => {},
    clients: { claim: () => Promise.resolve() },
    registration: {
        scope: options.scope || "https://fitzflix.test/shopping-list/",
        unregister: () => { unregistered = true; return Promise.resolve(true); },
    },
};
vm.runInNewContext(source, {
    self, caches, fetch: fakeFetch, Response: FakeResponse, URL,
    location: { origin: "https://fitzflix.test" }, Promise, console, Math,
});

(async () => {
    if (options.activate) {
        for (const name of options.caches) store(name).put({ url: "https://fitzflix.test/x" }, {});
        const waits = [];
        handlers.activate({ waitUntil: (p) => waits.push(p) });
        await Promise.all(waits);
        console.log(JSON.stringify({ caches: Object.keys(stores), unregistered }));
        return;
    }
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


def run_activate(tmp_path, scope, caches):
    """Run the activate handler under a scope. Return the caches left and the unregister flag."""

    if shutil.which("node") is None:
        pytest.skip("Node is not installed")
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS)
    options = {"activate": True, "scope": scope, "caches": caches}
    result = subprocess.run(
        ["node", str(harness), SW_PATH, "[]", json.dumps(options)],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(result.stdout)


def test_worker_keeps_only_the_shell_pages(app, tmp_path):
    kept = run_worker(
        tmp_path,
        [
            {"path": "/"},
            {"path": "/shopping-list/movie"},
            {"path": "/shopping-list/tv"},
            {"path": "/search?q=star"},
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
        "https://fitzflix.test/shopping-list/movie",
        "https://fitzflix.test/shopping-list/tv",
    ]


def test_worker_skips_redirects_and_no_store_pages(app, tmp_path):
    kept = run_worker(
        tmp_path,
        [
            {
                "path": "/shopping-list/movie",
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


def test_the_scoped_worker_deletes_only_the_older_caches(app, tmp_path):
    """The activate under the shopping list scope keeps its own caches (#647)."""

    state = run_activate(
        tmp_path,
        "https://fitzflix.test/shopping-list/",
        ["fitzflix-v3", "fitzflix-pages-v3", "fitzflix-v4", "fitzflix-pages-v4"],
    )
    assert sorted(state["caches"]) == ["fitzflix-pages-v4", "fitzflix-v4"]
    assert state["unregistered"] is False


def test_the_worker_under_the_old_root_scope_removes_itself(app, tmp_path):
    """An update of the older worker, whose scope is the site, removes it (#647).

    The browser updates the script of the older registration in place.
    That worker would then control every page again. Thus, it deletes
    every cache and unregisters itself on activate."""

    state = run_activate(
        tmp_path,
        "https://fitzflix.test/",
        ["fitzflix-v3", "fitzflix-pages-v3", "fitzflix-v4"],
    )
    assert state["caches"] == []
    assert state["unregistered"] is True


def _run_with_put(tmp_path, put, requests):
    """Run the worker with a different cache put. Return the kept URLs."""

    if shutil.which("node") is None:
        pytest.skip("Node is not installed")
    original = (
        "put: (req, res) => { map.set(req.url, res); return Promise.resolve(); },"
    )
    assert original in HARNESS
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS.replace(original, put))
    result = subprocess.run(
        ["node", str(harness), SW_PATH, json.dumps(requests)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    kept = json.loads(result.stdout)
    return sorted(url for urls in kept.values() for url in urls)


def test_the_cache_writes_are_held_open_with_wait_until(app, tmp_path):
    """A slow write still lands. The event waits for it (#547).

    The harness waits only for the promises of the event. A write
    outside waitUntil had not landed when the worker could stop."""

    slow = (
        "put: (req, res) => new Promise((done) => setTimeout(() => "
        "{ map.set(req.url, res); done(); }, 30)),"
    )
    kept = _run_with_put(
        tmp_path,
        slow,
        [
            {"path": "/shopping-list/movie"},
            {"path": "/static/site.css", "mode": "no-cors"},
        ],
    )
    assert kept == [
        "https://fitzflix.test/shopping-list/movie",
        "https://fitzflix.test/static/site.css",
    ]


def test_a_refused_cache_write_is_caught(app, tmp_path):
    """A QuotaExceeded put is no unhandled rejection (#547)."""

    refused = "put: (req, res) => Promise.reject(new Error('QuotaExceededError')),"
    assert (
        _run_with_put(
            tmp_path,
            refused,
            [
                {"path": "/shopping-list/movie"},
                {"path": "/static/site.css", "mode": "no-cors"},
            ],
        )
        == []
    )
