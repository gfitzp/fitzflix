"""Run the TMDB refresh pair (the strangler split from app.videos).

refresh_tmdb_info fetches the canonical TMDB payload of a record on the
network queue. apply_tmdb_refresh applies the payload on the
single-worker sql queue. That step does the record updates, the file
renames, the duplicate merges, and the untouched-key handoff in S3.
find_or_create_tmdb_movie is the shared record-creation door. Every
review and watchlist surface goes through it.

app.videos re-exports every name here. Thus, the stored rq job strings
("app.videos.refresh_tmdb_info") and the import sites continue to
resolve. The filename plumbing lives in app.importing. This module
imports it lazily. Thus, the module import direction stays one-way.
"""

import gzip
import json
import os
import shutil
import traceback
import zlib

from datetime import datetime, timedelta, timezone

from flask import current_app
from markupsafe import escape
from rq.registry import ScheduledJobRegistry, StartedJobRegistry
from werkzeug.local import LocalProxy

from app.tasks import enqueue_task, requeue
from app import db, get_app, makedirs_on_mounted_volume
from app.aws_storage import request_archive_move
from app.custom_artwork import (
    is_library_poster,
    library_poster_original,
    move_movie_poster,
)
from app.email import notify_admin, notify_admin_text
from app.models import (
    MOVIES,
    TV_SHOWS,
    ArchivedOriginal,
    File,
    Movie,
    MovieAward,
    RefTMDBCertification,
    TMDBUnavailable,
    TVSeries,
    UserMovieReview,
    UserMovieStatus,
    UserWatchlist,
    clip_movie_title,
)
from app.tmdb_apply import tmdb_json_or
from app.naming import retag_quality, with_extension
from app.title_keys import title_key


def library_name_details(file, file_details):
    """Return the file details of the library name of a record.

    The rename after a TMDB refresh evaluates the untouched name. That
    is the name of the original. The library file can have a different
    name. A conversion (#292) has the quality of its 1080p library file,
    not the quality of its original. A remux to Matroska has the "mkv"
    extension, not the extension of the original. The library name
    keeps both."""

    details = dict(file_details)
    basename = details["basename"]
    if file.converted_from_quality and file.quality:
        basename = retag_quality(
            basename, file.converted_from_quality, file.quality.quality_title
        )
    extension = os.path.splitext(basename)[1]
    library_extension = os.path.splitext(file.basename or "")[1]
    if library_extension and extension != library_extension:
        basename = with_extension(basename, library_extension)
        details["extension"] = library_extension.lstrip(".")
    details["basename"] = basename
    details["file_path"] = os.path.join(details["dirname"], basename)
    return details


def move_movie_rows(old_movie_id, new_movie_id):
    """Move the per-movie rows of a merged movie record to the survivor.

    These are the watchlist rows, the status flags, the awards, the
    archive-only originals, the reviews, the DVR channel picks, and the
    custom poster. A row that the
    survivor already has stays behind. The delete of the
    old record removes it. Thus, no unique key fails."""

    keys = (
        (UserWatchlist, ("user_id",)),
        (UserMovieStatus, ("user_id", "kind")),
        (MovieAward, ("award_id", "win", "year")),
        (ArchivedOriginal, ("id",)),
        # A user can log a film many times. Each review moves.
        (UserMovieReview, ("id",)),
    )
    for model, fields in keys:
        kept = {
            tuple(getattr(row, field) for field in fields)
            for row in model.query.filter_by(movie_id=new_movie_id)
        }
        for row in model.query.filter_by(movie_id=old_movie_id).all():
            key = tuple(getattr(row, field) for field in fields)
            if key not in kept:
                row.movie_id = new_movie_id
                kept.add(key)

    # The DVR channel picks are link rows. The delete of the old record
    # removes them.

    old_movie = db.session.get(Movie, old_movie_id)
    new_movie = db.session.get(Movie, new_movie_id)
    if old_movie is not None and new_movie is not None:
        for channel in old_movie.dvr_channels.all():
            if channel.movies.filter(Movie.id == new_movie_id).first() is None:
                channel.movies.append(new_movie)

        # The custom poster goes to the survivor if it has none (#463).

        move_movie_poster(db.session, old_movie, new_movie)


