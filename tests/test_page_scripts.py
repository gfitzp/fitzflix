"""Run the shared page scripts of base.html in Node with fakes (#445).

The earlier tests of these scripts only searched the page for lines of
code. A deleted line could still pass. These tests take each function
from the rendered page and run it. A fake document, timer, fetch and
form record what the function does. The tests skip when Node is not
installed."""

import json
import shutil
import subprocess

import pytest


def page_function(body, name):
    """Return the source of the JavaScript function called name in the page body."""

    start = body.index(f"function {name}(")
    depth = 0
    for index in range(body.index("{", start), len(body)):
        if body[index] == "{":
            depth += 1
        elif body[index] == "}":
            depth -= 1
            if depth == 0:
                return body[start : index + 1]
    raise AssertionError(f"no end of function {name}")


# The fakes of the browser. The test code at the end sets "result".
# The script prints it as JSON when the pending promises are done.

PRELUDE = r"""
const record = { fetches: 0, timers: 0, appended: [], submitted: 0 };
const listeners = {};
const document = {
    hidden: false,
    addEventListener: (type, fn) => { listeners[type] = fn; },
    createElement: (tag) => ({
        tag,
        attributes: {},
        setAttribute(name, value) { this.attributes[name] = value; },
    }),
};
const POLL_TIMEOUT_MS = 20000;
const pendingTimers = {};
function setTimeout(fn, ms) { record.timers += 1; pendingTimers[record.timers] = fn; return record.timers; }
function clearTimeout(id) { delete pendingTimers[id]; }
let answer = () => Promise.resolve({ ok: true, redirected: false, json: () => Promise.resolve({}) });
function fetch(url, options) { record.fetches += 1; return answer(url, options); }
function fakeForm() {
    return {
        querySelectorAll: () => [],
        appendChild(input) { record.appended.push({ name: input.name, value: input.value, type: input.type }); },
        submit() { record.submitted += 1; },
    };
}
let result = null;
"""

EPILOGUE = r"""
setImmediate(() => setImmediate(() => {
    record.live = Object.keys(pendingTimers).length;
    console.log(JSON.stringify({ record, result }));
}));
"""


def run_page_script(body, names, test_code, tmp_path):
    """Run the named page functions and the test code in Node. Return the record."""

    if shutil.which("node") is None:
        pytest.skip("Node is not installed")
    sources = "\n".join(page_function(body, name) for name in names)
    script = tmp_path / "page_script.js"
    script.write_text(PRELUDE + sources + "\n" + test_code + EPILOGUE)
    result = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=30, check=True
    )
    return json.loads(result.stdout)


POLL_FUNCTIONS = ["fetchWithTimeout", "startPoll"]


def test_the_queue_poll_stops_after_a_401(admin_client, tmp_path):
    """A 401 stops the poll. No timer stays armed, and the tab coming back does not poll (#348).

    The one timer is the abort timer of the request. The answer clears it."""

    body = admin_client.get("/").get_data(as_text=True)
    out = run_page_script(
        body,
        POLL_FUNCTIONS,
        r"""
answer = () => Promise.resolve({ ok: false, status: 401, redirected: false });
let payloads = 0;
startPoll("/api/queue_details", 5000, () => { payloads += 1; }, {
    json: true, stopOnFailure: true, immediate: true,
});
setImmediate(() => {
    listeners.visibilitychange();
    result = { payloads };
});
""",
        tmp_path,
    )
    assert out["record"]["fetches"] == 1
    assert out["record"]["timers"] == 1
    assert out["record"]["live"] == 0
    assert out["result"]["payloads"] == 0


def test_a_good_answer_arms_the_next_poll(admin_client, tmp_path):
    """The positive control of the test above: a good answer schedules the next poll."""

    body = admin_client.get("/").get_data(as_text=True)
    out = run_page_script(
        body,
        POLL_FUNCTIONS,
        r"""
startPoll("/api/queue_details", 5000, () => {}, {
    json: true, stopOnFailure: true, immediate: true,
});
""",
        tmp_path,
    )
    assert out["record"]["fetches"] == 1
    assert out["record"]["timers"] == 2
    assert out["record"]["live"] == 1


