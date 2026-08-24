"""What every task that rewrites a library file in place owes the rest
of the system: a re-analyze request to Plex (#194), and a file row that
still describes the file on disk.

The size half was a real gap — the Atmos supplement grew 43 films by
roughly a gigabyte each and left every row holding the pre-supplement
size.
"""

import ast
import os

# Each task that rewrites a library file in place, by (module, function).
# Import-time edits are deliberately absent: they happen in staging,
# before the file reaches the library, so the import's own track scan
# records the size and Plex's first scan analyzes it

IN_PLACE_REWRITES = [
    ("app/atmos.py", "_atmos_supplement_unlocked"),
    ("app/tracks.py", "mkvpropedit_unlocked"),
    ("app/tracks.py", "_remux_audio_plan_unlocked"),
]


def _calls_in(path, function):
    """The names of every plain function called inside one function."""

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
    """The MB and GB columns are gone: nothing read them but one line of
    one template, while four write sites had to keep them in step — the
    drift that left 43 supplemented films a gigabyte short."""

    from app.models import File

    assert "filesize_bytes" in File.__table__.columns
    assert "filesize_megabytes" not in File.__table__.columns
    assert "filesize_gigabytes" not in File.__table__.columns