def find_or_create_tmdb_movie(tmdb_id, film_title, year, details=None):
    """Return (movie, created), the record for a TMDB film.

    This function first uses an existing row with the tmdb id. Then it
    uses a record with the same canonical title and year, if that
    record has no tmdb id. Only then does it create a review-only
    record. If a different film has the title and the year, it returns
    (None, False). The movie can appear after the
    redirect check of the caller (an import or a concurrent log). The
    caller must commit the session. If the record is new, the caller
    must enqueue the standard TMDB refresh.

    The live TMDB payload of the caller (details) primes the display
    fields: title, date, overview, poster, and runtime. Thus, the movie
    page after the redirect is not bare while the queued refresh
    completes. tmdb_data_as_of stays unset until the full refresh
    stamps it.
    """

    film_title = clip_movie_title(film_title)
    movie = Movie.query.filter_by(tmdb_id=tmdb_id).first()
    if movie is None:
        movie = Movie.query.filter_by(title=film_title, year=year).first()
        if movie is not None and movie.tmdb_id is None:
            movie.tmdb_id = tmdb_id
        elif movie is not None:
            # A different film has this title and year. The title and the
            # year are unique. Thus, Fitzflix cannot hold this film. A
            # review or a watchlist row must not go to the wrong film.
            current_app.logger.warning(
                f"TMDB {tmdb_id} '{film_title} ({year})': {movie} has the same "
                f"title and year but TMDB {movie.tmdb_id}; not using it"
            )
            return None, False
    created = movie is None
    if created:
        movie = Movie(title=film_title, year=year, tmdb_id=tmdb_id)
        db.session.add(movie)
    if details and movie.tmdb_title is None:
        # The title and the date prime together. The display code treats
        # a set tmdb_title as a promise that the release date exists.
        try:
            release_date = datetime.strptime(
                details.get("release_date") or "", "%Y-%m-%d"
            )
        except ValueError:
            release_date = None
        if release_date is not None:
            title_length = Movie.__table__.c.tmdb_title.type.length
            movie.tmdb_title = (details.get("title") or "")[:title_length] or None
            movie.tmdb_release_date = release_date
        movie.tmdb_overview = movie.tmdb_overview or details.get("overview")
        movie.tmdb_poster_path = movie.tmdb_poster_path or details.get("poster_path")
        movie.tmdb_runtime = movie.tmdb_runtime or details.get("runtime")
    if created:
        db.session.flush()
    return movie, created


def _movie_refresh_lock_resources(*movies):
    """Return every title-lock resource that an import of these movies can hold.

    The list covers the identifier of each existing file. It also covers
    the base main-feature identifier of each movie. Thus, a new first
    file of the title that arrives during the refresh is also
    serialized. The list is sorted. Thus, 2 refreshes that take locks
    for overlapping movies cannot deadlock each other.
    """

    resources = set()
    for movie in movies:
        if movie is None:
            continue
        resources.add(
            json.dumps(
                {
                    "title": movie.title,
                    "year": movie.year,
                    "feature_type": None,
                    "plex_title": f"{movie.title} ({movie.year})",
                    "edition": None,
                }
            )
        )
        for file in movie.files.all():
            resources.add(file.file_identifier())
    return sorted(resources)


REFRESH_FUNC = "app.videos.refresh_tmdb_info"
APPLY_FUNC = "app.videos.apply_tmdb_refresh"


def _refresh_job_target(job):
    """Return the (library, id) that a refresh or apply job works on.

    The fetch job takes positional arguments. The apply job and its
    retries take keyword arguments. Both shapes are read here."""

    if job is None or job.func_name not in (REFRESH_FUNC, APPLY_FUNC):
        return None
    if len(job.args) >= 2:
        return (job.args[0], job.args[1])
    kwargs = job.kwargs or {}
    if "library" in kwargs and "id" in kwargs:
        return (kwargs["library"], kwargs["id"])
    return None


def tmdb_refresh_status(library, id):
    """Return the state of a TMDB refresh of this record, or None.

    The refresh is 2 jobs. The fetch runs on the request queue. It hands
    its payload to the apply on the sql queue. The apply takes the title
    lock. When the lock is busy, the apply puts a retry in the scheduled
    registry of the sql queue. This function reads all of these places.

    The result is a dict with a "state" key. The state is "deferred" when
    a retry waits for its time. Then "retry_at" gives that time as a
    naive UTC datetime. The state is "pending" when a fetch or an apply
    is queued or running. The result is None when no refresh is on its
    way. A deferred retry outranks a queued job, because it names the
    reason that the page still shows the old data.

    A task that also takes the title lock calls this function first. The
    Atmos supplement is one. On 2026-10-04, that task took the lock of
    'Jungle Cruise (2021)' 15 ms after the import released it. The
    refresh then found the lock busy and waited for the whole
    MediaConvert run. The match from the review page waited with it.
    """

    target = (library, int(id))

    def _matches(job):
        found = _refresh_job_target(job)
        return found is not None and (found[0], int(found[1])) == target

    from app.queue_page import scheduled_entries

    # The ids and the times come from 1 read (#511)
    scheduled = dict(
        scheduled_entries(ScheduledJobRegistry(queue=current_app.sql_queue))
    )
    for job in current_app.sql_queue.job_class.fetch_many(
        list(scheduled), connection=current_app.sql_queue.connection
    ):
        if job is not None and _matches(job):
            retry_at = scheduled[job.id]
            return {
                "state": "deferred",
                "retry_at": retry_at.astimezone(timezone.utc).replace(tzinfo=None),
            }

    for queue in (current_app.request_queue, current_app.sql_queue):
        candidates = list(queue.jobs)
        candidates.extend(
            queue.job_class.fetch_many(
                StartedJobRegistry(queue=queue).get_job_ids(),
                connection=queue.connection,
            )
        )
        if any(_matches(job) for job in candidates):
            return {"state": "pending"}
    return None


