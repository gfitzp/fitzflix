"""Rename a TV series on disk and in the database.

This is the fix for Plex disambiguation (Batman becomes Batman (1966)).

Fitzflix keeps its own TV naming. A series folder carries a year only
when Glenn added one. A person renames a series in Fitzflix, with the
Rename Series form of the series page or with flask tv rename. The
rename moves the folder and the files, changes the records, and gives
Sonarr the new folder. Nothing adds a year to a series on its own.

Fitzflix does not follow a rename that a person makes in Sonarr. The
nightly Sonarr path check (reconcile_sonarr_paths) compares the 2
folders of each series. It points Sonarr at the Fitzflix folder when
the Sonarr folder is not there. Each other difference is a conflict.
The check then changes nothing, and the admin gets a mail.

A rename rewrites the untouched name of each file. It makes no request
to S3. Each archive keeps its key, and the record keeps that key.
flask aws converge-keys moves the keys that lag their names. Glenn
runs it 2 times a year.

The task is resumable. Each file commits after its own disk move. A
file that is already at its target counts as moved. A row that already
has its new folder and name counts as done. Thus, a retry after
a stop in the loop completes the series. A record whose
local file was deleted intentionally (archived-only physical media)
gets new rows with no disk operation. The weekly sync does not start
during a rename. It checks 1 time, at its start, that every queue is
idle, and this task occupies one queue. A rename that starts later can
overlap the file pass of the sync.
"""

import os
import shlex
import traceback

from datetime import datetime, timezone

from flask import current_app
from pathvalidate import sanitize_filename
from werkzeug.local import LocalProxy

from app.media.custom_artwork import move_episode_thumb
from app.tasks import enqueue_task
from app.pipeline import migrate_renamed_file
from app.plex.plex_rename import remember_rename
from app import db, get_app, makedirs_on_mounted_volume
from app.models import TVSeries
from app.media import videos
from app.arr.arr_client import SONARR
from app.arr.arr_client import rescan_command
from app.arr import sonarr_push
from app.media.importing import chain
from app.models import TV_SHOWS
from app.models import title_lock_key
from app.tasks import requeue
from app.metadata.tmdb_refresh import MAX_RENAME_VOLUME_WAITS
from app import email
from app.media.importing.filenames import reconstruct_filename
from app.operations.maintenance import clear_leftover_directory
from app.models import File
from app.tasks import TASKS
from app.tasks import live_jobs

app = LocalProxy(get_app)


def _update_sonarr_path(series, old_folder, new_folder):
    """Point the Sonarr series at the renamed folder, then request a rescan.

    Sonarr matches the series by TheTVDB id, its native key. Thus, the
    Sonarr imports and upgrades continue to arrive in the correct folder.
    The PUT does not move files. This task moved them already. The
    RescanSeries command makes Sonarr see the renamed files at once.
    The call goes through the arr client (app.arr.arr_client)."""

    if not (sonarr_push.sonarr_configured() and series.tvdb_id):
        return False

    listing = (
        sonarr_push._sonarr("GET", f"/api/v3/series?tvdbId={int(series.tvdb_id)}") or []
    )
    entry = next(
        (item for item in listing if item.get("tvdbId") == series.tvdb_id), None
    )
    if entry is None:
        current_app.logger.info(
            f"Series rename: Sonarr doesn't manage tvdb {series.tvdb_id}, skipping"
        )
        return False

    old_path = entry.get("path") or ""
    # A path with a slash at the end gave the old folder itself as its
    # dirname. normpath removes the slash first.
    new_path = os.path.join(os.path.dirname(os.path.normpath(old_path)), new_folder)
    entry["path"] = new_path
    sonarr_push._sonarr("PUT", f"/api/v3/series/{entry['id']}", entry)
    current_app.logger.info(f"Series rename: Sonarr path {old_path!r} -> {new_path!r}")
    sonarr_push._sonarr("POST", "/api/v3/command", rescan_command(SONARR, entry["id"]))
    return True


def _folder_title(title):
    """Return the folder name of a title: no spaces or periods at the end.

    evaluate_filename and Sonarr remove them the same way."""

    while title.endswith(" ") or title.endswith("."):
        title = title.strip(" ").strip(".")
    return title


def _filesystem_safe(title):
    """Return True if a title is a folder name as the rename task writes it.

    A title with characters that a filesystem refuses is not safe. The
    rename task refuses it. Accented letters are safe. The TV import
    keeps them in the show folder and the episode names, for example
    "Les Misérables (2018)". The test reads the folder that the
    task writes (_folder_title), with no period at the end. Thus,
    "The Venture Bros." is safe. A folder that starts with a period is
    hidden, and it is not safe."""

    folder = _folder_title(title)
    return (
        bool(folder)
        and sanitize_filename(folder) == folder
        and not folder.startswith(".")
    )