def test_a_503_is_one_bad_tick_and_the_poll_goes_on(admin_client, tmp_path):
    """A 503 from CloudFront during a web restart arms the next poll (#650).

    Before the fix, every answer that was not OK stopped the poll for
    good, and the page showed no change until a reload."""

    body = admin_client.get("/").get_data(as_text=True)
    out = run_page_script(
        body,
        POLL_FUNCTIONS,
        r"""
answer = () => Promise.resolve({ ok: false, status: 503, redirected: false });
let payloads = 0;
startPoll("/api/queue_details", 5000, () => { payloads += 1; }, {
    json: true, stopOnFailure: true, immediate: true,
});
setImmediate(() => { result = { payloads }; });
""",
        tmp_path,
    )
    assert out["record"]["fetches"] == 1
    assert out["record"]["live"] == 1
    assert out["result"]["payloads"] == 0


def test_a_redirect_to_the_login_page_stops_the_poll(admin_client, tmp_path):
    """A redirected 200 is the login page, not page data. The poll stops (#348)."""

    body = admin_client.get("/").get_data(as_text=True)
    out = run_page_script(
        body,
        POLL_FUNCTIONS,
        r"""
answer = () => Promise.resolve({ ok: true, status: 200, redirected: true });
let payloads = 0;
startPoll("/api/queue_details", 5000, () => { payloads += 1; }, {
    json: true, stopOnFailure: true, immediate: true,
});
setImmediate(() => { result = { payloads }; });
""",
        tmp_path,
    )
    assert out["record"]["live"] == 0
    assert out["result"]["payloads"] == 0


def test_a_request_that_never_settles_is_aborted_and_the_poll_goes_on(
    admin_client, tmp_path
):
    """The abort timer ends a hung request, and the next poll is armed (#650).

    The fake fetch settles only through the abort signal. The test fires
    the one pending timer, which is the abort timer."""

    body = admin_client.get("/").get_data(as_text=True)
    out = run_page_script(
        body,
        POLL_FUNCTIONS,
        r"""
let aborted = 0;
answer = (url, options) => new Promise((resolve, reject) => {
    options.signal.addEventListener("abort", () => {
        aborted += 1;
        reject(new DOMException("aborted", "AbortError"));
    });
});
startPoll("/api/queue_details", 5000, () => {}, {
    json: true, stopOnFailure: true, immediate: true,
});
const armed = Object.keys(pendingTimers).length;
pendingTimers[1]();
setImmediate(() => { result = { aborted, armed }; });
""",
        tmp_path,
    )
    assert out["result"] == {"aborted": 1, "armed": 1}
    assert out["record"]["fetches"] == 1
    assert out["record"]["live"] == 1


def test_a_second_tap_during_a_post_does_nothing(admin_client, tmp_path):
    """The in-flight guard of backgroundPost drops the second tap (#337)."""

    body = admin_client.get("/").get_data(as_text=True)
    out = run_page_script(
        body,
        ["postForState", "backgroundPost"],
        r"""
answer = () => new Promise(() => {});
const form = fakeForm();
backgroundPost(form, "/rate", "ladder", {}, () => {}, null);
backgroundPost(form, "/rate", "ladder", {}, () => {}, null);
result = { inFlight: form._postInFlight };
""",
        tmp_path,
    )
    assert out["record"]["fetches"] == 1
    assert out["result"]["inFlight"] is True


def test_the_fallback_submission_carries_the_tapped_button(admin_client, tmp_path):
    """A failed post submits the form with the button as a hidden input (#406)."""

    body = admin_client.get("/").get_data(as_text=True)
    out = run_page_script(
        body,
        ["postForState", "backgroundPost"],
        r"""
answer = () => Promise.reject(new Error("offline"));
const form = fakeForm();
backgroundPost(form, "/rate", "ladder", {}, () => {},
    { name: "quick_rating", value: "4" });
setImmediate(() => { result = { inFlight: form._postInFlight }; });
""",
        tmp_path,
    )
    assert out["record"]["submitted"] == 1
    assert out["record"]["appended"] == [
        {"name": "quick_rating", "value": "4", "type": "hidden"}
    ]
    assert out["result"]["inFlight"] is False


