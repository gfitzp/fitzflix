"""Rename a TV series on disk and in the database.

This is the fix for Plex disambiguation (Batman becomes Batman (1966)).

Fitzflix keeps its own TV naming. A series folder carries a year only
when Glenn added one. Sonarr is where a series gets renamed (#370). A
rename in Sonarr carries over to Fitzflix: the title, the folder, the
file rows, the untouched names, and then the archive keys. The Sonarr
path check (reconcile_sonarr_paths) does this nightly. The Sonarr
webhook queues the check of one series when its folder differs. A
Sonarr rename of episode files in the same folder changes only the
rows. A series that Sonarr does not manage is not changed. Nothing
adds a year to a series on its own.

The archive keys follow the untouched names (#369). A rename rewrites
the untouched name of each file. Then it requests the move of each
archive to the key of the new name. The move job copies the S3 object
on the server side, after a restore from Deep Archive when necessary.
aws_key changes only after the copy is verified. Thus, aws_key names a
real object at every moment. No S3 work starts before the check that
each file carries the series title (#317).

The task is resumable. Each file commits after its own disk move. A
file that is already at its target counts as moved. A row that already
has its new folder and name counts as done (#374). Thus, a retry after
a stop in the loop completes the series. A record whose
local file was deleted intentionally (archived-only physical media)
gets new rows with no disk operation. The weekly sync cannot run during
a rename. The sync defers unless every queue is idle, and this task
occupies one queue.
"""

import os
import re
import time
import traceback

from datetime import datetime, timezone

from flask import current_app, g
from pathvalidate import sanitize_filename
from werkzeug.local import LocalProxy

from app.custom_artwork import move_episode_thumb
from app.tasks import delete_job, enqueue_task
from app.aws_storage import request_archive_move
from app.pipeline import migrate_renamed_file
from app import db, get_app, makedirs_on_mounted_volume, safe_job_id
from app.models import TVSeries

app = LocalProxy(get_app)