def _renamed_folder(part, old_folder, new_folder):
    """Return the new name of a series folder, or None if part is not it.

    The folder is the title, or the title and an id tag such as
    "{tmdb-1234}". The tag stays."""

    if part == old_folder:
        return new_folder
    tail = part[len(old_folder) :]
    if part.startswith(old_folder) and tail.startswith(" {") and tail.endswith("}"):
        return new_folder + tail
    return None


def _planned_target(file, old_title, new_title):
    """Return the show folder, dirname and basename of a file after a rename.

    Return None if the file does not carry the series title in its show
    folder and in its name. The rename task changes nothing then.
    A row that already carries the new title in both places is done. An
    earlier run of the task stopped after it. Its target is its
    own dirname and basename."""

    old_folder = _folder_title(old_title)
    new_folder = _folder_title(new_title)
    old_prefix = f"{old_title} - "
    parts = file.dirname.split("/")
    renamed = [_renamed_folder(part, old_folder, new_folder) for part in parts]
    if not any(renamed) or not file.basename.startswith(old_prefix):
        done = [part for part in parts if _renamed_folder(part, new_folder, new_folder)]
        if done and file.basename.startswith(f"{new_title} - "):
            return done[0], file.dirname, file.basename
        return None
    folder = next(name for name in renamed if name)
    new_dirname = "/".join(name or part for name, part in zip(renamed, parts))
    return folder, new_dirname, f"{new_title} - {file.basename[len(old_prefix):]}"


def rename_refusal(series, new_title):
    """Return the reason that the rename task refuses a new title, or None.

    The task and the `flask tv rename` command use the same checks.
    Thus, the command can refuse a title before it queues the task."""

    if new_title == series.title:
        return "the title does not change"
    if not _filesystem_safe(new_title):
        return f"{new_title!r} is not safe as a folder name"

    # The title column compares without case and accents (MySQL
    # general_ci). Thus, the check must not match the series itself.

    if (
        TVSeries.query.filter(
            TVSeries.title == new_title, TVSeries.id != series.id
        ).first()
        is not None
    ):
        return f"a series titled {new_title!r} already exists"
    return None


def rename_tv_series_task(series_id, new_title, volume_waits=0, stray_waits=0):
    """Rename the folder, files, and database rows of a TV series.

    This is a task. After the rename, it updates the Sonarr path and
    queues a Plex rescan. volume_waits counts the retries for the TV
    library volume. It stops at MAX_RENAME_VOLUME_WAITS.
    stray_waits counts the retries of a resumed rename that finds a media
    file with no record. It stops at
    MAX_RENAME_STRAY_WAITS. Each retry carries both counts.

    The rename makes no request to S3. An archive keeps its key.
    flask aws converge-keys moves the keys that lag their names."""

    with app.app_context():
        series = db.session.get(TVSeries, series_id)
        if series is None:
            current_app.logger.warning(f"Series rename: no series {series_id}")
            return False

        refusal = rename_refusal(series, new_title)
        if refusal:
            current_app.logger.warning(f"Series rename: {refusal}")
            return False

        waits = {"volume_waits": volume_waits, "stray_waits": stray_waits}
        locks = _take_series_locks(series, new_title, waits)
        if locks is None:
            return False
        try:
            return _move_series(series, new_title, waits)
        finally:
            for lock in locks:
                current_app.lock_manager.unlock(lock)


# A rename waits this many minutes when an import of the series is not
# done. It also waits when another task holds an episode lock.

RENAME_WAIT_MINUTES = (5, 15)

RENAME_TASK = "app.media.series_rename.rename_tv_series_task"

# The time limit of a rename grows with the number of files. A
# move on NFS can take 25 seconds with its retries. The episode locks
# have the same time limit as the job.

RENAME_SECONDS_PER_FILE = 30

# A resumed rename that finds a media file with no record waits this
# many times for the import of the file. With the waits
# of RENAME_WAIT_MINUTES, that is about 2 hours. Then the rename stops,
# and the admin gets a mail.

MAX_RENAME_STRAY_WAITS = 12


def rename_timeout(series):
    """Return the time limit of the rename of a series, in seconds.

    It is SQL_TASK_TIMEOUT, or RENAME_SECONDS_PER_FILE for each file if
    that is longer."""

    floor = current_app.config["SQL_TASK_TIMEOUT"]
    return max(floor, RENAME_SECONDS_PER_FILE * series.files.count())


def series_with_a_rename():
    """Return the ids of the series whose rename waits or runs."""

    return {
        job.args[0]
        for job in live_jobs([TASKS[RENAME_TASK].queue])
        if job.func_name == RENAME_TASK and job.args
    }


def rename_pending(series_id):
    """Return True if a rename of the series waits or runs."""

    return series_id in series_with_a_rename()


