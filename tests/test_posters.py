"""The poster picker pages: the TMDb gallery with its language filter, and
uploads and gallery picks flowing through the shared custom-poster pipeline.
"""

import io
import json
import os
import re

import pytest
from PIL import Image

from app import db
from tests.factories import make_movie, make_movie_file


def csrf_token_from(page_html):
    match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page_html)
    assert match, "no csrf token found in page"
    return match.group(1)


def png_bytes():
    buf = io.BytesIO()
    Image.new("RGB", (300, 450), color=(120, 40, 40)).save(buf, "PNG")
    return buf.getvalue()


GALLERY = [
    {"file_path": "/english.jpg", "iso_639_1": "en", "width": 2000, "height": 3000},
    {"file_path": "/textless.jpg", "iso_639_1": None, "width": 1400, "height": 2100},
    {"file_path": "/german.jpg", "iso_639_1": "de", "width": 1000, "height": 1500},
]


@pytest.fixture
def poster_pipeline(monkeypatch):
    """Capture custom-poster pipeline calls instead of writing into
    app/static — in this repo that's the real production artwork tree."""

    import app.main.routes as routes

    calls = {"saved": [], "library": []}

    def fake_save(uploaded_data, poster_filename, custom_poster_dir):
        calls["saved"].append(
            {
                "filename": uploaded_data.filename,
                "poster": poster_filename,
                "dir": custom_poster_dir,
            }
        )
        return os.path.join(custom_poster_dir, "original", poster_filename)

    monkeypatch.setattr(routes, "save_custom_poster", fake_save)
    monkeypatch.setattr(
        routes,
        "replace_library_poster",
        lambda library_directory, original_file, poster_filename: calls[
            "library"
        ].append(library_directory),
    )
    return calls


@pytest.fixture
def tmdb_image_cdn(monkeypatch):
    """A fake TMDb image CDN serving PNG bytes for any poster path."""

    import app.main.routes as routes

    fetched = []

    class FakeResponse:
        content = png_bytes()

        def raise_for_status(self):
            pass

    def fake_get(url, **kwargs):
        fetched.append(url)
        return FakeResponse()

    monkeypatch.setattr(routes.requests, "get", fake_get)
    return fetched


def make_galleried_movie(app):
    movie = make_movie("Poster Film", 1972, tmdb_id=578)
    db.session.commit()
    app.redis.setex(
        f"fitzflix:tmdb:movie:{movie.tmdb_id}:posters", 600, json.dumps(GALLERY)
    )
    return movie


def test_picker_gallery_defaults_to_english_with_language_pills(app, admin_client):
    with app.app_context():
        movie = make_galleried_movie(app)
        movie_id = movie.id

    page = admin_client.get(f"/movie/{movie_id}/poster").get_data(as_text=True)
    assert "/w185/english.jpg" in page
    assert "/w185/german.jpg" not in page
    assert "No text" in page  # the textless pill

    page = admin_client.get(f"/movie/{movie_id}/poster?language=all").get_data(
        as_text=True
    )
    assert "/w185/english.jpg" in page
    assert "/w185/german.jpg" in page
    assert "/w185/textless.jpg" in page

    page = admin_client.get(f"/movie/{movie_id}/poster?language=none").get_data(
        as_text=True
    )
    assert "/w185/textless.jpg" in page
    assert "/w185/english.jpg" not in page


def test_picker_highlights_the_default_tmdb_poster(app, admin_client):
    with app.app_context():
        movie = make_movie(
            "Default Poster Film", 1968, tmdb_id=579, tmdb_poster_path="/english.jpg"
        )
        db.session.commit()
        app.redis.setex(
            f"fitzflix:tmdb:movie:{movie.tmdb_id}:posters", 600, json.dumps(GALLERY)
        )
        movie_id = movie.id

    page = admin_client.get(f"/movie/{movie_id}/poster?language=all").get_data(
        as_text=True
    )
    assert page.count("TMDb default") == 1
    # The badge and the thicker border sit on the default poster's card
    default_card = re.search(r'<img src="[^"]*/w185/english\.jpg"[^>]*>', page).group(0)
    assert "border-primary" in default_card
    for other in ("german", "textless"):
        card = re.search(rf'<img src="[^"]*/w185/{other}\.jpg"[^>]*>', page).group(0)
        assert "border-primary" not in card


def test_picker_without_tmdb_id_offers_upload_only(app, admin_client):
    with app.app_context():
        movie = make_movie("No TMDb Film", 1980)
        db.session.commit()
        movie_id = movie.id

    page = admin_client.get(f"/movie/{movie_id}/poster").get_data(as_text=True)
    assert "no TMDb id" in page
    assert "custom-file-input" in page  # the upload form is still there