def tmdb_refresh_pending(library, id):
    """Return True while a TMDB refresh of this record is queued or running.

    This covers a deferred retry too. See tmdb_refresh_status."""

    return tmdb_refresh_status(library, id) is not None


# refresh_tmdb_info returns this when TMDB sent nothing for the id. The
# apply still runs. It handles a missing match.

TMDB_NO_MATCH = "no-match"


def _refresh_target(library, id, ignored="skipping TMDB refresh", rematch=False):
    """Return (record, description) of a TMDB refresh, or None.

    Both phases of a refresh find their record here. The result is None
    for a record that no longer exists. For example, an earlier job of a
    bulk refresh merged it into a different record. It is None
    for a record that is detached from TMDB on purpose. A fetch for it
    would search by title and attach a wrong id. ignored is the end of
    that log line. A series that shares its TMDB id with a canonical
    record refreshes that record. description names the job. rematch
    accepts a detached record: an admin entered an id by hand (#514)."""

    if library == MOVIES:
        movie = Movie.query.filter_by(id=id).first()
        if movie is None:
            current_app.logger.warning(
                f"Movie id {id} no longer exists, skipping TMDB refresh"
            )
            return None
        if movie.tmdb_ignored and not rematch:
            current_app.logger.info(
                f"{movie} is marked as having no TMDB match, {ignored}"
            )
            return None
        return movie, f"Updating '{movie.title} ({movie.year})' with TMDB data"

    if library == TV_SHOWS:
        tv_show = TVSeries.query.filter_by(id=id).first()
        if tv_show is None:
            current_app.logger.warning(
                f"TV series id {id} no longer exists, skipping TMDB refresh"
            )
            return None
        if tv_show.tmdb_ignored and not rematch:
            current_app.logger.info(
                f"{tv_show} is marked as having no TMDB match, {ignored}"
            )
            return None
        if tv_show.tmdb_id != None:
            existing_series = TVSeries.query.filter_by(tmdb_id=tv_show.tmdb_id).first()
            if existing_series:
                tv_show = existing_series
        return tv_show, f"Updating '{tv_show.title}' with TMDB data"

    return None


def refresh_tmdb_info(
    library,
    id,
    tmdb_id=None,
    notify_if_missing=False,
    apply_at_front=False,
    rematch=False,
):
    """Run the network phase of a TMDB refresh.

    This phase queries TMDB. Then it gives the payload to
    apply_tmdb_refresh on the sql queue.

    This phase runs on the user-request queue. There, several jobs can
    run at the same time. This is safe, because the phase writes nothing
    to the database. Every database and library-file change occurs in
    apply_tmdb_refresh. The single sql worker serializes those changes.

    Return True when TMDB sent data, TMDB_NO_MATCH when it sent nothing,
    and False on a failure. A page that waits for the refresh puts the
    apply at the front of the sql queue (apply_at_front). rematch is an
    id that an admin entered by hand. It fetches a detached record. The
    apply clears the detachment only when a payload applies (#514).
    """

    with app.app_context():
        try:

            target = _refresh_target(library, id, rematch=rematch)
            if target is None:
                return False
            record, description = target
            if library == MOVIES:
                tmdb_info = record.tmdb_movie_fetch(tmdb_id)
            else:
                tmdb_info = record.tmdb_tv_fetch(tmdb_id)

            # Compress the payload for its trip through Redis. A details
            # response is small. But a bulk refresh can have thousands of
            # them queued at the same time.

            tmdb_payload = None
            if tmdb_info:
                tmdb_payload = zlib.compress(json.dumps(tmdb_info).encode("utf-8"))

            enqueue_task(
                "app.videos.apply_tmdb_refresh",
                library=library,
                id=id,
                tmdb_id=tmdb_id,
                tmdb_payload=tmdb_payload,
                notify_if_missing=notify_if_missing,
                rematch=rematch,
                description=description,
                at_front=apply_at_front,
            )

        except TMDBUnavailable:
            # The fetch logged a warning. A TMDB outage is not an error of
            # Fitzflix, and a nightly refresh must not send 1 email per film

            return False

        except Exception:
            current_app.logger.error(traceback.format_exc())
            return False

        else:
            return True if tmdb_payload else TMDB_NO_MATCH


