"""Test that the triage aids and custom posters need a signed-in user (#468)."""

import os


def _write_aid(app, file_id, name, data=b"jpg"):
    """Write 1 triage aid into the configured directory and return its path."""

    path = os.path.join(app.config["TRIAGE_SNAPSHOT_DIR"], str(file_id), name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data)
    return path


def test_the_triage_directory_is_outside_the_static_tree(app):
    static = os.path.realpath(os.path.join(app.root_path, "static"))
    triage = os.path.realpath(app.config["TRIAGE_SNAPSHOT_DIR"])
    assert os.path.commonpath([static, triage]) != static

    from config import Config

    default = os.path.realpath(Config.TRIAGE_SNAPSHOT_DIR)
    if "TRIAGE_SNAPSHOT_DIR" not in os.environ:
        assert os.path.commonpath([static, default]) != static


def test_a_triage_snapshot_needs_a_signed_in_user(app, client, user_client):
    path = _write_aid(app, 987654, "2/snap-1.jpg")
    try:
        anonymous = client.get("/triage/987654/2/snap-1.jpg")
        assert anonymous.status_code in (302, 401)

        member = user_client.get("/triage/987654/2/snap-1.jpg")
        assert member.status_code == 200
        assert member.data == b"jpg"
        assert "private" in member.headers["Cache-Control"]
        assert "public" not in member.headers["Cache-Control"]

        assert user_client.get("/triage/987654/2/missing.jpg").status_code == 404
        assert user_client.get("/triage/987654/../../config.py").status_code == 404
    finally:
        os.remove(path)


def test_an_audio_sample_serves_through_the_route(app, user_client):
    path = _write_aid(app, 987655, "audio/lossy-1.m4a", b"m4a")
    try:
        response = user_client.get("/triage/987655/audio/lossy-1.m4a")
        assert response.status_code == 200
        assert response.data == b"m4a"
    finally:
        os.remove(path)


def test_the_private_static_subtrees_refuse_an_anonymous_request(
    app, client, user_client, monkeypatch, tmp_path
):
    """An anonymous request for a file of a private subtree gets 401 (#468).

    The folder app/static/custom is not in git. Thus, the test brings a
    static folder of its own and does not touch the posters of a
    checkout."""

    # The public assets of the site stay public
    assert client.get("/static/site.webmanifest").status_code == 200

    poster = tmp_path / "custom" / "movie" / "1" / "poster.jpg"
    poster.parent.mkdir(parents=True)
    poster.write_bytes(b"jpg")
    monkeypatch.setattr(app, "static_folder", str(tmp_path))

    path = "/static/custom/movie/1/poster.jpg"
    assert client.get(path).status_code == 401
    member = user_client.get(path)
    assert member.status_code == 200
    assert member.data == b"jpg"


def test_no_other_spelling_of_a_private_path_serves_an_anonymous_request(
    app, client, user_client, monkeypatch, tmp_path
):
    """The gate reads the file on disk, not the text of the path.

    A file system that ignores letter case serves "Custom" as "custom".
    A dot segment resolves into the private tree too."""

    poster = tmp_path / "custom" / "movie" / "1" / "poster.jpg"
    poster.parent.mkdir(parents=True)
    poster.write_bytes(b"jpg")
    (tmp_path / "public.txt").write_text("public")
    monkeypatch.setattr(app, "static_folder", str(tmp_path))

    assert user_client.get("/static/custom/movie/1/poster.jpg").status_code == 200
    for path in (
        "/static/custom/movie/1/poster.jpg",
        "/static/Custom/movie/1/poster.jpg",
        "/static/CUSTOM/Movie/1/POSTER.JPG",
        "/static/%2E/custom/movie/1/poster.jpg",
        "/static/./custom/movie/1/poster.jpg",
        "/static/x/../custom/movie/1/poster.jpg",
        "/static/custom/../custom/movie/1/poster.jpg",
    ):
        response = client.get(path)
        assert response.status_code != 200, path
        assert response.data != b"jpg", path
    assert client.get("/static/public.txt").status_code == 200


def test_a_path_that_the_file_system_refuses_is_a_plain_404(app, client):
    """A NUL byte in the path must not raise. An error would mail the admin."""

    for path in ("/static/%00", "/static/custom/%00", "/static/custom/a%00b.jpg"):
        assert client.get(path).status_code in (401, 404), path


def test_the_gate_checks_the_file_that_the_static_route_sends(
    app, client, monkeypatch, tmp_path
):
    """A link in the tree must not move the check to a different file."""

    static = tmp_path / "a-long-name-for-the-static-folder"
    poster = static / "custom" / "movie" / "1" / "poster.jpg"
    poster.parent.mkdir(parents=True)
    poster.write_bytes(b"jpg")
    (static / "a" / "b").mkdir(parents=True)
    (static / "link").symlink_to(static / "a" / "b")
    monkeypatch.setattr(app, "static_folder", str(static))

    response = client.get("/static/link/../custom/movie/1/poster.jpg")
    assert response.status_code != 200 and response.data != b"jpg"


def test_a_private_directory_behind_a_link_stays_private(
    app, client, user_client, monkeypatch, tmp_path
):
    """The posters can move to a different volume behind a link."""

    static = tmp_path / "a-long-name-for-the-static-folder"
    static.mkdir()
    elsewhere = tmp_path / "p"
    (elsewhere / "movie" / "1").mkdir(parents=True)
    (elsewhere / "movie" / "1" / "poster.jpg").write_bytes(b"jpg")
    (static / "custom").symlink_to(elsewhere)
    monkeypatch.setattr(app, "static_folder", str(static))

    assert client.get("/static/custom/movie/1/poster.jpg").status_code == 401
    assert user_client.get("/static/custom/movie/1/poster.jpg").status_code == 200


def test_a_public_static_file_needs_no_user_lookup(
    app, user_client, monkeypatch, tmp_path
):
    """The gate reads the path first. Thus, an icon costs no query for the user."""

    from sqlalchemy import event

    from app import db

    (tmp_path / "public.txt").write_text("public")
    monkeypatch.setattr(app, "static_folder", str(tmp_path))
    statements = []
    with app.app_context():
        engine = db.engine

    def count(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", count)
    try:
        assert user_client.get("/static/public.txt").status_code == 200
    finally:
        event.remove(engine, "before_cursor_execute", count)
    assert statements == []