def _take_series_locks(series, new_title, waits=None):
    """Take the title lock of each episode of a series. Return the locks.

    Return None after the rename goes back on its queue. An import takes
    the lock of its episode. A new episode has no lock yet, so the
    rename also waits for each import of the series that is not done.
    Else the import attaches by the old title at its end and makes a
    second series. waits holds the retry counts of the task."""

    reason = None
    source = chain.series_import_in_flight({series.title, new_title})
    if source:
        reason = f"waits for the import of '{os.path.basename(source)}'"
    locks = []
    episodes = {(file.season, file.episode) for file in series.files}
    ttl_ms = _lock_seconds(series) * 1000
    for season, episode in [] if reason else sorted(episodes, key=str):
        key = title_lock_key(TV_SHOWS, series.title, season=season, episode=episode)
        lock = current_app.lock_manager.lock(key, ttl_ms)
        if not lock:
            reason = f"waits for the title lock of {key}"
            break
        locks.append(lock)
    if reason is None:
        return locks
    for lock in locks:
        current_app.lock_manager.unlock(lock)
    _requeue_rename(series, new_title, reason, waits)
    return None


def _lock_seconds(series):
    """Return the TTL of the episode locks of a rename, in seconds.

        Under rq, it is the timeout of the running job. The job got that
        timeout from the file count when it was queued. A file that left
        the series since then must not make the locks shorter than the job.
    Outside rq, it is rename_timeout(series)."""

    from rq import get_current_job

    job = get_current_job()
    if job is not None and job.timeout:
        return max(int(job.timeout), rename_timeout(series))
    return rename_timeout(series)


def _requeue_rename(series, new_title, reason, waits=None, counted=None):
    """Put the rename of a series back on its queue after RENAME_WAIT_MINUTES.

    Return False when the rename stops at its limit, else True. waits
    holds the retry counts of the task. counted names the count that
    this wait adds 1 to. A wait for the volume counts, and so
    does a wait for the record of a media file. At the
    limit of its count, the rename stops with an ERROR, as the movie
    rename does. Each retry carries every count that is not 0."""

    waits = dict(waits or {})
    limits = {
        "volume_waits": MAX_RENAME_VOLUME_WAITS,
        "stray_waits": MAX_RENAME_STRAY_WAITS,
    }
    if counted:
        done = waits.get(counted, 0)
        if done >= limits[counted]:
            current_app.logger.error(
                f"Series rename of {series.title!r} {reason} after "
                f"{done} waits. It stops. Rename the series again later"
            )
            return False
        waits[counted] = done + 1

    # While the rename waits, a record can name a path with no file.
    # The marker holds the restores of the weekly sync for the series
    # in that time, as it does after a refusal.

    _mark_conflict(series)
    counters = {name: count for name, count in waits.items() if count}
    requeue(
        RENAME_TASK,
        key=series.id,
        delay=RENAME_WAIT_MINUTES,
        reason=f"Series rename of {series.title!r} {reason}",
        counters=counters or None,
        args=(series.id, new_title),
        timeout=rename_timeout(series),
    )
    return True


def _refuse_duplicate_target(series, src_relative, dst_relative):
    """Stop a series rename whose target file exists already, and tell the admin.

    A retry cannot clear this. Only the admin can, by deleting 1 copy.
    The movie rename refuses the same shape in the same way."""

    from markupsafe import escape

    detail = f"'{dst_relative}' exists, and so does '{src_relative}'"
    current_app.logger.warning(f"Series rename: {detail}. Nothing replaced")
    email.notify_admin_text(
        "Rename collision needs triage",
        f"Renaming the series '{series.title}' was refused: {detail}.\n\n"
        f"Delete one of the copies, then rename the series again.",
        f"<p>Renaming the series '{escape(series.title)}' was refused: "
        f"{escape(detail)}.</p><p>Delete one of the copies, then rename "
        f"the series again.</p>",
    )


def _tell_admin_of_stray_files(series, new_title, stray, resumed=False):
    """Mail the admin about the media files that stop a series rename.

    stray holds the paths, relative to LIBRARY_DIR, of the media files
    that belong to no record. A refusal mails 1 time for each life of
    SONARR_STRAY_MAIL_KEY (_first_refusal). A resumed rename
    (resumed=True) mails when its waits are used up. That occurs 1 time
    for each rename. The function never raises."""

    from markupsafe import escape

    try:
        if not resumed and not _first_refusal(
            SONARR_STRAY_MAIL_KEY.format(series_id=series.id)
        ):
            return
        names = sorted(stray)[:5]
        intro = (
            f"The series '{series.title}' is only partly renamed to "
            f"'{new_title}'. Some or all records have the new folder, but the "
            f"title did not change."
            if resumed
            else f"The series '{series.title}' was not renamed to '{new_title}'."
        )
        found = (
            f"{len(stray)} video file(s) in the new folder belong to no record "
            f"of the series."
        )
        action = (
            "Import each file or remove it. You can also give a file the name "
            "of its record again."
        )
        then = (
            f"Then rename the series to '{new_title}' again, with the form of "
            f"its page or with flask tv rename {series.id} {shlex.quote(new_title)}."
        )
        listing = "\n".join(f"- {name}" for name in names)
        email.notify_admin_text(
            "Series rename needs triage",
            f"{intro} {found}\n\n{listing}\n\n{action} {then}",
            f"<p>{escape(intro)} {escape(found)}</p><ul>"
            + "".join(f"<li>{escape(name)}</li>" for name in names)
            + f"</ul><p>{escape(action)} {escape(then)}</p>",
        )
    except Exception:
        current_app.logger.error(traceback.format_exc())


