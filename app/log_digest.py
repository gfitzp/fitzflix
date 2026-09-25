"""Find the warnings that repeat in the application log (#265).

A failure sends an email. But a warning that repeats all day sends
nothing. The Plex watchlist loop of #257 wrote the same warning about
95 times a day for 6 days before anyone saw it.

A nightly task reads the last 24 hours of the log. It reduces each
WARNING, ERROR, and CRITICAL entry to a signature. The signature is the
level, the source file, and the message without its variable parts.
Quoted text, numbers, long hex ids, and URL queries become placeholders.
A traceback gets the signature of its last line, which is the
exception. The task counts the signatures. A signature at
LOG_DIGEST_THRESHOLD or more is flagged. The result goes to Redis.

The System page shows the digest. The health probe reports each flagged
signature as a problem. Thus, the existing health email announces it
once, reminds daily while it continues, and reports its recovery.
LOG_DIGEST_IGNORE is a regex of the expected noise. The default skips
the health warnings, which the health email already reports.
"""

import glob
import gzip
import hashlib
import json
import os
import re

from collections import Counter
from datetime import datetime, timedelta

from flask import current_app
from werkzeug.local import LocalProxy

from app import get_app

app = LocalProxy(get_app)

DIGEST_KEY = "fitzflix:log-digest"
WINDOW = timedelta(hours=24)

# A digest older than this is not reported by the health probe. Then
# the nightly task is broken, and old counts are not news.

DIGEST_TRUST = timedelta(hours=48)
TOP_COUNT = 10
LEVELS = ("WARNING", "ERROR", "CRITICAL")

ENTRY_RE = re.compile(
    r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ ([A-Z]+): (.*)$",
)
SITE_RE = re.compile(r" \[in (?:.*/)?([^/\]]+?):\d+\]$")
QUOTED_RE = re.compile(r"'[^']*'|\"[^\"]*\"")
URL_QUERY_RE = re.compile(r"(https?://[^\s?]+)\?\S*")
HEX_RE = re.compile(r"\b[0-9a-f]{12,}\b")
NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
SIZE_RE = re.compile(r"\bN (?:B|KB|MB|GB|TB)\b")
STAMP = "%Y-%m-%d %H:%M:%S"


def _log_paths(log_file, since):
    """Return the log file and the archives that can hold entries after since.

    An archive is older than since if its last write is older. Rotation
    writes an archive at midnight, so its mtime is its newest entry."""

    archives = [
        path
        for path in glob.glob(f"{log_file}.*.gz")
        if datetime.fromtimestamp(os.path.getmtime(path)) >= since
    ]
    paths = sorted(archives, key=os.path.getmtime)
    if os.path.isfile(log_file):
        paths.append(log_file)
    return paths


def read_entries(log_file, since, until):
    """Yield (stamp, level, message, extra lines) for each entry in the window.

    The window starts at since and stops before until. The extra lines
    are the lines that follow an entry without a timestamp of their
    own, for example a traceback."""

    start, stop = since.strftime(STAMP), until.strftime(STAMP)

    def inside(entry):
        return entry and start <= entry[0] < stop

    current = None
    for path in _log_paths(log_file, since):
        opener = gzip.open if path.endswith(".gz") else open
        with opener(path, "rt", errors="replace") as handle:
            for line in handle:
                line = line.rstrip("\n")
                match = ENTRY_RE.match(line)
                if match:
                    if inside(current):
                        yield current
                    current = (match.group(1), match.group(2), match.group(3), [])
                elif current:
                    current[3].append(line)
    if inside(current):
        yield current


def signature(level, message, extra):
    """Return (key, text) for one log entry.

    The text is the level, the source file, and the message with its
    variable parts replaced. The key is a short hash of the text."""

    site = ""
    match = SITE_RE.search(message)
    if match:
        site = match.group(1)
        message = message[: match.start()]

    # A traceback is known by its exception, the last unindented line.

    if message.startswith("Traceback"):
        tail = [line for line in extra if line.strip() and not line.startswith(" ")]
        if tail:
            message = SITE_RE.sub("", tail[-1])

    text = QUOTED_RE.sub("…", message)
    text = URL_QUERY_RE.sub(r"\1", text)
    text = HEX_RE.sub("#", text)
    text = NUMBER_RE.sub("N", text)
    text = SIZE_RE.sub("N size", text)
    text = f"{level} {site}: {text.strip()}"[:200]
    return hashlib.sha1(text.encode()).hexdigest()[:12], text


def build_digest(config, now=None, previous=None):
    """Return the digest of the last WINDOW of the log as a dict.

    The dict holds generated_at, the entry total, the flagged
    signatures, and the most common signatures. Each signature has its
    count, an example message, and the first and last times. A flagged
    signature that the previous digest did not flag is marked new."""

    now = now or datetime.now()
    since = now - WINDOW
    ignore = (
        re.compile(config["LOG_DIGEST_IGNORE"]) if config["LOG_DIGEST_IGNORE"] else None
    )
    threshold = config["LOG_DIGEST_THRESHOLD"]
    previous_keys = {item["key"] for item in (previous or {}).get("flagged", [])}

    counts = Counter()
    details = {}
    total = 0
    for stamp, level, message, extra in read_entries(config["LOG_FILE"], since, now):
        if level not in LEVELS:
            continue
        if ignore and ignore.search(message):
            continue
        total += 1
        key, text = signature(level, message, extra)
        counts[key] += 1
        if key not in details:
            details[key] = {
                "key": key,
                "signature": text,
                "example": SITE_RE.sub("", message)[:300],
                "first": stamp,
            }
        details[key]["last"] = stamp

    def item(key, count):
        return {**details[key], "count": count, "new": key not in previous_keys}

    return {
        "generated_at": now.strftime(STAMP),
        "since": since.strftime(STAMP),
        "threshold": threshold,
        "total": total,
        "flagged": [item(k, c) for k, c in counts.most_common() if c >= threshold],
        "top": [item(k, c) for k, c in counts.most_common(TOP_COUNT)],
    }


def stored_digest(redis):
    """Return the stored digest, or None."""

    payload = redis.get(DIGEST_KEY)
    return json.loads(payload) if payload else None


def trusted_flagged(redis, now=None):
    """Return the flagged signatures of a digest newer than DIGEST_TRUST."""

    digest = stored_digest(redis)
    if not digest:
        return []
    generated = datetime.strptime(digest["generated_at"], STAMP)
    if generated < (now or datetime.now()) - DIGEST_TRUST:
        return []
    return digest["flagged"]


def log_digest_task():
    """Build the digest of the last 24 hours of the log and store it.

    This is a task. It runs nightly after the log rotation."""

    with app.app_context():
        redis = current_app.redis
        digest = build_digest(current_app.config, previous=stored_digest(redis))
        redis.set(DIGEST_KEY, json.dumps(digest))
        current_app.logger.info(
            f"Log digest: {digest['total']} warning or error entries in 24 hours, "
            f"{len(digest['flagged'])} signature(s) at "
            f"{digest['threshold']} or more"
        )
        return f"{len(digest['flagged'])} flagged"