def save_failed_payload(library, id, tmdb_payload):
    """Write a payload whose apply raised to a file next to the log.

    Then a transient upstream glitch can be examined later.

    The 2026-08-22 overnight TV refresh failed on 14 series. TMDB served
    malformed aggregate credits for some seconds. When somebody looked,
    the live payloads were clean again and the bad shape was gone. The
    apply had logged only the traceback. The dump is named under the log
    file (LOG_FILE.tmdb-payload.<library>-<id>.<stamp>.json.gz). Thus,
    the retention glob of rotate_logs prunes it with the archives. This
    function returns the path, or None when there was nothing to save.
    """

    if not tmdb_payload:
        return None

    slug = "tv" if library == TV_SHOWS else "movie"
    stamp = datetime.now().strftime("%Y-%m-%dT%H%M%S")
    path = f"{current_app.config['LOG_FILE']}.tmdb-payload.{slug}-{id}.{stamp}.json.gz"
    try:
        with gzip.open(path, "wb") as target:
            target.write(zlib.decompress(tmdb_payload))
    except Exception:
        current_app.logger.warning(
            f"Could not save the failed TMDB payload to {path}: "
            f"{traceback.format_exc()}"
        )
        return None
    return path


def _movie_folder(files):
    """Return the folder name of a movie from its file rows.

    A dirname is "Movies/<folder>" or "Movies/<folder>/<feature type>".
    Thus, the second component is the folder. A film can own a plain
    folder and edition folders (Brazil, 1985). Radarr downloads go to
    the plain folder. Thus, the plain folder of a main feature wins.
    Next is the folder of an edition main feature. A folder that holds
    only special features is last. Radarr finds no movie file there.
    The rows come in a stable order, so the choice is the same on each
    call."""

    candidates = []
    for f in files:
        parts = os.path.normpath(f.dirname or "").split(os.sep)
        if len(parts) < 2:
            continue
        folder = parts[1]
        candidates.append(
            (f.feature_type_id is not None, "{edition-" in folder, folder)
        )
    if not candidates:
        return None
    return min(candidates)[2]


def _follow_rename_in_radarr(old_tmdb_id, new_tmdb_id, new_folder):
    """Queue the report of a movie rename to Radarr.

    The report runs as its own job on the request queue. Thus, the
    refresh on the sql queue never waits on Radarr (#268). The job
    withdraws the entry of an old TMDB id and points the entry of the
    new id at the new folder."""

    from app.radarr_push import enqueue_radarr_push, radarr_configured

    if not radarr_configured():
        return
    enqueue_radarr_push(
        "app.radarr_push.follow_rename_task",
        old_tmdb_id,
        new_tmdb_id,
        new_folder,
        description=f"Reporting a rename to Radarr (tmdb {new_tmdb_id})",
    )


# _rename_movie_file gives this when the library volume does not answer.
# The apply then books itself again (follow-up #551). Before, the folder
# kept the old title until TMDB changed the film again.

VOLUME_DOWN = "volume-down"
MAX_RENAME_VOLUME_WAITS = 72