def _rows_not_renamed(series, old_title, new_title):
    """Return the paths of the files of a series that do not have the new title yet."""

    left = []
    for file in series.files.all():
        target = _planned_target(file, old_title, new_title)
        if target is None or target[1:] != (file.dirname, file.basename):
            left.append(file.file_path)
    return left


def _show_dir(folder, dirname):
    """Return the part of a planned dirname that ends with its show folder."""

    parts = dirname.split("/")
    return "/".join(parts[: parts.index(folder) + 1])


def _raise(error):
    """Raise the error of os.walk. A silent walk can hide a file."""

    raise error


def _account_for_files(targets):
    """Return the claimed and the unclaimed media files of the new show folders.

    targets holds (file, (folder, new_dirname, new_basename)) for each
    record of the series. Invariant: each media file in the new show
    folders belongs to exactly 1 record. A record claims a file of its
    planned folder that has its planned name. It claims a file under its
    old basename only when its old path holds no other file. Thus, a
    copy made by hand does not pass. On a volume that ignores case, the
    old path of a case-only rename is the same file. The size of the
    file must be the size of the record, when the record has one. Thus,
    an S3-only record of an other quality does not claim a file that
    Sonarr gave its name. Sonarr moves a series folder and keeps the name of a file
    that it does not track. The old path of each file is then gone.
    Thus, a record can be an archived-only record only when no file of
    the new folders is left unclaimed.

    The walk never enters a hidden directory or an @eaDir tree. It
    reads only video files, so artwork, thumbs and subtitles do not
    count. Return 2 sets of paths relative to LIBRARY_DIR. A fault of
    the walk or of a size read raises OSError. A show folder that
    os.path.isdir does not find is not read, also when its stat fails.
    os.walk does not enter a subdirectory that it cannot stat."""

    from app.media.manual_imports import VIDEO_EXTENSIONS

    library_dir = current_app.config["LIBRARY_DIR"]
    found = set()
    for show_dir in sorted({_show_dir(folder, d) for _f, (folder, d, _b) in targets}):
        top = os.path.join(library_dir, show_dir)
        if not os.path.isdir(top):
            continue
        for path, subdirs, names in os.walk(top, onerror=_raise):
            subdirs[:] = [
                name
                for name in subdirs
                if not name.startswith(".") and name != "@eaDir"
            ]
            found.update(
                os.path.relpath(os.path.join(path, name), library_dir)
                for name in names
                if not name.startswith(".") and name.lower().endswith(VIDEO_EXTENSIONS)
            )

    def claims(file, path):
        if path not in found or path in claimed:
            return False
        size = file.filesize_bytes
        absolute = os.path.join(library_dir, path)
        return size is None or os.path.getsize(absolute) == int(size)

    def copy_of(old, kept):
        old, kept = os.path.join(library_dir, old), os.path.join(library_dir, kept)
        return os.path.isfile(old) and not os.path.samefile(old, kept)

    claimed = set()
    for file, (_folder, new_dirname, new_basename) in targets:
        planned = f"{new_dirname}/{new_basename}"
        kept = f"{new_dirname}/{file.basename}"
        if claims(file, planned):
            claimed.add(planned)
        elif claims(file, kept) and not copy_of(file.file_path, kept):
            claimed.add(kept)
    return claimed, found - claimed


def _tv_library_down():
    """Return True if the TV library is not there or does not answer.

    A dropped share hides each file of each series. Then a file with
    neither its old nor its new path reads as an archive-only record."""

    tv_library = current_app.config["TV_LIBRARY"]
    return not os.path.isdir(tv_library) or bool(videos._dead_volumes([tv_library]))


