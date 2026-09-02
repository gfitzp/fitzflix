"""Test what each task that rewrites a library file in place owes the system.

Each such task must send a re-analyze request to Plex (#194). It must
also keep the file row correct for the file on disk.

The size half was a real gap. The Atmos supplement made 43 films
approximately 1 gigabyte larger each. It left each row with the size
from before the supplement.
"""

import ast
import os

# This lists each task that rewrites a library file in place, as
# (module, function). Import-time edits are not in the list by design.
# They occur in staging, before the file reaches the library. Thus, the
# track scan of the import records the size. The first scan of Plex
# analyzes the file

IN_PLACE_REWRITES = [
    ("app/atmos.py", "_atmos_supplement_unlocked"),
    ("app/tracks.py", "mkvpropedit_unlocked"),
    ("app/tracks.py", "_remux_audio_plan_unlocked"),
]


def _calls_in(path, function):
    """Return the names of each plain function that one function calls."""

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tree = ast.parse(open(os.path.join(root, path)).read())
    node = next(
        (
            child
            for child in ast.walk(tree)
            if isinstance(child, ast.FunctionDef) and child.name == function
        ),
        None,
    )
    assert node is not None, f"{path}:{function} has moved"
    return {
        call.func.id
        for call in ast.walk(node)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
    }


def test_every_in_place_rewrite_asks_plex_to_re_analyze():
    for path, function in IN_PLACE_REWRITES:
        assert "enqueue_plex_analyze" in _calls_in(path, function), (
            f"{path}:{function} rewrites the file in place but never asks "
            f"Plex to re-read it"
        )


def test_every_in_place_rewrite_records_the_new_filesize():
    for path, function in IN_PLACE_REWRITES:
        assert "record_filesize" in _calls_in(path, function), (
            f"{path}:{function} rewrites the file in place but leaves the "
            f"row holding the old size"
        )


def test_record_filesize_stores_the_size(app):
    from app.tracks import record_filesize
    from tests.factories import make_movie, make_movie_file

    with app.app_context():
        movie = make_movie("Gandhi", 1982)
        file = make_movie_file(movie, "WEBDL-1080p")

        record_filesize(file, 44933835055)

        assert file.filesize_bytes == 44933835055


def test_the_file_row_stores_one_size_and_no_derived_copies(app):
    """Make sure the MB and GB columns are gone.

    Only 1 line of 1 template read them. But 4 write sites had to keep
    them in step. That drift left 43 supplemented films 1 gigabyte
    short."""

    from app.models import File

    assert "filesize_bytes" in File.__table__.columns
    assert "filesize_megabytes" not in File.__table__.columns
    assert "filesize_gigabytes" not in File.__table__.columns


def test_the_file_page_formats_its_size_from_bytes(app, admin_client):
    """Format the size on the file page from bytes.

    The dropped columns used to feed this display. A size changes to GB
    at 1 GiB and shows 1 decimal. The derived-copy row beside it always
    used the same threshold and the same rounding. Thus, the reader sees
    no change."""

    from app import db
    from tests.factories import make_movie, make_movie_file

    with app.app_context():
        movie = make_movie("Size Display", 1994)
        big = make_movie_file(movie, "Bluray-1080p", filesize_bytes=44933835055)
        small = make_movie_file(
            movie, "DVD", feature_type_name="Trailers", filesize_bytes=9698581
        )
        db.session.commit()
        big_id, small_id = big.id, small.id

    page = admin_client.get(f"/file/{big_id}").get_data(as_text=True)
    assert "Size: 41.8 GB" in page

    page = admin_client.get(f"/file/{small_id}").get_data(as_text=True)
    assert "Size: 9.2 MB" in page


def test_size_display_crosses_to_gb_at_the_rounded_mb_threshold(app, admin_client):
    """Test the boundary case (#241).

    The dropped filesize_megabytes column rounded to 1 decimal BEFORE
    the >= 1024 test. Thus, a file just below 1 GiB with an MB figure
    that rounds to 1024.0 showed as GB. The from-bytes formatting must
    keep that. It must not show "1024.0 MB"."""

    from app import db
    from tests.factories import make_movie, make_movie_file

    with app.app_context():
        movie = make_movie("Size Boundary", 1994)
        # 1023.95001 MiB rounds to 1024.0 MB. Thus, it changes to GB
        crosses = make_movie_file(movie, "Bluray-1080p", filesize_bytes=1073689500)
        # 1023.86 MiB rounds to 1023.9 MB. It stays MB
        stays = make_movie_file(
            movie, "DVD", feature_type_name="Trailers", filesize_bytes=1073600000
        )
        db.session.commit()
        crosses_id, stays_id = crosses.id, stays.id

    page = admin_client.get(f"/file/{crosses_id}").get_data(as_text=True)
    assert "Size: 1.0 GB" in page

    page = admin_client.get(f"/file/{stays_id}").get_data(as_text=True)
    assert "Size: 1023.9 MB" in page