def _update_sonarr_path(series, old_folder, new_folder):
    """Point the Sonarr series at the renamed folder, then request a rescan.

    Sonarr matches the series by TheTVDB id, its native key. Thus, the
    Sonarr imports and upgrades continue to arrive in the correct folder.
    The PUT does not move files. This task moved them already. The
    RescanSeries command makes Sonarr see the renamed files at once.
    The call goes through the arr client (app.arr_client)."""

    from app.arr_client import SONARR, rescan_command
    from app.sonarr_push import _sonarr, sonarr_configured

    if not (sonarr_configured() and series.tvdb_id):
        return False

    listing = _sonarr("GET", f"/api/v3/series?tvdbId={int(series.tvdb_id)}") or []
    entry = next(
        (item for item in listing if item.get("tvdbId") == series.tvdb_id), None
    )
    if entry is None:
        current_app.logger.info(
            f"Series rename: Sonarr doesn't manage tvdb {series.tvdb_id}, skipping"
        )
        return False

    old_path = entry.get("path") or ""
    new_path = os.path.join(os.path.dirname(old_path), new_folder)
    entry["path"] = new_path
    _sonarr("PUT", f"/api/v3/series/{entry['id']}", entry)
    current_app.logger.info(f"Series rename: Sonarr path {old_path!r} -> {new_path!r}")
    _sonarr("POST", "/api/v3/command", rescan_command(SONARR, entry["id"]))
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
    "Les Misérables (2018)" (#408). The test reads the folder that the
    task writes (_folder_title), with no period at the end (#552). Thus,
    "The Venture Bros." is safe."""

    folder = _folder_title(title)
    return bool(folder) and sanitize_filename(folder) == folder


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
    folder and in its name. The rename task changes nothing then (#317).
    A row that already carries the new title in both places is done. An
    earlier run of the task stopped after it (#374). Its target is its
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


def rename_tv_series_task(series_id, new_title):
    """Rename the folder, files, and database rows of a TV series.

    This is a task. After the rename, it updates the Sonarr path and
    queues a Plex rescan."""

    with app.app_context():
        series = db.session.get(TVSeries, series_id)
        if series is None:
            current_app.logger.warning(f"Series rename: no series {series_id}")
            return False

        refusal = rename_refusal(series, new_title)
        if refusal:
            current_app.logger.warning(f"Series rename: {refusal}")
            return False

        locks = _take_series_locks(series, new_title)
        if locks is None:
            return False
        try:
            return _move_series(series, new_title)
        finally:
            for lock in locks:
                current_app.lock_manager.unlock(lock)


# A rename waits this many minutes when an import of the series is not
# done. It also waits when another task holds an episode lock.

RENAME_WAIT_MINUTES = (5, 15)

RENAME_TASK = "app.series_rename.rename_tv_series_task"

# The time limit of a rename grows with the number of files (#374). A
# move on NFS can take 25 seconds with its retries. The episode locks
# have the same time limit as the job.

RENAME_SECONDS_PER_FILE = 30


def rename_timeout(series):
    """Return the time limit of the rename of a series, in seconds.

    It is SQL_TASK_TIMEOUT, or RENAME_SECONDS_PER_FILE for each file if
    that is longer."""

    floor = current_app.config["SQL_TASK_TIMEOUT"]
    return max(floor, RENAME_SECONDS_PER_FILE * series.files.count())


def _take_series_locks(series, new_title):
    """Take the title lock of each episode of a series. Return the locks.

    Return None after the rename goes back on its queue. An import takes
    the lock of its episode. A new episode has no lock yet, so the
    rename also waits for each import of the series that is not done.
    Else the import attaches by the old title at its end and makes a
    second series (#370)."""

    from app.importing import series_import_in_flight
    from app.models import TV_SHOWS, title_lock_key

    reason = None
    source = series_import_in_flight({series.title, new_title})
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
    _requeue_rename(series, new_title, reason)
    return None


def _lock_seconds(series):
    """Return the TTL of the episode locks of a rename, in seconds.

    Under rq, it is the timeout of the running job. The job got that
    timeout from the file count when it was queued. A file that left
    the series since then must not make the locks shorter than the job
    (#374 follow-up). Outside rq, it is rename_timeout(series)."""

    from rq import get_current_job

    job = get_current_job()
    if job is not None and job.timeout:
        return max(int(job.timeout), rename_timeout(series))
    return rename_timeout(series)


def _requeue_rename(series, new_title, reason):
    """Put the rename of a series back on its queue after RENAME_WAIT_MINUTES."""

    from app.tasks import requeue

    requeue(
        RENAME_TASK,
        key=series.id,
        delay=RENAME_WAIT_MINUTES,
        reason=f"Series rename of {series.title!r} {reason}",
        args=(series.id, new_title),
        timeout=rename_timeout(series),
    )


def _rows_not_renamed(series, old_title, new_title):
    """Return the paths of the files of a series that do not have the new title yet."""

    left = []
    for file in series.files.all():
        target = _planned_target(file, old_title, new_title)
        if target is None or target[1:] != (file.dirname, file.basename):
            left.append(file.file_path)
    return left


def _tv_library_down():
    """Return True if the TV library is not there or does not answer (#551).

    A dropped share hides each file of each series. Then a file with
    neither its old nor its new path reads as an archive-only record."""

    from app.videos import _dead_volumes

    tv_library = current_app.config["TV_LIBRARY"]
    return not os.path.isdir(tv_library) or bool(_dead_volumes([tv_library]))


def _move_series(series, new_title):
    """Move the files and rewrite the rows of a series. The caller holds the locks.

    An unmounted TV library defers the rename. It must not rewrite the
    rows of files that it cannot see (#551)."""

    if _tv_library_down():
        _requeue_rename(series, new_title, "waits for the TV library volume")
        return False

    old_title = series.title
    library_dir = current_app.config["LIBRARY_DIR"]
    old_prefix = f"{old_title} - "
    new_prefix = f"{new_title} - "

    from app.importing import reconstruct_filename, series_import_in_flight
    from app.videos import _rename_with_retries

    # Plan each file first. If 1 file does not carry the series title,
    # nothing changes. A partial rename gave the series a title and a
    # Sonarr path for a folder that did not exist (#317).

    plan = []
    unmatched = []
    sonarr_folder = _folder_title(new_title)
    for file in series.files.all():
        target = _planned_target(file, old_title, new_title)
        if target is None:
            unmatched.append(file.file_path)
            continue
        sonarr_folder, new_dirname, new_basename = target
        plan.append((file, new_dirname, new_basename))
    if unmatched:
        current_app.logger.warning(
            f"Series rename: {len(unmatched)} file(s) don't carry the "
            f"series title, so nothing changed: {unmatched[:5]}"
        )
        return False

    moved = db_only = 0
    old_dirs = set()
    for file, new_dirname, new_basename in plan:
        new_path = f"{new_dirname}/{new_basename}"
        src = os.path.join(library_dir, file.file_path)
        dst = os.path.join(library_dir, new_path)

        if new_path == file.file_path:
            # An earlier run renamed this row (#374).
            if os.path.isfile(dst):
                moved += 1
            else:
                db_only += 1
        elif os.path.isfile(src):
            makedirs_on_mounted_volume(os.path.dirname(dst))
            _rename_with_retries(src, dst)
            # The episode thumb goes with its video (#469)
            move_episode_thumb(src, dst)
            old_dirs.add(os.path.dirname(src))
            moved += 1
        elif os.path.isfile(dst):
            # A previous partial run moved the file. Complete the row.
            moved += 1
        elif _tv_library_down():
            # The volume dropped during the loop. The rows that are
            # renamed already stay renamed. The retry does the rest.
            _requeue_rename(series, new_title, "waits for the TV library volume")
            return False
        else:
            # There is no local copy. This is an archived-only record.
            # The rows get the new name. The archive moves too.
            db_only += 1

        old_names = (file.basename, file.untouched_basename)
        file.dirname = new_dirname
        file.basename = new_basename
        file.file_path = new_path
        if file.plex_title.startswith(old_prefix):
            file.plex_title = new_prefix + file.plex_title[len(old_prefix) :]
        # The File Activity trail is keyed by the import name or by the
        # library name. Thus, the trail follows the new names, or the
        # card loses its chips for the life of the trail (#375).
        file.untouched_basename = reconstruct_filename(file.id, series_title=new_title)
        migrate_renamed_file(
            current_app.redis, old_names, (file.basename, file.untouched_basename)
        )
        # The move job goes to the queue at this commit.
        request_archive_move(file)
        db.session.commit()

    # An import can start during the loop. It can attach a file under the
    # old title at its end (#373). Then the title stays, and the rename
    # goes back on its queue. The retry counts the renamed rows as done.
    # A known and accepted gap stays: an import that starts after this
    # check and before the title commit below. The window is the time of
    # 1 commit and the leftover-folder pass. The nightly check reports
    # such a series as a conflict (review of 2026-10-26).

    source = series_import_in_flight({old_title, new_title})
    left = _rows_not_renamed(series, old_title, new_title)
    if source or left:
        reason = (
            f"waits for the import of '{os.path.basename(source)}'"
            if source
            else f"found {len(left)} file(s) with the old title: {left[:5]}"
        )
        _requeue_rename(series, new_title, reason)
        return False

    # This step is aware of junk files. It is not a plain rmdir. The
    # old series folder usually keeps a poster.png (from Sonarr or
    # placed by hand). Before, that file kept the empty folder alive
    # until the weekly sweep.

    from app.maintenance import clear_leftover_directory

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
            "app.plex_library.refresh_plex_libraries",
            description="Refreshing Plex libraries",
        )

    current_app.logger.info(
        f"Series rename: {old_title!r} -> {new_title!r}: {moved} files "
        f"moved, {db_only} archived-only rows"
    )
    return True


# The Sonarr path check (#370). Sonarr is where a series gets renamed.
# Fitzflix keeps its own TV naming, so a folder carries a year only when
# Glenn added one. The check finds the series whose Sonarr folder is not
# the Fitzflix folder and carries the change over in one direction.

# The nightly check changes at most this many series. More mismatches
# than this point to a systemic cause, for example a changed mount or
# root folder. Then the check changes nothing and warns.

MAX_SONARR_FIXES = 20

# The Sonarr events that carry series.path. The webhook compares the
# folder of each one before it acknowledges the event.

SONARR_SERIES_EVENTS = ("Rename", "Download", "SeriesAdd")

# A series with a conflict gets this marker for 1 day. While it stands,
# a Download event of the series queues no check. A Rename and a
# SeriesAdd still do. Thus, a series that stays in conflict does not
# write a warning for each episode.

SONARR_CONFLICT_KEY = "fitzflix:sonarr:conflict:{series_id}"
SONARR_CONFLICT_TTL = 86400


def _summary(**counts):
    """Return the summary of a Sonarr path check, with each count at 0 first."""

    summary = {"checked": 0, "renamed": 0, "repointed": 0, "conflicts": 0}
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


def find_series(entry):
    """Return the TV series of one Sonarr series, or None.

    This is the match of _match_series, with 2 small queries in place of
    the full lookup."""

    lookup = ({}, {})
    for index, column, key in (
        (lookup[0], TVSeries.tvdb_id, entry.get("tvdbId")),
        (lookup[1], TVSeries.tmdb_id, entry.get("tmdbId")),
    ):
        found = TVSeries.query.filter(column == key).limit(2).all() if key else []
        if len(found) == 1:
            index[key] = found[0]
    return _match_series(entry, lookup)


def sonarr_folder_of(entry):
    """Return the folder name at the end of the path of a Sonarr series."""

    return os.path.basename(os.path.normpath(entry.get("path") or "/"))


def fitzflix_folders(series):
    """Return the set of show folders that the files of a series are in.

    The show folder is the second part of the dirname, after the TV
    library part. A dirname outside the TV library gives None."""

    from app.models import TV_SHOWS, File

    folders = set()
    rows = File.query.filter_by(series_id=series.id).with_entities(File.dirname)
    for (dirname,) in rows.distinct():
        parts = dirname.split("/")
        folders.add(parts[1] if len(parts) > 1 and parts[0] == TV_SHOWS else None)
    return folders


def _rename_title(series, folder, sonarr_folder):
    """Return the title that gives the series the Sonarr folder, or None.

    The title goes through the checks of the rename task. The title must
    be safe and free. The folder that the task makes from it must be the
    Sonarr folder. At least one file must already be at its new path.
    Otherwise the task moves nothing, and the records point at a folder
    with no files. A refusal writes one warning."""

    old_folder = _folder_title(series.title)
    tail = folder[len(old_folder) :] if folder.startswith(old_folder) else None
    title = sonarr_folder
    if tail and sonarr_folder.endswith(tail):
        title = sonarr_folder[: -len(tail)]

    reason = None
    if tail is None:
        reason = f"the Fitzflix folder does not start with {old_folder!r}"
    elif title == series.title:
        reason = "the title does not change"
    elif not _filesystem_safe(title):
        reason = f"{title!r} is not safe as a folder name"
    elif _renamed_folder(folder, old_folder, _folder_title(title)) != sonarr_folder:
        reason = f"the title {title!r} does not give the folder {sonarr_folder!r}"
    elif (
        TVSeries.query.filter(TVSeries.title == title, TVSeries.id != series.id).first()
        is not None
    ):
        reason = f"a series titled {title!r} exists"
    else:
        library_dir = current_app.config["LIBRARY_DIR"]
        targets = [
            _planned_target(file, series.title, title) for file in series.files.all()
        ]
        if None in targets:
            reason = "a file does not carry the series title"
        elif not any(
            os.path.isfile(os.path.join(library_dir, dirname, basename))
            for _folder, dirname, basename in targets
        ):
            reason = "no file is at its new path in the Sonarr folder"
    if reason:
        current_app.logger.warning(
            f"Sonarr path check: {series.title!r} is at {sonarr_folder!r} in "
            f"Sonarr and at {folder!r} in Fitzflix. Not renamed, because "
            f"{reason}"
        )
        return None
    return title


def _conflict(series):
    """Return the conflict branch and mark the series for SONARR_CONFLICT_TTL.

    A Redis fault does not stop the check."""

    try:
        current_app.redis.set(
            SONARR_CONFLICT_KEY.format(series_id=series.id),
            "1",
            ex=SONARR_CONFLICT_TTL,
        )
    except Exception:
        current_app.logger.warning(traceback.format_exc())
    return "conflict", None


def _sonarr_branch(series, entry):
    """Compare the Sonarr folder of a series with its Fitzflix folder.

    Return the branch and its value. "no files" and "equal" change
    nothing. "rename" gives the new title. "repoint" gives the Fitzflix
    folder. "conflict" changes nothing and writes one warning. The
    folders are compared after the Sonarr folder goes through the
    checks of the rename task."""

    sonarr_folder = sonarr_folder_of(entry)
    folders = fitzflix_folders(series)
    if not folders:
        return "no files", None

    # An import can attach an episode in the Sonarr folder before the
    # rename (#372). Then the series has the Sonarr folder and 1 other
    # folder. Only the rename branch can join them.

    split = len(folders) == 2 and sonarr_folder in folders and None not in folders
    if split:
        folders = folders - {sonarr_folder}
    elif len(folders) > 1 or None in folders:
        current_app.logger.warning(
            f"Sonarr path check: the files of {series.title!r} are in more "
            f"than one show folder. Not changed"
        )
        return _conflict(series)
    (folder,) = folders
    if folder == sonarr_folder:
        return "equal", folder
    if not sonarr_folder:
        current_app.logger.warning(
            f"Sonarr path check: Sonarr gives no folder for {series.title!r}. "
            f"Not changed"
        )
        return _conflict(series)

    # The check reads the folders under TV_LIBRARY, and a repoint keeps
    # the parent of the Sonarr path. Thus, a series on another Sonarr
    # root folder is a conflict. A repoint there names a missing folder.

    library = current_app.config["TV_LIBRARY"]
    parent = os.path.dirname(os.path.normpath(entry.get("path") or "/"))
    if os.path.realpath(parent) != os.path.realpath(library):
        current_app.logger.warning(
            f"Sonarr path check: Sonarr holds {series.title!r} under "
            f"{parent!r}, not under the TV library. Not changed"
        )
        return _conflict(series)
    fitzflix_exists = os.path.isdir(os.path.join(library, folder))
    sonarr_exists = os.path.isdir(os.path.join(library, sonarr_folder))
    if sonarr_exists and not fitzflix_exists:
        title = _rename_title(series, folder, sonarr_folder)
        return ("rename", title) if title else _conflict(series)
    if fitzflix_exists and not sonarr_exists and not split:
        return "repoint", folder
    current_app.logger.warning(
        f"Sonarr path check: {series.title!r} is at {sonarr_folder!r} in "
        f"Sonarr and at {folder!r} in Fitzflix. Not changed (Sonarr folder "
        f"exists: {sonarr_exists}, Fitzflix folder exists: {fitzflix_exists})"
    )
    return _conflict(series)


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


def _job_ids(queue):
    """Return the ids of the jobs of a queue that wait, run, or are scheduled."""

    from rq.registry import ScheduledJobRegistry, StartedJobRegistry

    ids = set(queue.job_ids)
    ids.update(queue.intermediate_queue.get_job_ids())
    ids.update(StartedJobRegistry(queue=queue).get_job_ids())
    ids.update(ScheduledJobRegistry(queue=queue).get_job_ids())
    return ids


def _job_pending(queue, *job_ids):
    """Return True if a job with one of these ids waits, runs, or is scheduled.

    An id with a time tag (the id, "_", and digits) counts as the id."""

    return any(
        found == job_id or re.fullmatch(re.escape(job_id) + r"_\d+", found)
        for found in _job_ids(queue)
        for job_id in job_ids
    )


def _series_of_pending_rename(title):
    """Return the series that a rename to this title waits for, or None (#372)."""

    from rq.job import Job

    from app.tasks import TASKS, resolve_queue

    queue = resolve_queue(TASKS[RENAME_TASK].queue)
    for job in Job.fetch_many(list(_job_ids(queue)), connection=queue.connection):
        if job is None or job.func_name != RENAME_TASK or len(job.args) < 2:
            continue
        if job.args[1] == title:
            series = db.session.get(TVSeries, job.args[0])
            if series is not None:
                return series
    return None


def _sonarr_series_list():
    """Return the Sonarr series list, or None after a fault. Read it 1 time per app context.

    The cataloging reads it before its records (#372 follow-up). Thus,
    a slow Sonarr does not hold the open write transaction."""

    from app.sonarr_push import _sonarr

    if "sonarr_series" not in g:
        try:
            g.sonarr_series = _sonarr("GET", "/api/v3/series") or []
        except Exception as e:
            current_app.logger.warning(
                f"Sonarr path check: couldn't list the Sonarr series: {e}"
            )
            g.sonarr_series = None
    return g.sonarr_series


def _series_of_sonarr_folder(folder):
    """Return (series, ok) for the series that Sonarr keeps in a show folder.

    The Sonarr series must match exactly 1 series by its ids. ok is
    False when Sonarr is configured but did not answer."""

    from app.sonarr_push import sonarr_configured

    if not folder or not sonarr_configured():
        return None, True
    entries = _sonarr_series_list()
    if entries is None:
        return None, False
    library = os.path.realpath(current_app.config["TV_LIBRARY"])
    found = set()
    for entry in entries:
        parent = os.path.dirname(os.path.normpath(entry.get("path") or "/"))
        if sonarr_folder_of(entry) == folder and os.path.realpath(parent) == library:
            series = find_series(entry)
            if series is not None:
                found.add(series)
    return (found.pop() if len(found) == 1 else None), True


def series_for_import(title, folder):
    """Return (series, ok) for the series that a new episode title belongs to (#372).

    The import calls this function before it makes a new series. A
    rename to the title can wait for the import. Then the episode goes
    to the series of that rename. Else Sonarr can keep the show folder
    of the file for a series. Sonarr renames a series folder, for
    example "Batman" to "Batman (1966)", and the TMDB first-air year
    cannot always confirm the year. The Sonarr ids can. ok is False
    when Sonarr did not answer."""

    series = _series_of_pending_rename(title)
    if series is not None:
        return series, True
    return _series_of_sonarr_folder(folder)


def _enqueue_once(name, job_id, *args, description, also=(), timeout=None):
    """Queue a task with a fixed job id, once. Return the job, or None.

    The function does nothing if the job, or a job with an id in also,
    waits or runs. The finished job of the id is deleted first. Else the
    new job keeps the expiry of the old hash and can vanish in the
    queue. A failed job stays in the failed jobs for the sweep. Then the
    new job gets the id with a time tag. A timeout replaces the timeout
    of the task."""

    from rq.exceptions import NoSuchJobError
    from rq.job import Job

    from app.tasks import TASKS, resolve_queue

    queue = resolve_queue(TASKS[name].queue)
    if _job_pending(queue, job_id, *also):
        return None
    try:
        old = Job.fetch(job_id, connection=queue.connection)
    except NoSuchJobError:
        old = None
    if old is not None and old.is_failed:
        job_id = f"{job_id}_{int(time.time())}"
    elif old is not None:
        delete_job(old)
    return enqueue_task(
        name, *args, description=description, job_id=job_id, timeout=timeout
    )


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


def _apply_branch(series, entry, branch, value):
    """Carry out a rename or a repoint. Return True if it was done or queued."""

    from app.tasks import requeue_job_id

    sonarr_folder = sonarr_folder_of(entry)
    _adopt_tvdb_id(series, entry)
    if branch == "rename":
        name = RENAME_TASK
        job = _enqueue_once(
            name,
            safe_job_id(f"sonarr-rename:{series.id}"),
            series.id,
            value,
            description=f"Renaming TV series {series.id} to '{value}' after Sonarr",
            also=(requeue_job_id(name, series.id),),
            timeout=rename_timeout(series),
        )
        current_app.logger.info(
            f"Sonarr path check: Sonarr moved {series.title!r} to "
            f"{sonarr_folder!r}. "
            + ("Renaming the series" if job else "A rename is already queued")
        )
        return job is not None
    try:
        done = _update_sonarr_path(series, sonarr_folder, value)
    except Exception as e:
        current_app.logger.warning(
            f"Sonarr path check: couldn't repoint {series.title!r} from "
            f"{sonarr_folder!r} to {value!r}: {e}"
        )
        return False
    current_app.logger.info(
        f"Sonarr path check: Sonarr points {series.title!r} at the missing "
        f"folder {sonarr_folder!r}. "
        + (f"Repointed at {value!r}" if done else "Sonarr did not take the path")
    )
    return done


def reconcile_sonarr_paths():
    """Carry each Sonarr series folder over to Fitzflix or back (#370).

    This is a task. It runs nightly. Sonarr sends no event for a folder
    move. A push to Sonarr after a Fitzflix rename is lost when Sonarr
    is down. This check finds both cases. It matches each Sonarr series
    to a TV series by TheTVDB id and compares the 2 folders.

    If only the Sonarr folder exists, Sonarr moved the series, and the
    rename task carries the new title over. If only the Fitzflix folder
    exists, Sonarr is repointed at it. If both or neither exist, the
    check writes a warning and changes nothing. A series that Sonarr
    does not manage is not checked. Return the summary counts."""

    from app.sonarr_push import _sonarr, sonarr_configured

    with app.app_context():
        if not sonarr_configured():
            return _summary(error="Sonarr is not configured")
        if _tv_library_missing():
            return _summary(error="TV library not mounted")
        try:
            entries = _sonarr("GET", "/api/v3/series") or []
        except Exception as e:
            current_app.logger.warning(f"Sonarr path check: couldn't list series: {e}")
            return _summary(error="Sonarr unreachable")

        lookup = _series_lookup()
        summary = _summary()
        fixes = []
        for entry in entries:
            series = _match_series(entry, lookup)
            if series is None:
                continue
            branch, value = _sonarr_branch(series, entry)
            if branch == "no files":
                continue
            summary["checked"] += 1
            if branch == "conflict":
                summary["conflicts"] += 1
            elif branch in ("rename", "repoint"):
                fixes.append((series, entry, branch, value))

        if len(fixes) > MAX_SONARR_FIXES:
            current_app.logger.warning(
                f"Sonarr path check: {len(fixes)} series have a different "
                f"folder, more than {MAX_SONARR_FIXES}. Nothing changed. Check "
                f"the Sonarr root folder and the library mount"
            )
            return _summary(**summary, over_limit=len(fixes))

        for series, entry, branch, value in fixes:
            if _apply_branch(series, entry, branch, value):
                summary["renamed" if branch == "rename" else "repointed"] += 1

        current_app.logger.info(
            f"Sonarr path check: {len(entries)} Sonarr series, "
            f"{summary['checked']} checked, {summary['renamed']} renamed, "
            f"{summary['repointed']} repointed, {summary['conflicts']} left "
            f"for review"
        )
        return summary


def _sonarr_entry(series):
    """Return the Sonarr series of a TV series, or None."""

    from app.sonarr_push import _sonarr

    path = "/api/v3/series"
    if series.tvdb_id:
        path += f"?tvdbId={int(series.tvdb_id)}"
    lookup = ({series.tvdb_id: series}, {series.tmdb_id: series})
    return next(
        (item for item in _sonarr("GET", path) or [] if _match_series(item, lookup)),
        None,
    )


def _row_for_previous_path(series, previous_path):
    """Return the File row of a series at a library path, or None."""

    from app.models import File

    if not previous_path:
        return None
    library_dir = current_app.config["LIBRARY_DIR"]
    relative = os.path.relpath(os.path.normpath(previous_path), library_dir)
    if relative.startswith(os.pardir):
        return None
    return File.query.filter_by(file_path=relative, series_id=series.id).first()


def _adoption_details(series, file, path):
    """Return the parsed details of a renamed episode file and a refusal.

    The refusal is None when the row can take the new path. The path
    must be a file under the TV library. Its name must parse as the same
    episode of the same series, with the same quality and screen format.
    The name must give the exact path. The version text can change. No
    other row can hold the path."""

    from app.importing import evaluate_filename
    from app.manual_imports import fitzflix_basename
    from app.models import File

    library = os.path.realpath(current_app.config["TV_LIBRARY"])
    real = os.path.realpath(path)
    if os.path.commonpath([real, library]) != library:
        return None, "it is outside the TV library"
    if not os.path.isfile(real):
        return None, "it does not exist"
    if not fitzflix_basename(os.path.basename(real)):
        return None, "its name is not in the Fitzflix form"
    relative = os.path.relpath(
        real, os.path.realpath(current_app.config["LIBRARY_DIR"])
    )
    details = evaluate_filename(real, log=False)
    if not details or details["file_path"] != relative:
        return None, "its name does not give this path"
    wanted = {
        "title": series.title,
        "season": file.season,
        "episode": file.episode,
        "last_episode": file.last_episode,
        "quality_title": file.quality.quality_title if file.quality else None,
        "fullscreen": True if file.fullscreen else None,
    }
    changed = sorted(key for key, value in wanted.items() if details[key] != value)
    if changed:
        return None, f"its name changes the {', '.join(changed)}"
    other = File.query.filter_by(file_path=relative).first()
    if other is not None and other.id != file.id:
        return None, "another record holds the path"
    return details, None


def _adopt_episode_paths(series, renamed_files):
    """Point the File rows at the episode files that Sonarr renamed.

    Nothing moves on the disk. Sonarr moved the files. The row takes the
    new path, name and Plex title, and the version text of the new name.
    The untouched name stays, and the archive key too. Each row changes
    under the title lock of its episode, as an import or an edit of the
    episode holds it. A file whose lock is held is skipped and logged.
    Return the number of adopted rows."""

    adopted = 0
    for item in renamed_files:
        previous, path = item.get("previousPath"), item.get("path")
        file = _row_for_previous_path(series, previous)
        if file is None or not path:
            current_app.logger.info(
                f"Sonarr rename: no record of {series.title!r} is at "
                f"{previous!r}. Skipped"
            )
            continue
        details, reason = _adoption_details(series, file, path)
        if reason is None:
            lock = current_app.lock_manager.lock(file.file_identifier(), 60000)
            reason = None if lock else "another task holds its title lock"
        if reason:
            current_app.logger.warning(
                f"Sonarr rename: {file.basename!r} not moved to {path!r}, "
                f"because {reason}"
            )
            continue
        old_basename = file.basename
        old_video = os.path.join(current_app.config["LIBRARY_DIR"], file.file_path)
        try:
            for key in ("dirname", "basename", "file_path", "plex_title", "edition"):
                setattr(file, key, details[key])
            db.session.commit()
            # Sonarr moved the video. The episode thumb follows (#469).
            move_episode_thumb(
                old_video,
                os.path.join(current_app.config["LIBRARY_DIR"], file.file_path),
            )
        finally:
            current_app.lock_manager.unlock(lock)
        adopted += 1
        current_app.logger.info(
            f"Sonarr rename: {old_basename!r} is now {file.basename!r}"
        )
    return adopted


def reconcile_sonarr_series(series_id, renamed_files=()):
    """Run the Sonarr path check for one series, after a Sonarr event (#370).

    This is a task. The webhook queues it. renamed_files holds the
    previousPath and path of each episode file of a Sonarr rename. The
    series branch runs first. The rows follow the renamed episode files
    only when the 2 folders are equal. A folder change goes through the
    rename task, and the rows then hold the new folder."""

    from app.sonarr_push import sonarr_configured

    with app.app_context():
        series = db.session.get(TVSeries, series_id)
        if series is None or not sonarr_configured():
            return _summary(error="no series or Sonarr is not configured")
        if _tv_library_missing():
            return _summary(error="TV library not mounted")
        try:
            entry = _sonarr_entry(series)
        except Exception as e:
            current_app.logger.warning(
                f"Sonarr path check: couldn't read {series.title!r}: {e}"
            )
            return _summary(error="Sonarr unreachable")
        if entry is None:
            current_app.logger.info(
                f"Sonarr path check: Sonarr does not manage {series.title!r}"
            )
            return _summary(error="not in Sonarr")

        branch, value = _sonarr_branch(series, entry)
        summary = _summary(checked=int(branch != "no files"), adopted=0)
        if branch == "conflict":
            summary["conflicts"] = 1
        elif branch in ("rename", "repoint"):
            if _apply_branch(series, entry, branch, value):
                summary["renamed" if branch == "rename" else "repointed"] = 1
        if renamed_files and branch == "equal":
            summary["adopted"] = _adopt_episode_paths(series, renamed_files)
        elif renamed_files:
            current_app.logger.info(
                f"Sonarr rename: the folder of {series.title!r} changes first. "
                f"The episode paths are not adopted"
            )
        return summary


def queue_series_reconcile(event, sonarr_series, renamed_files=None):
    """Queue the Sonarr path check of one series after a Sonarr event.

    The webhook calls this function before it acknowledges the event.
    The job goes on the queue when the 2 folders differ. It also goes on
    the queue when a renamed episode file was at the path of a record.
    The job id holds the series and the renamed files. Thus, a repeat of
    the event adds no second job. Return the job, or None."""

    from app import path_tag

    series = find_series(sonarr_series)
    if series is None:
        return None
    marker = SONARR_CONFLICT_KEY.format(series_id=series.id)
    if event == "Download" and current_app.redis.exists(marker):
        return None
    folder = sonarr_folder_of(sonarr_series)
    folders = fitzflix_folders(series)
    renamed = [
        {"previousPath": item.get("previousPath"), "path": item.get("path")}
        for item in renamed_files or []
        if isinstance(item, dict)
        and _row_for_previous_path(series, item.get("previousPath")) is not None
    ]
    if (not folders or folders == {folder}) and not renamed:
        return None

    key = f"sonarr-reconcile:{series.id}"
    if renamed:
        key += ":" + path_tag(repr(renamed))
    job = _enqueue_once(
        "app.series_rename.reconcile_sonarr_series",
        safe_job_id(key),
        series.id,
        renamed,
        description=f"Checking the Sonarr path of '{series.title}'",
    )
    current_app.logger.info(
        f"Sonarr {event}: {series.title!r} is at {folder!r} in Sonarr. "
        + ("Checking the series" if job else "A check is already queued")
    )
    return job