def _move_series(series, new_title, waits=None):
    """Move the files and rewrite the rows of a series. The caller holds the locks.

    An unmounted TV library defers the rename. It must not rewrite the
    rows of files that it cannot see. A media file in the new
    folder that no record accounts for stops the rename before a row
    changes. A record whose file has its old basename in the new
    folder gets the file renamed to the planned name. A resumed rename
    whose rows changed already waits for the record of such a file. It
    must not stop for good, because the rows have the new folder
    already."""

    if _tv_library_down():
        _requeue_rename(
            series,
            new_title,
            "waits for the TV library volume",
            waits,
            counted="volume_waits",
        )
        return False

    old_title = series.title
    library_dir = current_app.config["LIBRARY_DIR"]
    old_prefix = f"{old_title} - "
    new_prefix = f"{new_title} - "

    # Plan each file first. If 1 file does not carry the series title,
    # nothing changes. A partial rename gave the series a title and a
    # Sonarr path for a folder that did not exist.

    plan = []
    targets = []
    unmatched = []
    sonarr_folder = _folder_title(new_title)
    for file in series.files.all():
        target = _planned_target(file, old_title, new_title)
        if target is None:
            unmatched.append(file.file_path)
            continue
        sonarr_folder, new_dirname, new_basename = target
        plan.append((file, new_dirname, new_basename))
        targets.append((file, target))
    if unmatched:
        current_app.logger.warning(
            f"Series rename: {len(unmatched)} file(s) don't carry the "
            f"series title, so nothing changed: {unmatched[:5]}"
        )
        return False

    # The new folder can exist already, for example after a rename in
    # Sonarr. Each media file in it must belong to a record before a
    # row changes.

    try:
        _claimed, unclaimed = _account_for_files(targets)
    except OSError as error:
        current_app.logger.warning(
            f"Series rename: the new folder of {old_title!r} could not be read: "
            f"{type(error).__name__}: {error}"
        )
        _requeue_rename(
            series,
            new_title,
            "waits for the TV library volume",
            waits,
            counted="volume_waits",
        )
        return False
    if unclaimed:
        names = sorted(unclaimed)[:5]
        if any(file.file_path == f"{d}/{b}" for file, d, b in plan):
            current_app.logger.warning(
                f"Series rename: {len(unclaimed)} media file(s) in the new "
                f"folder belong to no record of the series. The rows that are "
                f"renamed already stay renamed: {names}"
            )
            if not _requeue_rename(
                series,
                new_title,
                "waits for a record of each media file in the new folder",
                waits,
                counted="stray_waits",
            ):
                _tell_admin_of_stray_files(series, new_title, unclaimed, resumed=True)
            return False
        current_app.logger.warning(
            f"Series rename: {len(unclaimed)} media file(s) in the new folder "
            f"belong to no record of the series, so nothing changed: {names}"
        )
        _mark_conflict(series)
        _tell_admin_of_stray_files(series, new_title, unclaimed)
        return False

    # Keep the Plex item of the show through the rename. The show folder
    # is the first folder that the plan changes. A resumed rename, whose
    # rows carry the new folder already, has no folder to keep.

    changed = next(
        (
            (file.dirname.split("/"), new_dirname.split("/"))
            for file, new_dirname, _basename in plan
            if file.dirname != new_dirname
        ),
        None,
    )
    if changed is not None:
        old_parts, new_parts = changed
        depth = next(
            index
            for index, (old_part, new_part) in enumerate(zip(old_parts, new_parts))
            if old_part != new_part
        )
        remember_rename(
            os.path.join(library_dir, *old_parts[: depth + 1]),
            os.path.join(library_dir, *new_parts[: depth + 1]),
            "show",
            tmdb_id=series.tmdb_id,
            tvdb_id=series.tvdb_id,
        )

    moved = db_only = 0
    old_dirs = set()
    for file, new_dirname, new_basename in plan:
        new_path = f"{new_dirname}/{new_basename}"
        src = os.path.join(library_dir, file.file_path)
        dst = os.path.join(library_dir, new_path)
        # Sonarr moved the folder and kept the name of a file that it
        # does not track. The file is in the new folder under its
        # old basename.
        kept_name = os.path.join(library_dir, new_dirname, file.basename)
        source = src if os.path.isfile(src) else kept_name

        if new_path == file.file_path:
            # An earlier run renamed this row.
            if os.path.isfile(dst):
                moved += 1
            else:
                db_only += 1
        elif os.path.isfile(src) or os.path.isfile(kept_name):
            # os.rename replaces a file at the target without a word. The
            # movie rename refuses this shape. A case-only rename
            # on a case-insensitive volume is the same file. It goes on.
            if os.path.exists(dst) and not os.path.samefile(source, dst):
                # A retry cannot clear this. Notify 1 time and stop.
                _refuse_duplicate_target(
                    series, os.path.relpath(source, library_dir), new_path
                )
                return False
            try:
                makedirs_on_mounted_volume(os.path.dirname(dst))
                videos._rename_with_retries(source, dst)
            except OSError as error:
                # The share dropped inside the loop. The
                # rows renamed so far are committed. The retry resumes.
                current_app.logger.warning(
                    f"Series rename: {file.file_path!r} not moved: "
                    f"{type(error).__name__}: {error}"
                )
                _requeue_rename(
                    series,
                    new_title,
                    "waits for the TV library volume",
                    waits,
                    counted="volume_waits",
                )
                return False
            # The episode thumb goes with its video. The old
            # folder of a file that Sonarr moved is gone already, and
            # clear_leftover_directory returns at once for it.
            move_episode_thumb(source, dst)
            old_dirs.add(os.path.dirname(src))
            moved += 1
        elif os.path.isfile(dst):
            # A previous partial run moved the file. Complete the row.
            moved += 1
        elif _tv_library_down():
            # The volume dropped during the loop. The rows that are
            # renamed already stay renamed. The retry does the rest.
            _requeue_rename(
                series,
                new_title,
                "waits for the TV library volume",
                waits,
                counted="volume_waits",
            )
            return False
        else:
            # No file is at the old path, at the planned name or under the
            # old basename. The count found a record for each media file
            # of the new show folders. Thus, this record counts as
            # an archived-only record. The count does not read a folder
            # outside the new show folders. The rows get the new name. The
            # archive keeps its key.
            db_only += 1

        old_names = (file.basename, file.untouched_basename)
        file.dirname = new_dirname
        file.basename = new_basename
        file.file_path = new_path
        if file.plex_title.startswith(old_prefix):
            file.plex_title = new_prefix + file.plex_title[len(old_prefix) :]
        # The File Activity trail is keyed by the import name or by the
        # library name. Thus, the trail follows the new names, or the
        # card loses its chips for the life of the trail.
        file.untouched_basename = reconstruct_filename(file.id, series_title=new_title)
        migrate_renamed_file(
            current_app.redis, old_names, (file.basename, file.untouched_basename)
        )
        db.session.commit()

    # An import can start during the loop. It can attach a file under the
    # old title at its end. Then the title stays, and the rename
    # goes back on its queue. The retry counts the renamed rows as done.
    # A known and accepted gap stays: an import that starts after this
    # check and before the title commit below. The window is the time of
    # 1 commit and the leftover-folder pass. The nightly check reports
    # such a series as a conflict.

    source = chain.series_import_in_flight({old_title, new_title})
    left = _rows_not_renamed(series, old_title, new_title)
    if source or left:
        reason = (
            f"waits for the import of '{os.path.basename(source)}'"
            if source
            else f"found {len(left)} file(s) with the old title: {left[:5]}"
        )
        _requeue_rename(series, new_title, reason, waits)
        return False

    # This step is aware of junk files. It is not a plain rmdir. The
    # old series folder usually keeps a poster.png (from Sonarr or
    # placed by hand). Before, that file kept the empty folder alive
    # until the weekly sweep.

    for old_dir in sorted(old_dirs, key=len, reverse=True):
        clear_leftover_directory(old_dir)

    series.title = new_title
    series.date_updated = datetime.now(timezone.utc)
    db.session.commit()

    try:
        _update_sonarr_path(series, old_title, sonarr_folder)
    except Exception:
        current_app.logger.warning(traceback.format_exc())

    if current_app.config.get("PLEX_URL") and current_app.config.get("PLEX_TOKEN"):
        enqueue_task(
            "app.plex.plex_library.refresh_plex_libraries",
            description="Refreshing Plex libraries",
        )

    current_app.logger.info(
        f"Series rename: {old_title!r} -> {new_title!r}: {moved} files "
        f"moved, {db_only} archived-only rows"
    )
    return True