def test_the_queue_tooltips_wait_for_moment(app, admin_client, tmp_path):
    """The first-paint tooltip pass runs after moment.js loads (follow-up #547).

    moment.js loads in the scripts block, after the content. A direct
    call threw a ReferenceError with a job on the page. Then the whole
    script of the queue page stopped. Here, moment is undefined when the
    script runs, and it is defined before DOMContentLoaded."""

    app.import_queue.enqueue("app.videos.localization_task", "/import/Tip (2001).mkv")
    body = admin_client.get("/queue").get_data(as_text=True)
    assert body.index("scheduleFirstPaintTitles();") < body.index("libs/moment.js/")
    test_code = r"""
document.readyState = "loading";
const abbr = {
    attributes: { title: "2026-10-28 17:00:00+00:00" },
    querySelector: () => ({ getAttribute: () => "2026-10-28T17:00:00Z" }),
    setAttribute(name, value) { this.attributes[name] = value; },
};
document.querySelectorAll = () => [abbr];
scheduleFirstPaintTitles();
const before = abbr.attributes.title;
globalThis.moment = (stamp) => ({ format: (style) => "local " + stamp + " " + style });
listeners["DOMContentLoaded"]();
result = { before, after: abbr.attributes.title };
"""
    out = run_page_script(
        body,
        ["localizeFirstPaintTitles", "scheduleFirstPaintTitles"],
        test_code,
        tmp_path,
    )
    assert out["result"] == {
        "before": "2026-10-28 17:00:00+00:00",
        "after": "local 2026-10-28T17:00:00Z LLL",
    }


def test_an_unchanged_queue_answers_304_and_a_healed_chip_repaints(app, admin_client):
    """The ETag of /queue/rows is a digest of the row data (#639).

    A new CSRF token in the forms does not change it. Thus, an idle
    queue answers 304, and the forms stay in place. The heal of a chip
    writes only its status. It changes the ETag (#574)."""

    import json as json_module

    from app.pipeline import FILE_KEY, _digest

    basename = "Etag Row (2001) - [DVD].mkv"
    with app.app_context():
        app.import_queue.enqueue("app.videos.localization_task", "/import/Head.mkv")
        app.import_queue.enqueue(
            "app.videos.localization_task", args=(f"/import/{basename}",)
        )

    page = admin_client.get("/queue").get_data(as_text=True)
    first = admin_client.get("/queue/rows")
    assert first.status_code == 200
    assert 'class="queue-front"' in first.get_data(as_text=True)
    etag = first.headers["ETag"]
    assert f"var rowsEtag = {json_module.dumps(etag)};" in page

    with admin_client.session_transaction() as session:
        session.pop("csrf_token", None)
    again = admin_client.get("/queue/rows", headers={"If-None-Match": etag})
    assert again.status_code == 304
    assert again.get_data() == b""

    key = FILE_KEY.format(digest=_digest(basename))
    trail = json_module.loads(app.redis.hget(key, "trail"))
    trail[-1]["status"] = "started"
    app.redis.hset(key, "trail", json_module.dumps(trail))
    healed = admin_client.get("/queue/rows", headers={"If-None-Match": etag})
    assert healed.status_code == 200
    assert healed.headers["ETag"] != etag


@pytest.mark.parametrize("url", ["/queue/rows", "/file-activity/cards"])
def test_a_poll_with_its_etag_in_the_query_string_gets_an_empty_answer(
    app, admin_client, url
):
    """A proxy between the browser and the server drops If-None-Match (#753).

    Then no poll got a 304, and each poll downloaded and painted the
    same rows. The poll also sends its ETag as an argument. The answer
    for a match is 204 with no body."""

    from urllib.parse import quote

    first = admin_client.get(url)
    assert first.status_code == 200
    etag = first.headers["ETag"]

    same = admin_client.get(f"{url}?etag={quote(etag)}")
    assert same.status_code == 204
    assert same.get_data() == b""
    assert same.headers["ETag"] == etag

    for old in ('"an-old-etag"', ""):
        other = admin_client.get(f"{url}?etag={quote(old)}")
        assert other.status_code == 200
        assert other.get_data() != b""


