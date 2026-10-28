"""Test that the HTML part of 4 admin mails escapes its external strings (#450).

A release name such as "Fast & Furious <Extended>" comes from outside.
Unescaped, the tag vanishes from the HTML part and the ampersand breaks
the entity parse."""

import json
import zlib

from app import db
from tests.factories import make_movie, make_movie_file

NAME = "Fast & Furious <Extended>"


def _collect(monkeypatch, target):
    """Record the (subject, text, html) of each mail sent through target."""

    sent = []
    monkeypatch.setattr(target, lambda subject, text, html: sent.append(html))
    return sent


def _assert_escaped(html):
    assert "<Extended>" not in html
    assert "&lt;Extended&gt;" in html
    assert "Fast &amp; Furious" in html


def test_the_manual_import_mail_escapes_the_title_and_the_reason(app, monkeypatch):
    from app.manual_imports import email_manual_import_needed

    sent = _collect(monkeypatch, "app.email.notify_admin_text")
    monkeypatch.setitem(app.config, "MAIL_SERVER", "smtp.test")
    with app.test_request_context():
        email_manual_import_needed("Radarr", NAME, "No <match> & no file")
    _assert_escaped(sent[0])
    assert "No &lt;match&gt; &amp; no file" in sent[0]


def test_the_incomplete_download_mail_escapes_the_basename(app, monkeypatch, tmp_path):
    from app.api import arr as arr_module

    sent = _collect(monkeypatch, "app.email.notify_admin_text")
    monkeypatch.setattr(arr_module, "mark_grab_failed", lambda *args: False)
    junk = tmp_path / f"{NAME} (2001) - [WEBDL-1080p].mkv"
    junk.write_bytes(b"partial")
    with app.app_context():
        arr_module.reject_incomplete_download(
            "Radarr", {"downloadId": "abc"}, str(junk), None
        )
    _assert_escaped(sent[0])


def test_the_dangling_claims_mail_escapes_the_keys(app, monkeypatch):
    from types import SimpleNamespace

    from app import aws_storage

    sent = _collect(monkeypatch, "app.aws_storage.notify_admin_text")
    with app.app_context():
        file = make_movie_file(make_movie("Claimed Escape", 1999), "DVD")
        file.aws_key = f"untouched/{NAME}.mkv"
        db.session.commit()
        aws_storage._report_dangling_claims(SimpleNamespace(keys=set()))
    _assert_escaped(sent[0])


def test_the_rename_collision_mail_escapes_the_names(app, monkeypatch):
    from app.tmdb_refresh import apply_tmdb_refresh

    sent = _collect(monkeypatch, "app.tmdb_refresh.notify_admin_text")
    with app.app_context():
        movie = make_movie("Old Escape", 1999)
        file = make_movie_file(movie, "DVD")
        file.untouched_basename = file.basename
        file.basename = f"{NAME} (1999) - [DVD].mkv"
        other = make_movie("Claimant Escape", 1998)
        sibling = make_movie_file(other, "DVD", plex_title="New & Name (1999)")
        sibling.file_path = "Movies/New & Name (1999)/New & Name (1999) - [DVD].mkv"
        db.session.commit()
        movie_id = movie.id

    payload = zlib.compress(
        json.dumps(
            {"id": 7451, "title": "New & Name", "release_date": "1999-02-02"}
        ).encode("utf-8")
    )
    assert apply_tmdb_refresh("Movies", movie_id, tmdb_payload=payload) is True
    _assert_escaped(sent[0])
    assert "New &amp; Name (1999)" in sent[0]