# The Sonarr path check. A person renames a series in Fitzflix, and the
# rename task gives Sonarr the new folder. The check finds the series
# whose Sonarr folder is not the Fitzflix folder.

# The nightly check repoints at most this many series. More than this
# point to a systemic cause, for example a changed mount or root
# folder. Then the check changes nothing and warns.

MAX_SONARR_FIXES = 20

# A series with a conflict gets this marker for 2 days. While it stands,
# the weekly S3 sync requests no restore of a file of the series. A
# record of such a series can name a path that Sonarr moved away.
# The nightly check sets the marker again each night at 05:45. A life
# of 1 day ended at the next check, with no margin. A life of 2 days
# keeps the marker through 1 night with no check. Then the marker of
# Friday still stands at the sync of Sunday 02:00.

SONARR_CONFLICT_KEY = "fitzflix:sonarr:conflict:{series_id}"
SONARR_CONFLICT_TTL = 2 * 86400

# A refusal mails the admin 1 time for each life of its marker. The
# first key is for a rename that media files with no record stop. The
# second key is for a conflict of the path check. Each marker has the
# life of the conflict marker.

SONARR_STRAY_MAIL_KEY = "fitzflix:sonarr:stray-mail:{series_id}"
SONARR_CONFLICT_MAIL_KEY = "fitzflix:sonarr:conflict-mail:{series_id}"


def _first_refusal(key):
    """Return True if no mail marker stands at key. Give the marker a new life.

    Each refusal gives the marker a new life, as the conflict marker
    gets each night. Thus, a refusal that comes back each night sends 1
    mail. A Redis fault must not hide the refusal, and it returns True."""

    try:
        if current_app.redis.set(key, "1", ex=SONARR_CONFLICT_TTL, nx=True):
            return True
        current_app.redis.expire(key, SONARR_CONFLICT_TTL)
        return False
    except Exception:
        current_app.logger.warning(traceback.format_exc())
        return True


def _summary(**counts):
    """Return the summary of a Sonarr path check, with each count at 0 first."""

    summary = {"checked": 0, "repointed": 0, "conflicts": 0}
    summary.update(counts)
    return summary


def _series_lookup():
    """Return the TV series by TheTVDB id and by TMDB id.

    An id that 2 series share is left out. Such a match is not certain."""

    by_tvdb, by_tmdb = {}, {}
    for series in TVSeries.query.all():
        for index, key in ((by_tvdb, series.tvdb_id), (by_tmdb, series.tmdb_id)):
            if key:
                index[key] = None if key in index else series
    return by_tvdb, by_tmdb


