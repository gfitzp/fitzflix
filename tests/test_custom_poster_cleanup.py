"""Test that a merge or a delete does not orphan a custom poster tree (#463)."""

import os

from app import db
from tests.conftest import csrf_token_from
from tests.factories import make_movie, make_movie_file, make_tv_file, make_tv_series


def _tree(app, scope, record_id, name="poster.jpg"):
    """Write a small custom poster tree. Return its directory."""

    base = os.path.join(app.config["CUSTOM_ARTWORK_DIR"], scope, str(record_id))
    for size in ("original", "w185"):
        os.makedirs(os.path.join(base, size), exist_ok=True)
        with open(os.path.join(base, size, name), "wb") as f:
            f.write(b"jpeg")
    return base


def test_a_merge_gives_the_custom_poster_to_the_survivor(app, monkeypatch, tmp_path):
    from app.models import Movie
    from app.tmdb_refresh import move_movie_rows

    monkeypatch.setitem(app.config, "CUSTOM_ARTWORK_DIR", str(tmp_path))
    with app.app_context():
        old = make_movie("Old Poster Record", 2004)
        new = make_movie("New Poster Record", 2004, tmdb_id=463001)
        old.custom_poster = "poster.jpg"
        db.session.commit()
        old_id, new_id = old.id, new.id
        old_dir = _tree(app, "movie", old_id)

        move_movie_rows(old_id, new_id)
        db.session.flush()
        # The old tree stays until the commit.
        assert os.path.isdir(old_dir)
        db.session.delete(db.session.get(Movie, old_id))
        db.session.commit()

        assert db.session.get(Movie, new_id).custom_poster == "poster.jpg"
        new_dir = os.path.join(str(tmp_path), "movie", str(new_id))
        assert os.path.isfile(os.path.join(new_dir, "w185", "poster.jpg"))
        assert not os.path.exists(old_dir)


def test_a_merge_keeps_the_poster_of_the_survivor(app, monkeypatch, tmp_path):
    from app.models import Movie
    from app.tmdb_refresh import move_movie_rows

    monkeypatch.setitem(app.config, "CUSTOM_ARTWORK_DIR", str(tmp_path))
    with app.app_context():
        old = make_movie("Old Twin Poster", 2005)
        new = make_movie("New Twin Poster", 2005, tmdb_id=463002)
        old.custom_poster = "old.jpg"
        new.custom_poster = "new.jpg"
        db.session.commit()
        old_id, new_id = old.id, new.id
        old_dir = _tree(app, "movie", old_id, "old.jpg")
        new_dir = _tree(app, "movie", new_id, "new.jpg")

        move_movie_rows(old_id, new_id)
        db.session.delete(db.session.get(Movie, old_id))
        db.session.commit()

        assert db.session.get(Movie, new_id).custom_poster == "new.jpg"
        assert os.path.isfile(os.path.join(new_dir, "w185", "new.jpg"))
        assert not os.path.exists(os.path.join(new_dir, "w185", "old.jpg"))
        assert not os.path.exists(old_dir)


def test_a_rolled_back_merge_keeps_the_old_tree(app, monkeypatch, tmp_path):
    from app.tmdb_refresh import move_movie_rows

    monkeypatch.setitem(app.config, "CUSTOM_ARTWORK_DIR", str(tmp_path))
    with app.app_context():
        old = make_movie("Rollback Poster", 2006)
        new = make_movie("Rollback Survivor", 2006, tmdb_id=463003)
        old.custom_poster = "poster.jpg"
        db.session.commit()
        old_dir = _tree(app, "movie", old.id)
        move_movie_rows(old.id, new.id)
        db.session.rollback()
        db.session.commit()
        assert os.path.isdir(old_dir)


