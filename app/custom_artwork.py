"""The custom poster trees of the movie and file records on disk (#463).

An upload goes to CUSTOM_ARTWORK_DIR/<scope>/<record id>. The scope is
"movie" or "file". A merge or a delete of a record must not leave its
tree behind. A tree goes only after the commit that removes its record.
Thus, a failed commit keeps the poster of a record that stays.
sync_folder_poster decides the poster copy in a library folder (#633).
restore_folder_poster and reconcile_library_posters put a lost copy
back (#654).
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
    its poster. A folder that is not on disk has no copy to change. A
    file with no movie record has no folder rule."""

    library_directory = os.path.join(current_app.config["LIBRARY_DIR"], dirname)
    if movie is None or not os.path.isdir(library_directory):
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


# The seconds that a poster restore waits after a Radarr delete event.
# Radarr deletes the copy during its scan of the folder. The scan ends
# before the copy returns.

POSTER_RESTORE_DELAY = 30


def folder_has_library_poster(library_directory):
    """Return True if a library folder on disk holds poster art that Plex reads."""

    try:
        names = os.listdir(library_directory)
    except OSError:
        return False
    return any(is_library_poster(name) for name in names)


def _restore_folder(dirname):
    """Put the custom poster back into 1 main-feature folder. Return True on a copy.

    The folder rule of sync_folder_poster decides the copy. A folder
    that is not on disk, that holds poster art, or that has no main
    feature with a movie record changes nothing."""

    library_directory = os.path.join(current_app.config["LIBRARY_DIR"], dirname)
    if not os.path.isdir(library_directory):
        return False
    if folder_has_library_poster(library_directory):
        return False
    file = (
        File.query.filter(File.dirname == dirname)
        .filter(File.feature_type_id.is_(None))
        .filter(File.movie_id.isnot(None))
        .order_by(File.id)
        .first()
    )
    if file is None or file.movie is None:
        return False
    sync_folder_poster(file.movie, file.dirname)
    return folder_has_library_poster(library_directory)


def restore_folder_poster(dirname):
    """Put the custom poster back into 1 library folder (#654).

    This is a task. The Radarr webhook queues it after Radarr deletes a
    movie file. Radarr linked the poster copy of the folder to that
    file, and deleted the copy with it. dirname is the folder, relative
    to LIBRARY_DIR. Return True when a copy went in."""

    from app import get_app

    with get_app().app_context():
        restored = _restore_folder(dirname)
        if restored:
            current_app.logger.info(
                f"'{dirname}' Restored the custom poster of the folder"
            )
        return restored


def queue_folder_poster_restore(folder, reason=None):
    """Queue restore_folder_poster for a library folder. Return the job, or None.

    folder is an absolute path. A folder outside LIBRARY_DIR has no
    record, and queues nothing. The job waits POSTER_RESTORE_DELAY
    seconds. A repeat of the event adds a second job. The second job
    finds the copy in place and changes nothing."""

    from app.tasks import enqueue_task

    if not folder:
        return None
    library = os.path.realpath(current_app.config["LIBRARY_DIR"])
    path = os.path.realpath(folder)
    if path == library or os.path.commonpath([path, library]) != library:
        return None
    dirname = os.path.relpath(path, library)
    current_app.logger.info(
        f"Radarr deleted a movie file in '{dirname}' ({reason}). The custom "
        f"poster of the folder returns in {POSTER_RESTORE_DELAY} s"
    )
    return enqueue_task(
        "app.custom_artwork.restore_folder_poster",
        dirname,
        delay=POSTER_RESTORE_DELAY,
        description=f"'{dirname}' Restoring the custom poster of the folder",
    )


def _poster_folders():
    """Return the dirnames of the main-feature folders that a custom poster rules.

    A folder counts when its movie, or a main feature in it, has a
    custom poster. Each folder comes once."""

    from sqlalchemy import or_

    from app import db
    from app.models import Movie

    rows = (
        db.session.query(File.dirname)
        .join(Movie, Movie.id == File.movie_id)
        .filter(File.feature_type_id.is_(None))
        .filter(or_(Movie.custom_poster.isnot(None), File.custom_poster.isnot(None)))
        .distinct()
    )
    return sorted(dirname for (dirname,) in rows)


def reconcile_library_posters():
    """Restore the custom poster copy of each main-feature folder that lost it (#654).

    This is a task. It runs nightly, as the backstop of the Radarr
    webhook. Radarr deletes the copy of a folder when the movie file
    that it linked the copy to goes missing, after a rip upgrade. A
    folder that holds poster art changes nothing. A folder that is not
    on disk is skipped. Return a summary."""

    from app import get_app

    with get_app().app_context():
        checked = restored = 0
        for dirname in _poster_folders():
            checked += 1
            try:
                if _restore_folder(dirname):
                    restored += 1
            except OSError:
                current_app.logger.warning(traceback.format_exc())
        summary = (
            f"Custom posters: {checked} folder(s) checked, "
            f"{restored} copy(ies) restored"
        )
        current_app.logger.info(summary)
        return summary


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