def _match_series(entry, lookup):
    """Return the TV series of a Sonarr series, or None.

    The match is by TheTVDB id, the native key of Sonarr. The TMDB id is
    the fallback, and only for a series that has no TheTVDB id."""

    by_tvdb, by_tmdb = lookup
    tvdb_id = entry.get("tvdbId")
    if tvdb_id and by_tvdb.get(tvdb_id) is not None:
        return by_tvdb[tvdb_id]
    series = by_tmdb.get(entry.get("tmdbId")) if entry.get("tmdbId") else None
    if series is not None and series.tvdb_id is None:
        return series
    return None


def sonarr_folder_of(entry):
    """Return the folder name at the end of the path of a Sonarr series."""

    return os.path.basename(os.path.normpath(entry.get("path") or "/"))


def fitzflix_folders(series):
    """Return the set of show folders that the files of a series are in.

    The show folder is the second part of the dirname, after the TV
    library part. A dirname outside the TV library gives None."""

    folders = set()
    rows = File.query.filter_by(series_id=series.id).with_entities(File.dirname)
    for (dirname,) in rows.distinct():
        parts = dirname.split("/")
        folders.add(parts[1] if len(parts) > 1 and parts[0] == TV_SHOWS else None)
    return folders


def _mark_conflict(series):
    """Mark the series as a Sonarr conflict for SONARR_CONFLICT_TTL.

    A Redis fault does not stop the caller."""

    try:
        current_app.redis.set(
            SONARR_CONFLICT_KEY.format(series_id=series.id),
            "1",
            ex=SONARR_CONFLICT_TTL,
        )
    except Exception:
        current_app.logger.warning(traceback.format_exc())


def _tell_admin_of_conflict(series, detail):
    """Mail the admin that the 2 folders of a series differ.

    The mail goes out 1 time for each life of SONARR_CONFLICT_MAIL_KEY
    (_first_refusal). The function never raises."""

    from markupsafe import escape

    try:
        if not _first_refusal(SONARR_CONFLICT_MAIL_KEY.format(series_id=series.id)):
            return
        action = (
            "Fitzflix changed nothing. If you renamed the series in Sonarr, "
            "give it the same title in Fitzflix, with the Rename Series form "
            "of its page. If you did not, give Sonarr the folder of Fitzflix "
            "again. Until the 2 folders are the same, the weekly S3 sync "
            "requests no restore for this series."
        )
        email.notify_admin_text(
            "Sonarr folder needs triage",
            f"{detail}.\n\n{action}",
            f"<p>{escape(detail)}.</p><p>{escape(action)}</p>",
        )
    except Exception:
        current_app.logger.error(traceback.format_exc())


def _conflict(series, detail):
    """Return the conflict branch of a series whose 2 folders differ.

    detail says how they differ. The function writes one warning, marks
    the series for SONARR_CONFLICT_TTL, and mails the admin."""

    current_app.logger.warning(f"Sonarr path check: {detail}. Not changed")
    _mark_conflict(series)
    _tell_admin_of_conflict(series, detail)
    return "conflict", None


def series_in_sonarr_conflict(series_ids):
    """Return the ids of the series that have the Sonarr conflict marker.

    The weekly S3 sync calls this 1 time for its file pass.
    1 MGET reads the markers of all the series. A Redis fault
    raises, and the caller decides the safe side."""

    ids = sorted(series_ids)
    if not ids:
        return set()
    values = current_app.redis.mget(
        [SONARR_CONFLICT_KEY.format(series_id=series_id) for series_id in ids]
    )
    return {series_id for series_id, value in zip(ids, values) if value is not None}


def _sonarr_branch(series, entry, renaming=()):
    """Compare the Sonarr folder of a series with its Fitzflix folder.

    Return the branch and its value. "no files" and "equal" change
    nothing. "repoint" gives the Fitzflix folder. "conflict" changes
    nothing. It writes one warning and mails the admin. renaming holds
    the ids of the series whose rename waits or runs. The files of such
    a series can be in 2 folders. It gets the marker and no mail."""

    sonarr_folder = sonarr_folder_of(entry)
    folders = fitzflix_folders(series)
    if not folders:
        return "no files", None
    if series.id in renaming:
        _mark_conflict(series)
        return "renaming", None
    if len(folders) > 1 or None in folders:
        return _conflict(
            series, f"The files of {series.title!r} are in more than one show folder"
        )
    (folder,) = folders
    if folder == sonarr_folder:
        return "equal", folder
    if not sonarr_folder:
        return _conflict(series, f"Sonarr gives no folder for {series.title!r}")

    # The check reads the folders under TV_LIBRARY, and a repoint keeps
    # the parent of the Sonarr path. Thus, a series on another Sonarr
    # root folder is a conflict. A repoint there names a missing folder.

    library = current_app.config["TV_LIBRARY"]
    parent = os.path.dirname(os.path.normpath(entry.get("path") or "/"))
    if os.path.realpath(parent) != os.path.realpath(library):
        return _conflict(
            series,
            f"Sonarr holds {series.title!r} under {parent!r}, not under the "
            f"TV library",
        )
    fitzflix_exists = os.path.isdir(os.path.join(library, folder))
    sonarr_exists = os.path.isdir(os.path.join(library, sonarr_folder))
    if fitzflix_exists and not sonarr_exists:
        return "repoint", folder
    on_disk = {
        (True, False): "Only the Sonarr folder is on the disk",
        (True, True): "The 2 folders are on the disk",
        (False, False): "No folder of the 2 is on the disk",
    }[(sonarr_exists, fitzflix_exists)]
    return _conflict(
        series,
        f"{series.title!r} is at {sonarr_folder!r} in Sonarr and at {folder!r} "
        f"in Fitzflix. {on_disk}",
    )


