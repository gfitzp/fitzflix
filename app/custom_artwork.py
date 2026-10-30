"""The custom poster trees of the movie and file records on disk (#463).

An upload goes to CUSTOM_ARTWORK_DIR/<scope>/<record id>. The scope is
"movie" or "file". A merge or a delete of a record must not leave its
tree behind. A tree goes only after the commit that removes its record.
Thus, a failed commit keeps the poster of a record that stays.
sync_folder_poster decides the poster copy in a library folder (#633).
"""

import os
import shutil
import traceback

from flask import current_app
from sqlalchemy import event as sa_event
from sqlalchemy.orm import Session as OrmSession

from app.models import File

# The trees that this session removes after its commit.

_TREES_AFTER_COMMIT = "fitzflix_custom_trees"


def custom_artwork_dir(scope, record_id):
    """Return the directory of the custom poster tree of a movie or a file."""

    return os.path.join(current_app.config["CUSTOM_ARTWORK_DIR"], scope, str(record_id))


def is_library_poster(name):
    """Return True if a file name in a library folder is poster art that Plex reads."""

    lower = name.lower()
    return lower.startswith(("cover", "default", "movie", "poster")) and lower.endswith(
        ("jpg", "jpeg", "png", "tbn")
    )


def replace_library_poster(library_directory, original_file, poster_filename):
    """Remove the poster art from a library directory, then copy in the new art."""

    for name in os.listdir(library_directory):
        if is_library_poster(name):
            current_app.logger.info(f"Deleting {os.path.join(library_directory, name)}")
            os.remove(os.path.join(library_directory, name))

    destination_file = os.path.join(library_directory, poster_filename)
    shutil.copy(original_file, destination_file)
    current_app.logger.info(f"'{original_file}' Copied to '{destination_file}'")


def sync_folder_poster(movie, dirname, lost=None):
    """Make the poster art of 1 library folder of a movie obey the precedence rule (#633).

    Plex reads 1 poster in a folder. The winner is the first main feature
    of the folder, in id order, with a file-scoped original on disk.
    Then comes the original of the movie. With no winner, the library
    copy named lost goes. lost is the poster name of the record that lost
    its poster. A folder that is not on disk has no copy to change."""

    library_directory = os.path.join(current_app.config["LIBRARY_DIR"], dirname)
    if not os.path.isdir(library_directory):
        return
    files = (
        File.query.filter(File.movie_id == movie.id)
        .filter(File.feature_type_id.is_(None))
        .order_by(File.id)
    )
    candidates = [
        ("file", file.id, file.custom_poster)
        for file in files
        if file.dirname == dirname
    ]
    candidates.append(("movie", movie.id, movie.custom_poster))
    for scope, record_id, name in candidates:
        if not name:
            continue
        original = os.path.join(custom_artwork_dir(scope, record_id), "original", name)
        if os.path.isfile(original):
            replace_library_poster(library_directory, original, name)
            return
    if lost and os.path.isfile(os.path.join(library_directory, lost)):
        os.remove(os.path.join(library_directory, lost))
        current_app.logger.info(f"Deleted '{os.path.join(library_directory, lost)}'")


# The extensions of an episode thumb, "<video name without extension>.<ext>"
# beside the episode file (#469).

EPISODE_THUMB_EXTENSIONS = (".jpg", ".jpeg", ".png", ".tbn")


def move_episode_thumb(old_video, new_video):
    """Move the episode thumb of a TV file from its old video path to its new one.

    A rename of the episode must take its thumb. Plex finds the thumb by
    the name of the video. Return the number of thumbs that moved."""

    old_stem, new_stem = os.path.splitext(old_video)[0], os.path.splitext(new_video)[0]
    if old_stem == new_stem:
        return 0
    moved = 0
    for ext in EPISODE_THUMB_EXTENSIONS:
        source = old_stem + ext
        if not os.path.isfile(source):
            continue
        target = new_stem + ext
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            os.replace(source, target)
            moved += 1
            current_app.logger.info(f"Moved the episode thumb '{source}' to '{target}'")
        except OSError:
            current_app.logger.warning(traceback.format_exc())
    return moved


def remove_episode_thumb_of(video):
    """Delete the episode thumb beside a TV video path, if it exists."""

    stem = os.path.splitext(video)[0]
    for ext in EPISODE_THUMB_EXTENSIONS:
        path = stem + ext
        try:
            os.remove(path)
            current_app.logger.info(f"Deleted the episode thumb '{path}'")
        except FileNotFoundError:
            continue
        except OSError:
            current_app.logger.warning(traceback.format_exc())


def remove_custom_artwork(scope, record_id):
    """Remove the custom poster tree of a record now. A missing tree is not an error."""

    shutil.rmtree(custom_artwork_dir(scope, record_id), ignore_errors=True)


def remove_custom_artwork_after_commit(session, scope, record_id):
    """Remove the custom poster tree of a record after the next commit of session."""

    session.info.setdefault(_TREES_AFTER_COMMIT, set()).add(
        custom_artwork_dir(scope, record_id)
    )


def move_movie_poster(session, old_movie, new_movie):
    """Give the custom poster of a merged movie to the survivor (#463).

    If the survivor has no custom poster, the tree of the old record is
    copied to the survivor, and the survivor takes the file name. The
    tree of the old record goes after the commit in both cases."""

    if not old_movie.custom_poster:
        return
    old_dir = custom_artwork_dir("movie", old_movie.id)
    if not new_movie.custom_poster and os.path.isdir(old_dir):
        shutil.copytree(
            old_dir, custom_artwork_dir("movie", new_movie.id), dirs_exist_ok=True
        )
        new_movie.custom_poster = old_movie.custom_poster
        current_app.logger.info(
            f"{new_movie} takes the custom poster of the merged record {old_movie.id}"
        )
    remove_custom_artwork_after_commit(session, "movie", old_movie.id)


@sa_event.listens_for(OrmSession, "after_commit")
def _remove_trees(session):
    """Remove the custom poster trees that this session marked for removal."""

    for path in sorted(session.info.pop(_TREES_AFTER_COMMIT, None) or ()):
        try:
            shutil.rmtree(path, ignore_errors=True)
        except Exception:
            current_app.logger.warning(traceback.format_exc())


@sa_event.listens_for(OrmSession, "after_rollback")
def _keep_trees(session):
    """Keep the trees of a transaction that rolled back."""

    session.info.pop(_TREES_AFTER_COMMIT, None)