def test_the_rows_hook_swaps_only_on_a_new_answer(admin_client, tmp_path):
    """A 304 keeps the rows. A 200 swaps them and localizes the tooltips (#639, #547)."""

    body = admin_client.get("/queue").get_data(as_text=True)
    test_code = r"""
const ROWS_URL = "/queue/rows";
let rowsEtag = '"seed"';
const sent = [];
const rendered = [];
let localized = 0;
const tbody = { innerHTML: "old rows", querySelectorAll: () => ["span"] };
document.getElementById = () => tbody;
document.querySelectorAll = () => { localized += 1; return []; };
globalThis.moment = () => ({ format: () => "local" });
function flask_moment_render(span) { rendered.push(span); }
let status = 304;
let text = "new rows";
const urls = [];
answer = (url, options) => {
    urls.push(url);
    sent.push(options.headers["If-None-Match"] || null);
    return Promise.resolve({
        status,
        headers: { get: () => '"new"' },
        text: () => Promise.resolve(text),
    });
};
refreshRows().then(() => {
    const kept = tbody.innerHTML;
    status = 204;
    return refreshRows().then(() => {
        const kept204 = tbody.innerHTML;
        status = 200;
        return refreshRows().then(() => {
            const swapped = tbody.innerHTML;
            text = "the same rows again";
            return refreshRows().then(() => {
                result = { kept, kept204, swapped, again: tbody.innerHTML, sent, urls, rendered: rendered.length, localized };
            });
        });
    });
});
"""
    out = run_page_script(
        body,
        ["fetchWithTimeout", "localizeFirstPaintTitles", "refreshRows"],
        test_code,
        tmp_path,
    )

    # The last answer is a 200 with the ETag that the page has (#753).
    # The rows stay, and so do the forms in them.

    assert out["result"] == {
        "kept": "old rows",
        "kept204": "old rows",
        "swapped": "new rows",
        "again": "new rows",
        "sent": ['"seed"', '"seed"', '"seed"', '"new"'],
        "urls": [
            "/queue/rows?etag=%22seed%22",
            "/queue/rows?etag=%22seed%22",
            "/queue/rows?etag=%22seed%22",
            "/queue/rows?etag=%22new%22",
        ],
        "rendered": 1,
        "localized": 1,
    }


def test_the_cards_hook_keeps_the_cards_of_an_answer_with_its_etag(
    admin_client, tmp_path
):
    """The File Activity poll sends its ETag as an argument too (#753)."""

    body = admin_client.get("/file-activity").get_data(as_text=True)
    test_code = r"""
const window = { fileActivityCardsUrl: "/file-activity/cards" };
let cardsEtag = null;
const urls = [];
const placed = [];
document.getElementById = () => ({});
function placeCards(holder, html) { placed.push(html); }
let status = 200;
answer = (url, options) => {
    urls.push(url);
    return Promise.resolve({
        status,
        headers: { get: () => '"cards"' },
        text: () => Promise.resolve("cards " + urls.length),
    });
};
refreshCards().then(() => refreshCards()).then(() => {
    status = 204;
    return refreshCards();
}).then(() => { result = { urls, placed }; });
"""
    out = run_page_script(
        body, ["fetchWithTimeout", "refreshCards"], test_code, tmp_path
    )
    assert out["result"] == {
        "urls": [
            "/file-activity/cards",
            "/file-activity/cards?etag=%22cards%22",
            "/file-activity/cards?etag=%22cards%22",
        ],
        "placed": ["cards 1"],
    }


def _template_source(name):
    """Return the source of a template. Its script functions hold no Jinja."""

    import os

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "app", "templates", name)) as template:
        return template.read()


