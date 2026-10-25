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

from datetime import datetime, timezone

from flask import current_app, render_template
from rq.registry import ScheduledJobRegistry, StartedJobRegistry
from unidecode import unidecode
from werkzeug.local import LocalProxy

from app.tasks import enqueue_task, requeue
from app import db, get_app
from app.aws_storage import rename_untouched_object, sanitize_s3_key
from app.email import send_email as send_email_async
from app.models import (
    ArchivedOriginal,
    File,
    Movie,
    MovieAward,
    RefTMDBCertification,
    TMDBUnavailable,
    TVSeries,
    User,
    UserMovieReview,
    UserMovieStatus,
    UserWatchlist,
    tmdb_get,
)


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
        basename = basename.replace(
            f"[{file.converted_from_quality}]", f"[{file.quality.quality_title}]"
        )
    stem, extension = os.path.splitext(basename)
    library_extension = os.path.splitext(file.basename or "")[1]
    if library_extension and extension != library_extension:
        basename = f"{stem}{library_extension}"
        details["extension"] = library_extension.lstrip(".")
    details["basename"] = basename
    details["file_path"] = os.path.join(details["dirname"], basename)
    return details


def move_movie_rows(old_movie_id, new_movie_id):
    """Move the per-movie rows of a merged movie record to the survivor.

    These are the watchlist rows, the status flags, the awards, the
    archive-only originals, and the DVR channel picks. A row that the
    survivor already has stays behind. The delete of the
    old record removes it. Thus, no unique key fails."""

    keys = (
        (UserWatchlist, ("user_id",)),
        (UserMovieStatus, ("user_id", "kind")),
        (MovieAward, ("award_id", "win", "year")),
        (ArchivedOriginal, ("id",)),
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
            movie.tmdb_title = details.get("title")
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

    scheduled = ScheduledJobRegistry(queue=current_app.sql_queue)
    scheduled_ids = scheduled.get_job_ids()
    for job in current_app.sql_queue.job_class.fetch_many(
        scheduled_ids, connection=current_app.sql_queue.connection
    ):
        if _matches(job):
            retry_at = scheduled.get_scheduled_time(job.id)
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


def refresh_tmdb_info(
    library, id, tmdb_id=None, notify_if_missing=False, apply_at_front=False
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
    apply at the front of the sql queue (apply_at_front).
    """

    with app.app_context():
        try:

            if library == "Movies":
                movie = Movie.query.filter_by(id=id).first()
                if movie is None:
                    # For example, an earlier job in a bulk refresh merged
                    # it into a different record.
                    current_app.logger.warning(
                        f"Movie id {id} no longer exists, skipping TMDB refresh"
                    )
                    return False
                if movie.tmdb_ignored:
                    # The record is detached from TMDB on purpose. A fetch
                    # here would search by title and attach a wrong id.
                    current_app.logger.info(
                        f"{movie} is marked as having no TMDB match, "
                        f"skipping TMDB refresh"
                    )
                    return False
                description = f"Updating '{movie.title} ({movie.year})' with TMDB data"
                current_app.logger.info(f"tmdb_id: {tmdb_id}")
                tmdb_info = movie.tmdb_movie_fetch(tmdb_id)

            elif library == "TV Shows":
                tv_show = TVSeries.query.filter_by(id=id).first()
                if tv_show is None:
                    current_app.logger.warning(
                        f"TV series id {id} no longer exists, skipping TMDB refresh"
                    )
                    return False

                if tv_show.tmdb_ignored:
                    current_app.logger.info(
                        f"{tv_show} is marked as having no TMDB match, "
                        f"skipping TMDB refresh"
                    )
                    return False

                # If this series already shares a tmdb_id with a canonical
                # record, search under the title of that record.

                if tv_show.tmdb_id != None:
                    existing_series = TVSeries.query.filter_by(
                        tmdb_id=tv_show.tmdb_id
                    ).first()
                    if existing_series:
                        tv_show = existing_series
                description = f"Updating '{tv_show.title}' with TMDB data"
                tmdb_info = tv_show.tmdb_tv_fetch(tmdb_id)

            else:
                return False

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

    slug = "tv" if library == "TV Shows" else "movie"
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


def apply_tmdb_refresh(
    library, id, tmdb_id=None, tmdb_payload=None, notify_if_missing=False
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
    payload is applied.
    """

    # The filename plumbing lives in app.importing. The import is lazy.
    # Thus, the module import direction stays one-way.

    from app.importing import evaluate_filename, reconstruct_filename

    with app.app_context():
        locks = []
        try:
            tmdb_info = None
            if tmdb_payload:
                tmdb_info = json.loads(zlib.decompress(tmdb_payload).decode("utf-8"))

            if library == "Movies":
                # Get the Movie record to update.

                movie = Movie.query.filter_by(id=id).first()
                if movie is None:
                    current_app.logger.warning(
                        f"Movie id {id} no longer exists, skipping TMDB refresh"
                    )
                    return False

                if movie.tmdb_ignored:
                    # The record was detached from TMDB after the payload fetch.
                    current_app.logger.info(
                        f"{movie} is marked as having no TMDB match, "
                        f"discarding the fetched TMDB payload"
                    )
                    return False

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
                        locks = []
                        requeue(
                            "app.videos.apply_tmdb_refresh",
                            key=f"{library}:{id}",
                            delay=(5, 15),
                            reason="A file is locked by another task",
                            kwargs={
                                "library": library,
                                "id": id,
                                "tmdb_id": tmdb_id,
                                "tmdb_payload": tmdb_payload,
                                "notify_if_missing": notify_if_missing,
                            },
                            description=(
                                f"Updating '{movie.title} ({movie.year})' "
                                f"with TMDB data"
                            ),
                        )
                        return False
                    locks.append(lock)

                if existing_movie:
                    movie = existing_movie
                    current_app.logger.info(f"Existing movie: {movie}")
                    existing_movie.tmdb_movie_apply(tmdb_info)
                    db.session.commit()
                else:
                    movie.tmdb_movie_apply(tmdb_info)

                if notify_if_missing and movie.tmdb_id == None:
                    admin_user = User.query.filter(User.admin == True).first()
                    send_email_async(
                        "Fitzflix - Added a movie without a TMDB ID",
                        sender=("Fitzflix", current_app.config["SERVER_EMAIL"]),
                        recipients=[admin_user.email],
                        text_body=render_template(
                            "email/no_tmdb_id.txt", user=admin_user.email, movie=movie
                        ),
                        html_body=render_template(
                            "email/no_tmdb_id.html", user=admin_user.email, movie=movie
                        ),
                    )

                # Keep the updated movie_id field.

                updated_movie_id = movie.id

                # Move the files to the new movie record.

                old_files = File.query.filter_by(movie_id=original_movie_id).all()

                for old_record in old_files:
                    old_record.movie_id = updated_movie_id

                # The merge below deletes the old record, and the files of
                # a record go with it. Thus, stop if the files did not move.
                # The next refresh tries again.

                try:
                    db.session.commit()

                except Exception:
                    current_app.logger.error(traceback.format_exc())
                    db.session.rollback()
                    if updated_movie_id != original_movie_id:
                        return False

                # Reconstruct the untouched filenames with the new movie details.

                files = File.query.filter_by(movie_id=updated_movie_id).all()

                for f in files:
                    untouched_basename = reconstruct_filename(f.id)
                    f.untouched_basename = untouched_basename
                    current_app.logger.info(
                        f"New untouched basename: '{untouched_basename}'"
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

                # Create new directories and move the files if necessary.

                files = File.query.filter_by(movie_id=updated_movie_id).all()
                renamed = False

                for f in files:
                    if tmdb_id != None:
                        file_details = evaluate_filename(
                            f.untouched_basename, tmdb_id=tmdb_id
                        )
                    else:
                        file_details = evaluate_filename(f.untouched_basename)

                    if not file_details:
                        # For example, an id tag in the untouched name no
                        # longer resolves. Leave the file where it is.
                        current_app.logger.warning(
                            f"'{f.untouched_basename}' no longer evaluates, "
                            f"skipping its rename"
                        )
                        continue
                    file_details = library_name_details(f, file_details)

                    new_relative = file_details.get("file_path")
                    old_file = os.path.join(
                        current_app.config["LIBRARY_DIR"], f.file_path
                    )
                    old_directory = os.path.dirname(old_file)
                    new_file = os.path.join(
                        current_app.config["LIBRARY_DIR"], new_relative
                    )

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
                        new_file != old_file
                        and os.path.exists(new_file)
                        and os.path.exists(old_file)
                    )
                    if collision:
                        detail = (
                            f"file #{sibling.id} already claims that path"
                            if sibling
                            else "a file already exists at that path"
                        )
                        current_app.logger.error(
                            f"'{f.basename}' (file #{f.id}) not renamed to "
                            f"'{new_relative}': {detail}. Delete one copy, "
                            f"then re-assign the TMDB id."
                        )
                        admin_user = User.query.filter(User.admin == True).first()
                        send_email_async(
                            "Fitzflix - Rename collision needs triage",
                            sender=("Fitzflix", current_app.config["SERVER_EMAIL"]),
                            recipients=[admin_user.email],
                            text_body=(
                                f"Renaming '{f.basename}' (file #{f.id}) to "
                                f"'{new_relative}' was refused: {detail}.\n\n"
                                f"Delete one of the copies, then re-assign "
                                f"the TMDB id to finish the rename."
                            ),
                            html_body=(
                                f"<p>Renaming '{f.basename}' (file #{f.id}) "
                                f"to '{new_relative}' was refused: {detail}."
                                f"</p><p>Delete one of the copies, then "
                                f"re-assign the TMDB id to finish the "
                                f"rename.</p>"
                            ),
                        )
                        continue

                    os.makedirs(
                        os.path.join(
                            current_app.config["LIBRARY_DIR"],
                            file_details.get("dirname"),
                        ),
                        exist_ok=True,
                    )

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
                                current_app.logger.info(
                                    f"Renaming '{old_file}' to '{new_file}'"
                                )
                                os.rename(old_file, new_file)
                                renamed = True
                    except Exception:
                        current_app.logger.error(traceback.format_exc())
                        continue

                    # Move or delete the old local assets.
                    try:
                        old_assets = os.listdir(old_directory)
                        new_directory = os.path.join(
                            current_app.config["LIBRARY_DIR"],
                            file_details.get("dirname"),
                        )
                        for old_asset in old_assets:
                            if (
                                old_asset.startswith(
                                    ("cover", "default", "movie", "poster")
                                )
                                and old_asset.endswith(("jpg", "jpeg", "png", "tbn"))
                                and f.feature_type_id is None
                                and os.path.join(old_directory, old_asset)
                                != os.path.join(new_directory, old_asset)
                                and os.path.isfile(
                                    os.path.join(old_directory, old_asset)
                                )
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

                    # Clear the old directory tree. This clear knows junk.
                    # It does not only clear an empty directory. The poster
                    # assets moved above. But OS metadata or a leftover
                    # image kept the empty shell alive for the weekly sweep
                    # before.

                    from app.maintenance import clear_leftover_directory

                    clear_leftover_directory(old_directory)

                    # The path fields were already updated inside the
                    # savepoint, before the physical rename.

                    try:
                        db.session.commit()

                    except Exception:
                        current_app.logger.error(traceback.format_exc())
                        db.session.rollback()

                # Rename the archive objects LAST. The deferred re-archive
                # reads the record path from its own session, then
                # uploads that local file. On 2026-09-05 a job dequeued
                # 11 ms after the disk rename, but before the path
                # commit. It saw the old path, found no file, and gave
                # up. Thus, the archive move is queued only after the
                # path commit above.

                for f in files:
                    aws_key = os.path.join(
                        current_app.config["AWS_UNTOUCHED_PREFIX"],
                        sanitize_s3_key(f.untouched_basename),
                    )

                    if f.aws_key != aws_key and os.path.exists(
                        os.path.join(current_app.config["LIBRARY_DIR"], f.file_path)
                    ):
                        # This moves the S3 object. The field changes only
                        # when the object really moved. An object that
                        # cannot be copied server-side (Deep Archive) keeps
                        # its key (#299).
                        try:
                            rename_untouched_object(f, aws_key)
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
                    _follow_rename_in_radarr(
                        original_tmdb_id, movie.tmdb_id, _movie_folder(files)
                    )

                if updated_movie_id != original_movie_id:

                    # Migrate the reviews to the new movie if the movie_id changed.

                    reviews = UserMovieReview.query.filter_by(
                        movie_id=original_movie_id
                    ).all()
                    for review in reviews:
                        review.movie_id = movie.id

                    # The watchlist rows, the status flags, and the awards
                    # move too. The delete of the old record would delete
                    # them with it.

                    move_movie_rows(original_movie_id, movie.id)
                    db.session.flush()

                    # Delete the old movie record from the database.

                    original_movie_record = Movie.query.filter_by(
                        id=original_movie_id
                    ).first()
                    db.session.delete(original_movie_record)

            elif library == "TV Shows":
                # Get the TVSeries record to update.

                tv_show = TVSeries.query.filter_by(id=id).first()
                if tv_show is None:
                    current_app.logger.warning(
                        f"TV series id {id} no longer exists, skipping TMDB refresh"
                    )
                    return False

                if tv_show.tmdb_ignored:
                    current_app.logger.info(
                        f"{tv_show} is marked as having no TMDB match, "
                        f"discarding the fetched TMDB payload"
                    )
                    return False

                # Check if the requested tmdb_id already exists in the
                # TVSeries table. If it does, use that existing TVSeries record.

                if tv_show.tmdb_id != None:
                    existing_series = TVSeries.query.filter_by(
                        tmdb_id=tv_show.tmdb_id
                    ).first()
                    current_app.logger.info(f"Existing TV Series: {existing_series}")
                    if existing_series:
                        tv_show = existing_series

                tv_show.tmdb_tv_apply(tmdb_info)

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
        try:
            response = tmdb_get(
                current_app.config["TMDB_API_URL"] + "/certification/movie/list",
                params={"api_key": current_app.config["TMDB_API_KEY"]},
            )
            response.raise_for_status()
            listing = response.json().get("certifications") or {}
        except Exception:
            current_app.logger.warning(traceback.format_exc())
            return False

        # The unique key compares without case or accents in MySQL. Thus,
        # the lookup folds the value the same way, and a near-duplicate
        # updates the stored row instead of failing the insert.

        def folded(country, value):
            """Return the key of a row as the MySQL collation compares it."""

            return country, unidecode(value).casefold()

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