def _tv_library_missing():
    """Return True, with a warning, if the TV library directory is not there.

    A dropped mount hides both folders of every series. Then the check
    changes nothing and writes one warning, not one for each series."""

    if os.path.isdir(current_app.config["TV_LIBRARY"]):
        return False
    current_app.logger.warning(
        "Sonarr path check: the TV library is not mounted. Nothing changed"
    )
    return True


def _adopt_tvdb_id(series, entry):
    """Give a series the TheTVDB id of its Sonarr series, if it has none.

    A series that the TMDB id matched has no TheTVDB id. Without one,
    Fitzflix cannot find the Sonarr series to repoint or rescan it. The
    id is set only when no other series holds it."""

    tvdb_id = entry.get("tvdbId")
    if series.tvdb_id or not tvdb_id:
        return
    if TVSeries.query.filter_by(tvdb_id=tvdb_id).first() is not None:
        return
    series.tvdb_id = tvdb_id
    db.session.commit()
    current_app.logger.info(
        f"Sonarr path check: {series.title!r} takes the TheTVDB id {tvdb_id} "
        f"of its Sonarr series"
    )


def _repoint(series, entry, folder):
    """Point the Sonarr series at the Fitzflix folder. Return True if it was done."""

    sonarr_folder = sonarr_folder_of(entry)
    _adopt_tvdb_id(series, entry)
    try:
        done = _update_sonarr_path(series, sonarr_folder, folder)
    except Exception as e:
        current_app.logger.warning(
            f"Sonarr path check: couldn't repoint {series.title!r} from "
            f"{sonarr_folder!r} to {folder!r}: {e}"
        )
        return False
    current_app.logger.info(
        f"Sonarr path check: Sonarr points {series.title!r} at the missing "
        f"folder {sonarr_folder!r}. "
        + (f"Repointed at {folder!r}" if done else "Sonarr did not take the path")
    )
    return done


def reconcile_sonarr_paths():
    """Compare each Sonarr series folder with the Fitzflix folder of the series.

    This is a task. It runs nightly. A push to Sonarr after a Fitzflix
    rename is lost when Sonarr is down. A person can also move a series
    folder in Sonarr, and Sonarr sends no event for that. This check
    finds both cases. It matches each Sonarr series to a TV series by
    TheTVDB id and compares the 2 folders.

    If only the Fitzflix folder exists, Sonarr is repointed at it. Each
    other difference is a conflict. The check then changes nothing,
    writes a warning, and mails the admin. A series that Sonarr does
    not manage is not checked. Return the summary counts."""

    with app.app_context():
        if not sonarr_push.sonarr_configured():
            return _summary(error="Sonarr is not configured")
        if _tv_library_missing():
            return _summary(error="TV library not mounted")
        try:
            entries = sonarr_push._sonarr("GET", "/api/v3/series") or []
        except Exception as e:
            current_app.logger.warning(f"Sonarr path check: couldn't list series: {e}")
            return _summary(error="Sonarr unreachable")

        lookup = _series_lookup()
        renaming = series_with_a_rename()
        summary = _summary()
        fixes = []
        for entry in entries:
            series = _match_series(entry, lookup)
            if series is None:
                continue
            branch, value = _sonarr_branch(series, entry, renaming)
            if branch == "no files":
                continue
            summary["checked"] += 1
            if branch == "conflict":
                summary["conflicts"] += 1
            elif branch == "repoint":
                fixes.append((series, entry, value))

        if len(fixes) > MAX_SONARR_FIXES:
            current_app.logger.warning(
                f"Sonarr path check: Sonarr points {len(fixes)} series at a "
                f"missing folder, more than {MAX_SONARR_FIXES}. Nothing changed. "
                f"Check the Sonarr root folder and the library mount"
            )
            return _summary(**summary, over_limit=len(fixes))

        for series, entry, folder in fixes:
            if _repoint(series, entry, folder):
                summary["repointed"] += 1

        current_app.logger.info(
            f"Sonarr path check: {len(entries)} Sonarr series, "
            f"{summary['checked']} checked, {summary['repointed']} repointed, "
            f"{summary['conflicts']} left for review"
        )
        return summary
