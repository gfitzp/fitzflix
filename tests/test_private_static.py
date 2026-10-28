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


def test_the_private_static_subtrees_refuse_an_anonymous_request(client, user_client):
    for path in (
        "/static/custom/movie/1/poster.jpg",
        "/static/triage/12995/2/snap-5.jpg",
    ):
        assert client.get(path).status_code == 401
        assert user_client.get(path).status_code != 401

    # The public assets of the site stay public
    assert client.get("/static/sw.js").status_code != 401