def test_picking_a_tmdb_poster_runs_the_pipeline(
    app, admin_client, poster_pipeline, tmdb_image_cdn
):
    from app.models import Movie

    with app.app_context():
        movie = make_galleried_movie(app)
        make_movie_file(movie, "DVD")
        db.session.commit()
        movie_id = movie.id

    page = admin_client.get(f"/movie/{movie_id}/poster").get_data(as_text=True)
    response = admin_client.post(
        f"/movie/{movie_id}/poster",
        data={
            "csrf_token": csrf_token_from(page),
            "poster_path": "/english.jpg",
            "poster_select_submit": "Use this poster",
        },
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith(f"/movie/{movie_id}")

    assert tmdb_image_cdn == [f"{app.config['TMDB_IMAGE_URL']}/original/english.jpg"]
    assert poster_pipeline["saved"] == [
        {
            "filename": "english.jpg",
            "poster": "poster.jpg",
            "dir": os.path.join(
                app.config["CUSTOM_ARTWORK_DIR"], "movie", str(movie_id)
            ),
        }
    ]
    # The main-feature file's library directory got the new poster
    assert len(poster_pipeline["library"]) == 1

    with app.app_context():
        assert Movie.query.get(movie_id).custom_poster == "poster.jpg"


def test_uploading_a_poster_runs_the_pipeline(app, admin_client, poster_pipeline):
    from app.models import Movie

    with app.app_context():
        movie = make_movie("Upload Film", 1985)
        db.session.commit()
        movie_id = movie.id

    page = admin_client.get(f"/movie/{movie_id}/poster").get_data(as_text=True)
    response = admin_client.post(
        f"/movie/{movie_id}/poster",
        data={
            "csrf_token": csrf_token_from(page),
            "custom_poster": (io.BytesIO(png_bytes()), "my poster.png"),
            "poster_submit": "Upload",
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 302
    assert poster_pipeline["saved"][0]["poster"] == "poster.png"

    with app.app_context():
        assert Movie.query.get(movie_id).custom_poster == "poster.png"


def test_bad_poster_path_is_rejected_before_any_fetch(
    app, admin_client, poster_pipeline, tmdb_image_cdn
):
    with app.app_context():
        movie = make_galleried_movie(app)
        db.session.commit()
        movie_id = movie.id

    page = admin_client.get(f"/movie/{movie_id}/poster").get_data(as_text=True)
    response = admin_client.post(
        f"/movie/{movie_id}/poster",
        data={
            "csrf_token": csrf_token_from(page),
            "poster_path": "/../../etc/passwd",
            "poster_select_submit": "Use this poster",
        },
        follow_redirects=True,
    )
    assert "isn&#39;t a TMDb poster path" in response.get_data(as_text=True)
    assert tmdb_image_cdn == []
    assert poster_pipeline["saved"] == []


def test_movie_and_file_pages_link_to_the_picker(app, admin_client):
    with app.app_context():
        movie = make_movie("Linked Film", 1990)
        file = make_movie_file(movie, "DVD")
        db.session.commit()
        movie_id, file_id = movie.id, file.id

    page = admin_client.get(f"/movie/{movie_id}").get_data(as_text=True)
    assert f"/movie/{movie_id}/poster" in page
    assert "custom-file-input" not in page  # the inline form moved away

    page = admin_client.get(f"/file/{file_id}").get_data(as_text=True)
    assert f"/file/{file_id}/poster" in page
    assert "custom-file-input" not in page


def test_file_picker_refuses_when_file_is_not_local(app, admin_client, poster_pipeline):
    with app.app_context():
        movie = make_movie("Remote Film", 1995)
        file = make_movie_file(movie, "DVD")
        db.session.commit()
        file_id = file.id

    page = admin_client.get(f"/file/{file_id}/poster").get_data(as_text=True)
    assert "<fieldset disabled>" in page

    response = admin_client.post(
        f"/file/{file_id}/poster",
        data={
            "csrf_token": csrf_token_from(page),
            "custom_poster": (io.BytesIO(png_bytes()), "poster.png"),
            "poster_submit": "Upload",
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert "is not present locally" in response.get_data(as_text=True)
    assert poster_pipeline["saved"] == []


def test_file_picker_pick_sets_the_file_poster(
    app, admin_client, poster_pipeline, tmdb_image_cdn
):
    from app.models import File

    with app.app_context():
        movie = make_galleried_movie(app)
        file = make_movie_file(movie, "DVD")
        db.session.commit()
        file_id = file.id

        # The custom poster lands beside the library file, so it must exist
        local_path = os.path.join(app.config["LIBRARY_DIR"], file.file_path)
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        with open(local_path, "wb") as f:
            f.write(b"movie bytes")

    try:
        page = admin_client.get(f"/file/{file_id}/poster").get_data(as_text=True)
        assert "/w185/english.jpg" in page

        response = admin_client.post(
            f"/file/{file_id}/poster",
            data={
                "csrf_token": csrf_token_from(page),
                "poster_path": "/english.jpg",
                "poster_select_submit": "Use this poster",
            },
        )
        assert response.status_code == 302
        assert poster_pipeline["saved"][0]["dir"] == os.path.join(
            app.config["CUSTOM_ARTWORK_DIR"], "file", str(file_id)
        )

        with app.app_context():
            assert File.query.get(file_id).custom_poster == "poster.jpg"
    finally:
        os.remove(local_path)


def build_custom_tree(app, scope, record_id, poster_filename):
    """A custom-poster tree the way save_custom_poster lays it out."""

    base = os.path.join(app.config["CUSTOM_ARTWORK_DIR"], scope, str(record_id))
    os.makedirs(os.path.join(base, "original"), exist_ok=True)
    with open(os.path.join(base, "original", poster_filename), "wb") as f:
        f.write(png_bytes())
    return base


def test_removing_movie_poster_deletes_tree_and_library_copy(app, admin_client):
    from app.models import Movie

    with app.app_context():
        movie = make_movie("Removable Film", 2000, custom_poster="poster.jpg")
        file = make_movie_file(movie, "DVD")
        db.session.commit()
        movie_id = movie.id
        custom_dir = build_custom_tree(app, "movie", movie_id, "poster.jpg")
        library_dir = os.path.join(app.config["LIBRARY_DIR"], file.dirname)
        os.makedirs(library_dir, exist_ok=True)
        library_copy = os.path.join(library_dir, "poster.jpg")
        with open(library_copy, "wb") as f:
            f.write(png_bytes())

    page = admin_client.get(f"/movie/{movie_id}/poster").get_data(as_text=True)
    assert "Remove custom poster" in page

    response = admin_client.post(
        f"/movie/{movie_id}/poster",
        data={
            "csrf_token": csrf_token_from(page),
            "poster_remove_submit": "Remove custom poster",
        },
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith(f"/movie/{movie_id}/poster")

    assert not os.path.exists(custom_dir)
    assert not os.path.exists(library_copy)
    with app.app_context():
        assert db.session.get(Movie, movie_id).custom_poster is None

    page = admin_client.get(f"/movie/{movie_id}/poster").get_data(as_text=True)
    assert "Remove custom poster" not in page


def test_removing_file_poster_restores_movie_precedence(app, admin_client):
    """Removing a file's custom poster falls back to the movie's custom
    poster, whose art is restored into the library directory."""

    from app.models import File, Movie

    with app.app_context():
        movie = make_movie("Layered Film", 2005, custom_poster="poster.jpg")
        file = make_movie_file(movie, "DVD", custom_poster="poster.png")
        db.session.commit()
        movie_id, file_id = movie.id, file.id
        movie_tree = build_custom_tree(app, "movie", movie_id, "poster.jpg")
        file_tree = build_custom_tree(app, "file", file_id, "poster.png")
        library_dir = os.path.join(app.config["LIBRARY_DIR"], file.dirname)
        os.makedirs(library_dir, exist_ok=True)
        with open(os.path.join(library_dir, "poster.png"), "wb") as f:
            f.write(png_bytes())

    page = admin_client.get(f"/file/{file_id}/poster").get_data(as_text=True)
    response = admin_client.post(
        f"/file/{file_id}/poster",
        data={
            "csrf_token": csrf_token_from(page),
            "poster_remove_submit": "Remove custom poster",
        },
    )
    assert response.status_code == 302

    assert not os.path.exists(file_tree)
    assert os.path.exists(movie_tree)  # the movie's own poster is untouched
    assert not os.path.exists(os.path.join(library_dir, "poster.png"))
    assert os.path.exists(os.path.join(library_dir, "poster.jpg"))  # restored
    with app.app_context():
        assert db.session.get(File, file_id).custom_poster is None
        assert db.session.get(Movie, movie_id).custom_poster == "poster.jpg"


def test_remove_without_custom_poster_warns(app, admin_client):
    with app.app_context():
        movie = make_movie("Plain Film", 2010)
        db.session.commit()
        movie_id = movie.id

    page = admin_client.get(f"/movie/{movie_id}/poster").get_data(as_text=True)
    assert "Remove custom poster" not in page

    response = admin_client.post(
        f"/movie/{movie_id}/poster",
        data={
            "csrf_token": csrf_token_from(page),
            "poster_remove_submit": "Remove custom poster",
        },
        follow_redirects=True,
    )
    assert "has no custom poster to remove" in response.get_data(as_text=True)


def test_save_custom_poster_builds_thumbnails(app, tmp_path):
    """The real pipeline function, pointed at a scratch directory."""

    from werkzeug.datastructures import FileStorage

    from app.main.routes import save_custom_poster

    with app.app_context():
        upload = FileStorage(stream=io.BytesIO(png_bytes()), filename="poster.png")
        original = save_custom_poster(upload, "poster.png", str(tmp_path))

    assert os.path.isfile(original)
    for width in ("92", "154", "185", "342", "500", "780"):
        assert os.path.isfile(tmp_path / f"w{width}" / "poster.png"), width