REPLACEMENT_FAKES = r"""
const TILE_URL = "/recommendations/tile";
const window = { matchMedia: () => ({ matches: true }), setTimeout };
const shown = ["1", "2"];
const urls = [];
function shownIds() { return shown.slice(); }
function hydrate() {}
document.createElement = () => ({
    set innerHTML(id) { this.firstElementChild = { id, classList: { add() {} } }; },
});
function tile() {
    return {
        isConnected: true,
        classList: { add() {} },
        remove() { this.isConnected = false; },
        replaceWith(fresh) { this.isConnected = false; shown.push(fresh.id); },
    };
}
const shelf = { getAttribute: () => "genre:1" };
let replacements = Promise.resolve();
// The server returns the best film that the request does not exclude.
answer = (url) => {
    urls.push(url);
    const excluded = url.split("exclude=")[1].split(",");
    const best = ["101", "102", "103"].find((id) => !excluded.includes(id));
    return Promise.resolve({ status: 200, ok: true, text: () => Promise.resolve(best) });
};
"""


def test_two_fast_replacements_do_not_bring_in_the_same_film(tmp_path):
    """The second request left before the first film was in the page (#754).

    Both requests then named the same films, and the server gave the
    same best film to both."""

    body = _template_source("base.html") + _template_source("recommendations.html")
    test_code = REPLACEMENT_FAKES + r"""
replaceCell(shelf, tile());
replaceCell(shelf, tile()).then(() => { result = { shown, urls }; });
"""
    out = run_page_script(
        body,
        ["fetchWithTimeout", "fetchReplacement", "replaceCell"],
        test_code,
        tmp_path,
    )
    assert out["result"]["shown"] == ["1", "2", "101", "102"]
    assert out["result"]["urls"][1].endswith("exclude=1,2,101")


def test_two_fast_replacements_with_the_fade_do_not_bring_in_the_same_film(tmp_path):
    """The fade of the old tile is a part of the wait (#754).

    Most people see the fade. The film is in the page only at its end."""

    body = _template_source("base.html") + _template_source("recommendations.html")
    test_code = REPLACEMENT_FAKES + r"""
window.matchMedia = () => ({ matches: false });
const fades = [];
window.setTimeout = (fn, ms) => { fades.push(ms); fn(); };
replaceCell(shelf, tile());
replaceCell(shelf, tile()).then(() => { result = { shown, urls, fades }; });
"""
    out = run_page_script(
        body,
        ["fetchWithTimeout", "fetchReplacement", "replaceCell"],
        test_code,
        tmp_path,
    )
    assert out["result"]["shown"] == ["1", "2", "101", "102"]
    assert out["result"]["urls"][1].endswith("exclude=1,2,101")
    assert out["result"]["fades"] == [250, 250]


def test_a_replacement_with_no_answer_does_not_hold_the_next_one(tmp_path):
    """The requests wait for each other. A request has a time limit (#754, #650)."""

    body = _template_source("base.html") + _template_source("recommendations.html")
    test_code = REPLACEMENT_FAKES + r"""
const served = answer;
let calls = 0;
// The first request gets no answer. Its time limit stops it.
answer = (url, options) => {
    calls += 1;
    if (calls > 1) return served(url, options);
    return new Promise((resolve, reject) => {
        if (!options.signal) return;
        options.signal.addEventListener("abort", () => reject(new Error("aborted")));
    });
};
replaceCell(shelf, tile());
replaceCell(shelf, tile()).then(() => { result = { shown, calls }; });
// The fake timers do not run by themselves. Run the time limit.
Promise.resolve().then(() => { Object.values(pendingTimers).forEach((fn) => fn()); });
"""
    out = run_page_script(
        body,
        ["fetchWithTimeout", "fetchReplacement", "replaceCell"],
        test_code,
        tmp_path,
    )
    assert out["result"] == {"shown": ["1", "2", "101"], "calls": 2}