def _rename_movie_file(f, tmdb_id, probe=True):
    """Move 1 library file of a refreshed movie to the name of its new details.

    The file keeps its place if its untouched name no longer evaluates,
    if the new path is claimed, or if the move fails. The poster assets
    of a main feature go with it. Return True if the file moved on
    disk. The row commits right after the move. If that commit fails,
    the file goes back to its old path (#380). With probe False, the
    caller probed the volumes already (#575)."""

    # The filename plumbing lives in app.importing. The import is lazy.
    # Thus, the module import direction stays one-way.

    from app.importing import evaluate_filename

    # The import lines of evaluate_filename (File, Movie, Existing movie)
    # do not belong to a rename. A bulk refresh wrote them for each file
    # (#590). The warning below names the reason for a reject.

    renamed = False
    if tmdb_id != None:
        file_details = evaluate_filename(
            f.untouched_basename, tmdb_id=tmdb_id, log=False
        )
    else:
        file_details = evaluate_filename(f.untouched_basename, log=False)

    if not file_details:
        # For example, an id tag in the untouched name no
        # longer resolves. Leave the file where it is.
        detail = getattr(file_details, "detail", None)
        reason = f" ({detail})" if detail else ""
        current_app.logger.warning(
            f"'{f.untouched_basename}' no longer evaluates{reason}, "
            f"skipping its rename"
        )
        return False
    file_details = library_name_details(f, file_details)

    new_relative = file_details.get("file_path")
    old_file = os.path.join(current_app.config["LIBRARY_DIR"], f.file_path)
    old_directory = os.path.dirname(old_file)
    new_file = os.path.join(current_app.config["LIBRARY_DIR"], new_relative)
    new_directory = os.path.dirname(new_file)

    # A dropped share leaves its mountpoint as a plain folder on the boot
    # disk. Then neither path exists, and the row would move with no file
    # (#551). Thus, the rename waits for the volume. The probe comes
    # before the collision check (#575). A hung share blocks the stat of
    # that check. A row with no local copy on a live volume still gets
    # its new name.

    from app.videos import _dead_volumes

    if probe and _dead_volumes([new_directory, old_directory]):
        current_app.logger.warning(
            f"'{f.basename}' (file #{f.id}) not renamed to '{new_relative}': "
            f"the library volume does not answer"
        )
        return VOLUME_DOWN

    # A merge can put this rename on a path that the target
    # movie already owns (the 25 Cats incident: os.rename
    # silently overwrote the file of the sibling, then the
    # path UPDATE died on the unique index). Refuse loudly
    # and leave both records untouched. The admin deletes
    # one on purpose instead. One shape is benign: the old
    # file is gone, the new file is already in place, and
    # there is no sibling row. That shape falls through.
    # Thus, an interrupted rename can heal its record.

    sibling = (
        File.query.filter(File.file_path == new_relative)
        .filter(File.id != f.id)
        .first()
    )
    collision = sibling is not None or (
        new_file != old_file and os.path.exists(new_file) and os.path.exists(old_file)
    )
    if collision:
        detail = (
            f"file #{sibling.id} already claims that path"
            if sibling
            else "a file already exists at that path"
        )
        # The notification is the alert. An ERROR record would mail the
        # admin a second time (#453).
        current_app.logger.warning(
            f"'{f.basename}' (file #{f.id}) not renamed to "
            f"'{new_relative}': {detail}. Delete one copy, "
            f"then re-assign the TMDB id."
        )
        notify_admin_text(
            "Rename collision needs triage",
            f"Renaming '{f.basename}' (file #{f.id}) to "
            f"'{new_relative}' was refused: {detail}.\n\n"
            f"Delete one of the copies, then re-assign "
            f"the TMDB id to finish the rename.",
            f"<p>Renaming '{escape(f.basename)}' (file #{f.id}) "
            f"to '{escape(new_relative)}' was refused: {escape(detail)}."
            f"</p><p>Delete one of the copies, then "
            f"re-assign the TMDB id to finish the "
            f"rename.</p>",
        )
        return False

    try:
        makedirs_on_mounted_volume(new_directory)
    except OSError as e:
        current_app.logger.warning(
            f"'{f.basename}' (file #{f.id}) not renamed to '{new_relative}': "
            f"the library volume is not available ({e})"
        )
        return VOLUME_DOWN

    # Database first, disk second. The path update flushes
    # inside a savepoint. Thus, a unique-index conflict
    # shows BEFORE the file moves, and a failed move rolls
    # the record back immediately.

    try:
        with db.session.begin_nested():
            f.file_path = new_relative
            f.dirname = file_details.get("dirname")
            f.basename = file_details.get("basename")
            f.plex_title = file_details.get("plex_title")
            db.session.flush()

            if old_file != new_file and os.path.exists(old_file):
                current_app.logger.info(f"Renaming '{old_file}' to '{new_file}'")
                os.rename(old_file, new_file)
                renamed = True
    except Exception:
        current_app.logger.error(traceback.format_exc())
        return False

    # The path fields were updated inside the savepoint, before the
    # physical rename. A failed commit rolls them back. Then the file
    # must go back too, or the row names a path with no file.

    try:
        db.session.commit()
    except Exception:
        current_app.logger.error(traceback.format_exc())
        db.session.rollback()
        if renamed:
            try:
                os.rename(new_file, old_file)
                current_app.logger.warning(
                    f"'{new_file}' moved back to '{old_file}', because its "
                    f"record did not commit"
                )
            except OSError:
                current_app.logger.error(traceback.format_exc())
        return False

    # Move or delete the old local assets. The poster art of a folder
    # that already holds poster art stays (#475). A merge must not put
    # the poster of the merged record over the poster of the survivor.
    new_directory = os.path.join(
        current_app.config["LIBRARY_DIR"],
        file_details.get("dirname"),
    )
    try:
        old_assets = os.listdir(old_directory)
        target_has_poster = new_directory != old_directory and any(
            is_library_poster(name) for name in os.listdir(new_directory)
        )
        for old_asset in old_assets:
            if (
                old_asset.startswith(("cover", "default", "movie", "poster"))
                and old_asset.endswith(("jpg", "jpeg", "png", "tbn"))
                and f.feature_type_id is None
                and not target_has_poster
                and os.path.join(old_directory, old_asset)
                != os.path.join(new_directory, old_asset)
                and os.path.isfile(os.path.join(old_directory, old_asset))
            ):
                current_app.logger.info(
                    f"Renaming '{os.path.join(old_directory, old_asset)}' to '{os.path.join(new_directory, old_asset)}'"
                )
                os.rename(
                    os.path.join(old_directory, old_asset),
                    os.path.join(new_directory, old_asset),
                )

            elif old_asset == "@eaDir":
                current_app.logger.info(
                    f"Deleting '{os.path.join(old_directory, old_asset)}'"
                )
                shutil.rmtree(
                    os.path.join(old_directory, old_asset),
                    ignore_errors=True,
                )

    except FileNotFoundError:
        pass

    # A custom poster decides the library copy of a main feature that
    # moved. The poster of the file outranks the poster of the movie.

    if f.feature_type_id is None and new_directory != old_directory:
        original = library_poster_original(f)
        if original is not None and os.path.isdir(new_directory):
            from app.main.posters import replace_library_poster

            try:
                replace_library_poster(new_directory, *original)
            except OSError:
                current_app.logger.warning(traceback.format_exc())

    # Clear the old directory tree. This clear knows junk.
    # It does not only clear an empty directory. The poster
    # assets moved above. But OS metadata or a leftover
    # image kept the empty shell alive for the weekly sweep
    # before.

    from app.maintenance import clear_leftover_directory

    clear_leftover_directory(old_directory)
    return renamed


