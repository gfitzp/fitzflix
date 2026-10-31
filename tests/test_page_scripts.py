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
answer = (url, options) => {
    sent.push(options.headers["If-None-Match"] || null);
    return Promise.resolve({
        status,
        headers: { get: () => '"new"' },
        text: () => Promise.resolve("new rows"),
    });
};
refreshRows().then(() => {
    const kept = tbody.innerHTML;
    status = 200;
    return refreshRows().then(() => {
        return refreshRows().then(() => {
            result = { kept, swapped: tbody.innerHTML, sent, rendered: rendered.length, localized };
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
    assert out["result"] == {
        "kept": "old rows",
        "swapped": "new rows",
        "sent": ['"seed"', '"seed"', '"new"'],
        "rendered": 2,
        "localized": 2,
    }
