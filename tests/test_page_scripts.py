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
function setTimeout(fn, ms) { record.timers += 1; return record.timers; }
function clearTimeout(id) {}
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


def test_the_queue_poll_stops_after_a_401(admin_client, tmp_path):
    """A 401 stops the poll. No timer is armed, and the tab coming back does not poll (#348)."""

    body = admin_client.get("/").get_data(as_text=True)
    out = run_page_script(
        body,
        ["startPoll"],
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
    assert out["record"]["timers"] == 0
    assert out["result"]["payloads"] == 0


def test_a_good_answer_arms_the_next_poll(admin_client, tmp_path):
    """The positive control of the test above: a good answer schedules the next poll."""

    body = admin_client.get("/").get_data(as_text=True)
    out = run_page_script(
        body,
        ["startPoll"],
        r"""
startPoll("/api/queue_details", 5000, () => {}, {
    json: true, stopOnFailure: true, immediate: true,
});
""",
        tmp_path,
    )
    assert out["record"]["fetches"] == 1
    assert out["record"]["timers"] == 1


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