def test_a_tile_that_left_the_page_gets_no_second_replacement(tmp_path):
    """Two actions on 1 tile ask for 1 new film (#754)."""

    body = _template_source("base.html") + _template_source("recommendations.html")
    test_code = REPLACEMENT_FAKES + r"""
const cell = tile();
replaceCell(shelf, cell);
replaceCell(shelf, cell).then(() => { result = { shown, fetches: urls.length }; });
"""
    out = run_page_script(
        body,
        ["fetchWithTimeout", "fetchReplacement", "replaceCell"],
        test_code,
        tmp_path,
    )
    assert out["result"] == {"shown": ["1", "2", "101"], "fetches": 1}


def test_a_passkey_step_names_an_answer_that_is_not_json(tmp_path):
    """The sign-in page and the error page of a proxy are HTML (#755).

    The script parsed each answer as JSON first. The person then read
    the message of the parser of the browser."""

    import os

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "app", "static", "passkeys.js")) as script:
        body = script.read()
    test_code = r"""
function csrfToken() { return "token"; }
function html(status, redirected) {
    return {
        ok: status < 400, status, redirected,
        headers: { get: () => "text/html; charset=utf-8" },
        json: () => Promise.reject(new SyntaxError("The string did not match the expected pattern.")),
    };
}
function json(status, data) {
    return {
        ok: status < 400, status, redirected: false,
        headers: { get: () => "application/json" },
        json: () => Promise.resolve(data),
    };
}
const answers = [
    () => Promise.resolve(html(200, true)),
    () => Promise.resolve(html(503, false)),
    () => Promise.reject(new TypeError("Load failed")),
    () => Promise.resolve(json(401, { error: "The passkey was not accepted." })),
    () => Promise.resolve(json(200, { redirect: "/" })),
];
const seen = [];
function next() {
    if (!answers.length) { result = seen; return null; }
    answer = answers.shift();
    return post("/auth/passkey/confirm/options", {})
        .then((data) => seen.push(data), (error) => seen.push(error.message))
        .then(next);
}
next();
"""
    out = run_page_script(body, ["post"], test_code, tmp_path)
    assert out["result"] == [
        "Your session ended. Reload the page and sign in again.",
        "The server could not do this step. Try again in a moment.",
        "The server could not do this step. Try again in a moment.",
        "The passkey was not accepted.",
        {"redirect": "/"},
    ]


def test_a_whole_passkey_error_gets_no_password_hint(tmp_path):
    """The password field does not help after an ended session (#755)."""

    import os

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "app", "static", "passkeys.js")) as script:
        body = script.read()
    test_code = r"""
function csrfToken() { return "token"; }
const shownFor = [];
function showPassword(form, show) { shownFor.push(form); return true; }
answer = () => Promise.resolve({
    ok: true, status: 200, redirected: true,
    headers: { get: () => "text/html" },
});
post("/auth/passkey/confirm/options", {}).catch((ended) => {
    const refused = new Error("The passkey was not accepted.");
    answer = () => Promise.resolve({
        ok: false, status: 500, redirected: false,
        headers: { get: () => "text/html" },
    });
    return post("/auth/passkey/confirm/options", {}).catch((fault) => {
        result = {
            ended: failedProof(ended, "form 1"),
            refused: failedProof(refused, "form 2"),
            fault: failedProof(fault, "form 3"),
            shownFor,
        };
    });
});
"""
    out = run_page_script(body, ["post", "message", "failedProof"], test_code, tmp_path)

    # A fault of the server keeps the password as a way forward.

    assert out["result"] == {
        "ended": "Your session ended. Reload the page and sign in again.",
        "refused": "The passkey was not accepted. Try again, or type your password.",
        "fault": "The server could not do this step. Try again in a moment. "
        "Or type your password.",
        "shownFor": ["form 2", "form 3"],
    }


