"""Test the nightly digest of repeated log warnings (#265)."""

import gzip
import os

from datetime import datetime, timedelta

NOW = datetime(2026, 9, 22, 0, 15)


def entry(moment, level, message, site="plex_watchlist.py:285"):
    """Return 1 log line in the format of the application log."""

    stamp = moment.strftime("%Y-%m-%d %H:%M:%S")
    return f"{stamp},123 {level}: {message} [in /app/{site}]\n"


def plex_line(moment, tmdb_id):
    return entry(
        moment,
        "WARNING",
        f"Plex watchlist: couldn't add tmdb {tmdb_id}: 400 Client Error: Bad "
        f"Request for url: https://discover.provider.plex.tv/actions/"
        f"addToWatchlist?ratingKey=5d776d59594b2b001e70714b&X-Plex-Token=x",
    )


def write_logs(tmp_path, archive_lines, current_lines):
    """Write a gzip archive from before midnight and a current log file."""

    log_file = tmp_path / "fitzflix.log"
    archive = tmp_path / "fitzflix.log.2026-09-22.gz"
    with gzip.open(archive, "wt") as f:
        f.writelines(archive_lines)
    midnight = datetime(2026, 9, 22).timestamp()
    os.utime(archive, (midnight, midnight))
    log_file.write_text("".join(current_lines))
    return str(log_file)


def test_signature_ignores_the_variable_parts(app):
    """Test that 2 entries of 1 problem get 1 signature.

    The ids, the numbers, the quoted names, and the URL query vary. The
    source file and the message text stay."""

    from app.log_digest import signature

    first = plex_line(NOW, 471036).split(": ", 1)[1].rstrip("\n")
    second = plex_line(NOW, 471038).split(": ", 1)[1].rstrip("\n")
    key_a, text_a, _ = signature("WARNING", first, [])
    key_b, text_b, _ = signature("WARNING", second, [])
    assert key_a == key_b
    assert text_a.startswith("WARNING plex_watchlist.py: Plex watchlist: couldn't")
    assert "471036" not in text_a and "ratingKey" not in text_a

    key_c, _, _ = signature("WARNING", "'Brazil (1985)' Lock exists [in /a/b.py:9]", [])
    key_d, _, _ = signature("WARNING", "'Gandhi (1982)' Lock exists [in /a/b.py:9]", [])
    assert key_c == key_d


def test_traceback_signature_is_its_exception(app):
    from app.log_digest import signature

    extra = [
        '  File "/app/x.py", line 3, in run',
        "    raise HTTPError(message)",
        "requests.exceptions.HTTPError: 502 Server Error: Bad Gateway for url: "
        "https://query.wikidata.org/sparql?query=SELECT",
    ]
    _, text, _ = signature("WARNING", "Traceback (most recent call last):", extra)
    assert "HTTPError: N Server Error: Bad Gateway" in text
    assert "query=" not in text


def test_digest_counts_the_window_and_flags_the_repeats(app, tmp_path, monkeypatch):
    """Test the window, the threshold, the ignore regex, and the new marker.

    The window is the 24 hours before now, across the archive and the
    current log. Entries outside it do not count. A health warning is
    skipped by the default ignore regex."""

    from app.log_digest import build_digest

    before_window = NOW - timedelta(hours=25)
    archive = [plex_line(before_window, 1)] + [
        plex_line(NOW - timedelta(hours=10, minutes=i), 471036) for i in range(15)
    ]
    current = [plex_line(NOW - timedelta(minutes=i + 1), 471038) for i in range(10)] + [
        entry(NOW - timedelta(minutes=5), "WARNING", "Health: Volume low", "m.py:1"),
        entry(NOW - timedelta(minutes=5), "INFO", "Imported a film", "i.py:1"),
        entry(NOW - timedelta(minutes=4), "WARNING", "One-off problem", "o.py:1"),
        plex_line(NOW + timedelta(minutes=1), 471038),
    ]
    monkeypatch.setitem(app.config, "LOG_FILE", write_logs(tmp_path, archive, current))
    monkeypatch.setitem(app.config, "LOG_DIGEST_THRESHOLD", 20)

    digest = build_digest(app.config, now=NOW)
    assert digest["total"] == 26
    assert [item["count"] for item in digest["flagged"]] == [25]
    flagged = digest["flagged"][0]
    assert flagged["new"] is True
    assert "couldn't add tmdb" in flagged["example"]
    assert [item["count"] for item in digest["top"]] == [25, 1]

    # The next digest with the same repeat does not mark it new.
    again = build_digest(app.config, now=NOW, previous=digest)
    assert again["flagged"][0]["new"] is False


def test_system_page_shows_the_digest(app, admin_client):
    import json

    from app.log_digest import DIGEST_KEY

    item = {
        "key": "abc123",
        "signature": "WARNING plex_watchlist.py: Plex watchlist: couldn't add",
        "example": "Plex watchlist: couldn't add tmdb 471036",
        "count": 96,
        "new": True,
        "first": "2026-09-21 00:20:00",
        "last": "2026-09-22 00:10:00",
    }
    app.redis.set(
        DIGEST_KEY,
        json.dumps(
            {
                "generated_at": "2026-09-22 00:15:00",
                "since": "2026-09-21 00:15:00",
                "threshold": 20,
                "total": 105,
                "flagged": [item],
                "top": [item],
            }
        ),
    )
    page = admin_client.get("/system").get_data(as_text=True)
    assert "Repeated log warnings" in page
    assert "Plex watchlist: couldn&#39;t add tmdb 471036" in page
    assert ">New<" in page


def test_signature_keeps_apostrophes_inside_names(app):
    """Test that 1 problem with names that hold apostrophes is 1 signature.

    The old quote pattern paired the apostrophe of "Couldn't" with the
    quote around the name. Then each film got its own signature."""

    from app.log_digest import signature

    def leftover(path):
        return (
            f"'{path}' Couldn't delete leftover directory: [Errno 66] "
            f"Directory not empty: '{path}' [in /app/maintenance.py:1342]"
        )

    keys = {
        signature("WARNING", leftover(f"/Volumes/Movies/{title}"), [])[0]
        for title in ("Heat (1995)", "Schindler's List (1993)", "Alien (1979)")
    }
    assert len(keys) == 1


def test_traceback_signatures_name_the_exception_and_the_source(app):
    """Test the 2 traceback shapes of the log.

    A whole traceback in the message takes the source file of its
    suffix and uses the exception as the example. A message with a
    traceback after it adds the exception type. Thus, 2 errors on 1
    route stay apart."""

    from app.log_digest import signature

    extra = [
        '  File "/app/plex_library.py", line 3, in run',
        "requests.exceptions.ReadTimeout: HTTPSConnectionPool(host='plex.tv', "
        "port=443): Read timed out. [in /app/plex_library.py:130]",
    ]
    _, text, example = signature("WARNING", "Traceback (most recent call last):", extra)
    assert text.startswith("WARNING plex_library.py: requests.exceptions.ReadTimeout")
    assert example.startswith("requests.exceptions.ReadTimeout")

    def route_error(exception):
        return signature(
            "ERROR",
            "Exception on /movie/6310 [GET] [in /venv/flask/app.py:875]",
            ["Traceback (most recent call last):", '  File "a.py"', exception],
        )

    key_error, type_error = route_error("KeyError: 'x'"), route_error("TypeError: bad")
    assert key_error[0] != type_error[0]
    assert key_error[1].endswith("[KeyError]")
    assert key_error[2] == "Exception on /movie/6310 [GET] (KeyError: 'x')"