def _requeue_for_the_volume(movie_id, tmdb_id, tmdb_payload, description, waits):
    """Book the apply again for a file that the dead volume kept from its rename.

    The retry renames the files that kept their old names. It stops
    after MAX_RENAME_VOLUME_WAITS (6 hours)."""

    if waits >= MAX_RENAME_VOLUME_WAITS:
        current_app.logger.error(
            f"Movie id {movie_id}: the library volume was not available for "
            f"the rename after {waits} waits. Refresh the TMDB data again later"
        )
        return
    try:
        requeue(
            "app.videos.apply_tmdb_refresh",
            key=f"{MOVIES}:{movie_id}:volume",
            delay=timedelta(minutes=5),
            reason="The library volume is not available for the rename",
            counters={"volume_waits": waits + 1},
            kwargs={
                "library": MOVIES,
                "id": movie_id,
                "tmdb_id": tmdb_id,
                "tmdb_payload": tmdb_payload,
            },
            description=description,
        )
    except Exception:
        current_app.logger.error(traceback.format_exc())


def _apply_movie_refresh(
    movie,
    description,
    tmdb_id,
    tmdb_info,
    tmdb_payload,
    notify_if_missing,
    locks,
    volume_waits=0,
    rematch=False,
):
    """Apply a movie payload: merge, rename the files, and move the rows.

    The lock-busy retry carries rematch and volume_waits (#560). Without
    rematch, the retry discards the payload of a detached record.
    locks collects the title locks that the driver releases. Return
    False if the apply must stop. That is a busy lock (the apply is
    booked again) or a commit that failed (#380). Return True
    otherwise. The driver commits the rest."""

    from app.importing import reconstruct_filename

    # Keep the original movie_id field, and the TMDB id
    # before the apply. Radarr keys its entries by TMDB id.

    original_movie_id = movie.id
    original_tmdb_id = movie.tmdb_id

    # Check if the requested tmdb_id already exists in the Movie
    # table. If it does, use that existing Movie record.

    existing_movie = None
    if tmdb_id != None:
        existing_movie = (
            Movie.query.filter_by(tmdb_id=tmdb_id)
            .order_by(Movie.date_created.asc())
            .first()
        )

    # This task rewrites file paths. When the TMDB id shows a
    # duplicate, it merges 2 movie records. Thus, it must not
    # interleave with a localization chain that holds a lock
    # on one of these titles. Take every lock that an import
    # of one of the 2 movies can hold. Take them in sorted
    # order, so concurrent refreshes cannot deadlock. If a
    # lock is busy, retry later.

    for resource in _movie_refresh_lock_resources(movie, existing_movie):
        lock = current_app.lock_manager.lock(
            resource, current_app.config["SQL_TASK_TIMEOUT"] * 1000
        )
        if not lock:
            for held in locks:
                current_app.lock_manager.unlock(held)
            locks.clear()
            requeue(
                "app.videos.apply_tmdb_refresh",
                key=f"{MOVIES}:{original_movie_id}",
                delay=(5, 15),
                reason="A file is locked by another task",
                kwargs={
                    "library": MOVIES,
                    "id": original_movie_id,
                    "tmdb_id": tmdb_id,
                    "tmdb_payload": tmdb_payload,
                    "notify_if_missing": notify_if_missing,
                    "rematch": rematch,
                    "volume_waits": volume_waits,
                },
                description=description,
            )
            return False
        locks.append(lock)

    if existing_movie:
        movie = existing_movie
        existing_movie.tmdb_movie_apply(tmdb_info)
        db.session.commit()
    else:
        movie.tmdb_movie_apply(tmdb_info)

    if notify_if_missing and movie.tmdb_id == None:
        notify_admin("Added a movie without a TMDB ID", "no_tmdb_id", movie=movie)

    # Keep the updated movie_id field.

    updated_movie_id = movie.id

    # Move the files to the new movie record.

    old_files = File.query.filter_by(movie_id=original_movie_id).all()

    for old_record in old_files:
        old_record.movie_id = updated_movie_id

    # The merge below deletes the old record, and the files of
    # a record go with it. Thus, stop if the files did not move.
    # This commit also holds the new TMDB details of the record. The
    # names below come from them. Thus, a failed commit always stops
    # the apply. The next refresh tries again.

    try:
        db.session.commit()

    except Exception:
        current_app.logger.error(traceback.format_exc())
        db.session.rollback()
        return False

    # Reconstruct the untouched filenames with the new movie details.
    # A bulk refresh applies hundreds of movies a night. Most names do
    # not change. Thus, a name gets a log line only when it changes, and
    # each apply writes 1 summary line (#590).

    files = File.query.filter_by(movie_id=updated_movie_id).all()
    changed = 0

    for f in files:
        untouched_basename = reconstruct_filename(f.id)
        if untouched_basename != f.untouched_basename:
            current_app.logger.info(
                f"New untouched basename: '{f.untouched_basename}' "
                f"to '{untouched_basename}'"
            )
            changed += 1
        f.untouched_basename = untouched_basename
    current_app.logger.info(
        f"{movie} refreshed: {changed} of {len(files)} names changed"
    )

    # Commit the new basenames BEFORE you queue an archive
    # move. A deferred re-archive reads the key that the record
    # wants from its own session. Thus, it must not start while
    # that key is only in this transaction.

    try:
        db.session.commit()

    except Exception:
        current_app.logger.error(traceback.format_exc())
        db.session.rollback()
        return False

    # Create new directories and move the files if necessary.

    files = File.query.filter_by(movie_id=updated_movie_id).all()
    renamed = False

    # 1 volume probe for the apply, not 1 for each file while the title
    # locks are held (#575). The files of a movie share the library mount.

    from app.videos import _dead_volumes

    volume_down = bool(files) and bool(
        _dead_volumes(
            [current_app.config["MOVIE_LIBRARY"]]
            + [
                os.path.join(current_app.config["LIBRARY_DIR"], f.file_path)
                for f in files
            ]
        )
    )
    for f in [] if volume_down else files:
        outcome = _rename_movie_file(f, tmdb_id, probe=False)
        if outcome == VOLUME_DOWN:
            volume_down = True
        else:
            renamed = outcome or renamed

    if volume_down:
        _requeue_for_the_volume(
            updated_movie_id, tmdb_id, tmdb_payload, description, volume_waits
        )

    # Request the archive moves LAST, after the path commit above. The
    # move job does not need the library file. It copies the S3 object
    # to the key of the new untouched name (#369). The jobs go to the
    # queue at the commit below.

    for f in files:
        try:
            request_archive_move(f)
        except Exception:
            current_app.logger.error(traceback.format_exc())

    try:
        db.session.commit()

    except Exception:
        current_app.logger.error(traceback.format_exc())
        db.session.rollback()

    # Radarr shares the library root. Thus, it must learn the
    # new folder, or it downloads the film again.

    if renamed or original_tmdb_id != movie.tmdb_id:
        _follow_rename_in_radarr(original_tmdb_id, movie.tmdb_id, _movie_folder(files))

    if updated_movie_id != original_movie_id:

        # The reviews, the watchlist rows, the status flags, and the
        # awards move to the new movie. The delete of the old record
        # would delete them with it.

        move_movie_rows(original_movie_id, movie.id)
        movie.date_updated = datetime.now(timezone.utc)
        db.session.flush()

        # Delete the old movie record from the database.

        original_movie_record = Movie.query.filter_by(id=original_movie_id).first()
        db.session.delete(original_movie_record)

    return True