def test_a_series_delete_removes_the_file_poster_trees(
    app, admin_client, monkeypatch, tmp_path
):
    monkeypatch.setitem(app.config, "CUSTOM_ARTWORK_DIR", str(tmp_path))
    with app.app_context():
        series = make_tv_series("Poster Delete Show", tmdb_id=463004)
        file = make_tv_file(series, 1, 1, "DVD")
        file.custom_poster = "poster.jpg"
        db.session.commit()
        series_id, file_id = series.id, file.id
    tree = _tree(app, "file", file_id)

    token = csrf_token_from(admin_client.get(f"/tv/{series_id}").get_data(as_text=True))
    response = admin_client.post(
        f"/tv/{series_id}/delete",
        data={"csrf_token": token, "delete_submit": "Delete"},
    )
    assert response.status_code == 302
    assert not os.path.exists(tree)


def test_a_file_delete_removes_its_poster_tree(
    app, admin_client, monkeypatch, tmp_path
):
    monkeypatch.setitem(app.config, "CUSTOM_ARTWORK_DIR", str(tmp_path))
    with app.app_context():
        file = make_movie_file(make_movie("Poster File Delete", 2007), "DVD")
        file.custom_poster = "poster.jpg"
        db.session.commit()
        file_id = file.id
    tree = _tree(app, "file", file_id)

    token = csrf_token_from(admin_client.get(f"/file/{file_id}").get_data(as_text=True))
    response = admin_client.post(
        f"/file/{file_id}",
        data={"csrf_token": token, "delete_submit": "Delete and Purge File"},
    )
    assert response.status_code == 302
    assert not os.path.exists(tree)


def test_a_merge_rename_keeps_the_library_poster_of_the_survivor(
    app, monkeypatch, tmp_path
):
    """The poster of the merged folder must not replace the poster of the survivor (#475)."""

    import json
    import shutil
    import zlib

    from app.models import Movie
    from app.tmdb_refresh import apply_tmdb_refresh

    monkeypatch.setitem(app.config, "CUSTOM_ARTWORK_DIR", str(tmp_path))
    library = app.config["LIBRARY_DIR"]
    with app.app_context():
        survivor = make_movie("Merge Poster", 2012, tmdb_id=475001)
        survivor.custom_poster = "new.jpg"
        merged = make_movie("Merge Poster Copy", 2012)
        merged.custom_poster = "old.jpg"
        file = make_movie_file(merged, "DVD")
        file.untouched_basename = file.basename
        db.session.commit()
        survivor_id, merged_id = survivor.id, merged.id
        _tree(app, "movie", survivor_id, "new.jpg")
        _tree(app, "movie", merged_id, "old.jpg")
        old_dir = os.path.join(library, file.dirname)
        target_dir = os.path.join(library, "Movies", "Merge Poster (2012)")

    os.makedirs(old_dir, exist_ok=True)
    os.makedirs(target_dir, exist_ok=True)
    with open(os.path.join(old_dir, file.basename), "wb") as f:
        f.write(b"film")
    with open(os.path.join(old_dir, "poster.jpg"), "wb") as f:
        f.write(b"merged art")
    with open(os.path.join(target_dir, "poster.jpg"), "wb") as f:
        f.write(b"survivor art")

    payload = zlib.compress(
        json.dumps(
            {"id": 475001, "title": "Merge Poster", "release_date": "2012-01-01"}
        ).encode("utf-8")
    )
    try:
        assert (
            apply_tmdb_refresh(
                "Movies", merged_id, tmdb_id=475001, tmdb_payload=payload
            )
            is True
        )
        with app.app_context():
            assert db.session.get(Movie, merged_id) is None
            assert db.session.get(Movie, survivor_id).custom_poster == "new.jpg"

        posters = {}
        for name in os.listdir(target_dir):
            if name.endswith(".jpg"):
                with open(os.path.join(target_dir, name), "rb") as f:
                    posters[name] = f.read()
        assert b"merged art" not in posters.values()
        # The library copy is the custom original of the survivor
        assert posters == {"new.jpg": b"jpeg"}
    finally:
        shutil.rmtree(old_dir, ignore_errors=True)
        shutil.rmtree(target_dir, ignore_errors=True)