SUGGESTION_FAKES = r"""
// The variables of the closure of the page script. hide, typed and the
// fetch function come from the page.
let wanted = null;
let timer = null;
let activeIndex = -1;
let active = -1;
const pending = {};
const input = { value: "", dataset: { kind: "series" } };
const menu = {
    children: [], shown: false,
    set innerHTML(html) { menu.children = []; },
    classList: { add() { menu.shown = true; }, remove() { menu.shown = false; } },
    appendChild(item) { menu.children.push(item.textContent); },
};
// Each request waits until the test settles it.
answer = (url) => new Promise((resolve) => {
    const q = decodeURIComponent(url.split("q=")[1]);
    pending[q] = () => resolve({
        ok: true,
        json: () => Promise.resolve({ results: [{ url: "/", title: q, detail: "film" }] }),
    });
});
// The harness prints after 2 turns of the event loop. Thus, a wait
// uses only the queue of the promises.
const tick = async () => { for (let i = 0; i < 20; i += 1) await Promise.resolve(); };
// The person types text. Then the wait of the type-ahead ends.
function type(text) {
    input.value = text;
    typed();
    Object.keys(pendingTimers).forEach((id) => { const fn = pendingTimers[id]; delete pendingTimers[id]; fn(); });
}
const state = () => ({ shown: menu.shown, items: menu.children.slice() });
"""

SUGGESTION_STEPS = r"""
(async () => {
    const seen = {};
    // The box is cleared while the answer for "st" is on its way.
    type("st");
    type("");
    pending["st"]();
    await tick();
    seen.cleared = state();

    // "st", then "sta". The answer for "sta" comes first.
    type("st");
    type("sta");
    pending["sta"]();
    await tick();
    pending["st"]();
    await tick();
    seen.overtaken = state();

    // The menu is closed while the answer for "xy" is on its way.
    type("xy");
    hide();
    pending["xy"]();
    await tick();
    seen.closed = state();

    // An answer for the text in the box opens the menu.
    type("zz");
    pending["zz"]();
    await tick();
    seen.current = state();
    result = seen;
})();
"""


def test_an_old_search_answer_does_not_paint_the_menu(admin_client, tmp_path):
    """An answer paints only for the text that the box holds (#756).

    The person typed more, cleared the box or closed the menu while the
    request was on its way. The old answer opened the menu."""

    body = admin_client.get("/").get_data(as_text=True)
    out = run_page_script(
        body,
        ["hide", "suggest", "typed"],
        SUGGESTION_FAKES + SUGGESTION_STEPS,
        tmp_path,
    )
    assert out["result"] == {
        "cleared": {"shown": False, "items": []},
        "overtaken": {"shown": True, "items": ["sta — film"]},
        "closed": {"shown": False, "items": []},
        "current": {"shown": True, "items": ["zz — film"]},
    }


def test_an_old_dvr_title_answer_does_not_paint_the_menu(tmp_path):
    """The title box of a DVR channel page has the same rule (#756)."""

    body = _template_source("dvr_channel.html")
    test_code = SUGGESTION_FAKES + r"""
function render(results) {
    menu.children = results.map((item) => item.title + " — film");
    menu.shown = results.length > 0;
}
""" + SUGGESTION_STEPS
    out = run_page_script(body, ["hide", "lookup", "typed"], test_code, tmp_path)
    assert out["result"] == {
        "cleared": {"shown": False, "items": []},
        "overtaken": {"shown": True, "items": ["sta — film"]},
        "closed": {"shown": False, "items": []},
        "current": {"shown": True, "items": ["zz — film"]},
    }


def test_escape_on_the_dvr_title_box_stops_an_answer_on_its_way(tmp_path):
    """Escape with no menu yet did nothing, and the answer then opened it (#756)."""

    body = _template_source("dvr_channel.html")
    test_code = SUGGESTION_FAKES + r"""
menu.querySelectorAll = () => [];
function render(results) { menu.shown = results.length > 0; }
(async () => {
    type("do");
    pressed({ key: "Escape", preventDefault() {} });
    pending["do"]();
    await tick();
    result = { shown: menu.shown, wanted };
})();
"""
    out = run_page_script(
        body, ["hide", "lookup", "typed", "pressed"], test_code, tmp_path
    )
    assert out["result"] == {"shown": False, "wanted": None}