def apply_tmdb_refresh(
    library,
    id,
    tmdb_id=None,
    tmdb_payload=None,
    notify_if_missing=False,
    rematch=False,
    volume_waits=0,
):
    """Run the database phase of a TMDB refresh.

    This phase applies a payload that refresh_tmdb_info fetched. It
    rewrites the file paths and merges duplicate records.

    This phase runs on the single-worker sql queue. Thus, the refreshes
    are serialized against each other and against all other database
    writes. A movie refresh also holds the locks of the affected titles
    for its duration. Thus, it cannot interleave with an import of the
    same title. With notify_if_missing (used for new imports), Fitzflix
    sends an email if the movie still has no TMDB match after the
    payload is applied. A payload whose apply raises goes to a file
    (save_failed_payload). With rematch, a payload clears the
    detachment of the record. No payload leaves the record detached
    (#514).
    """

    with app.app_context():
        locks = []
        try:
            tmdb_info = None
            if tmdb_payload:
                tmdb_info = json.loads(zlib.decompress(tmdb_payload).decode("utf-8"))

            # The record can be detached from TMDB after the payload fetch.

            target = _refresh_target(
                library,
                id,
                ignored="discarding the fetched TMDB payload",
                rematch=rematch and tmdb_info is not None,
            )
            if target is None:
                return False
            record, description = target
            if rematch and tmdb_info is not None:
                record.tmdb_ignored = False

            if library == MOVIES:
                if not _apply_movie_refresh(
                    record,
                    description,
                    tmdb_id,
                    tmdb_info,
                    tmdb_payload,
                    notify_if_missing,
                    locks,
                    volume_waits,
                    rematch=rematch,
                ):
                    return False
            else:
                record.tmdb_tv_apply(tmdb_info)

            db.session.commit()

        except Exception:
            saved = save_failed_payload(library, id, tmdb_payload)
            current_app.logger.error(
                traceback.format_exc()
                + (
                    f"TMDB payload that failed to apply saved to {saved}"
                    if saved
                    else ""
                )
            )
            db.session.rollback()
            return False

        else:
            return True

        finally:
            for held in locks:
                current_app.lock_manager.unlock(held)


def sync_certification_list():
    """Add the official TMDB certifications that the lookup table lacks.

    tmdb_movie_apply stores a certification only when the table has its
    row. The table was a 1-time seed. It had 142 of the 316 official
    values, and US "NR" was not 1 of them. Thus, the most common US
    rating of the old films never showed. This weekly task adds the new
    values and updates the meaning and the order. It deletes no row,
    because movie rows link to them. Return True when TMDB answered."""

    with app.app_context():
        body = tmdb_json_or("/certification/movie/list", None, None, timeout=30)
        if body is None:
            return False
        listing = body.get("certifications") or {}

        # The unique key compares without case or accents in MySQL. Thus,
        # the lookup folds the value the same way, and a near-duplicate
        # updates the stored row instead of failing the insert.

        def folded(country, value):
            """Return the key of a row as the MySQL collation compares it."""

            return country, title_key(value)

        stored = {
            folded(row.country, row.certification): row
            for row in RefTMDBCertification.query.all()
        }
        added = 0
        for country, entries in listing.items():
            for entry in entries or []:
                value = (entry.get("certification") or "").strip()
                if not value:
                    continue
                row = stored.get(folded(country, value))
                if row is None:
                    row = RefTMDBCertification(country=country, certification=value)
                    db.session.add(row)
                    stored[folded(country, value)] = row
                    added += 1
                row.meaning = entry.get("meaning")
                row.order = entry.get("order")
        db.session.commit()
        current_app.logger.info(
            f"TMDB certifications: {added} added, {len(stored)} in the table"
        )
        return True


def refresh_in_production_tv():
    """Enqueue the standard TMDB refresh again for every series in production.

    This is the nightly sweep. Thus, the new episodes and the season
    counts stay current without a manual bulk refresh.

    Ended and canceled series change rarely. The refresh-on-import
    trigger and the bulk refresh of the maintenance page cover them. A
    NULL status counts as in production. It only means that the series
    was not refreshed since before Fitzflix stored statuses.
    """

    with app.app_context():
        series = (
            TVSeries.query.filter(TVSeries.tmdb_id != None)
            .filter(TVSeries.tmdb_ignored == False)
            .filter(
                db.or_(
                    TVSeries.tmdb_in_production == True,
                    TVSeries.tmdb_status == None,
                    TVSeries.tmdb_status.notin_(["Ended", "Canceled"]),
                )
            )
            .order_by(TVSeries.title.asc())
            .all()
        )
        for tv in series:
            enqueue_task(
                "app.videos.refresh_tmdb_info",
                "TV Shows",
                tv.id,
                tv.tmdb_id,
                description=f"Refreshing TMDB data for '{tv.title}'",
            )

        current_app.logger.info(
            f"Queued TMDB refreshes for {len(series)} in-production TV series"
        )
        return len(series)


# The app instance of this process. It resolves lazily. Thus, an import of
# this module from a process that already has an application does not
# build a second one.

app = LocalProxy(get_app)
