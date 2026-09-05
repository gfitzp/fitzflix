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
import random
import shutil
import traceback
import zlib

from datetime import datetime, timedelta

from flask import current_app, render_template
from werkzeug.local import LocalProxy

from app import db, get_app, safe_job_id
from app.aws_storage import rename_untouched_object, sanitize_s3_key
from app.email import send_email as send_email_async
from app.models import File, Movie, TVSeries, User, UserMovieReview


def find_or_create_tmdb_movie(tmdb_id, film_title, year, details=None):
    """Return (movie, created), the record for a TMDB film.

    This function first uses an existing row with the tmdb id. Then it
    uses a record with the same canonical title and year. Only then
    does it create a review-only record. The movie can appear after the
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


def refresh_tmdb_info(library, id, tmdb_id=None, notify_if_missing=False):
    """Run the network phase of a TMDB refresh.

    This phase queries TMDB. Then it gives the payload to
    apply_tmdb_refresh on the sql queue.

    This phase runs on the user-request queue. There, several jobs can
    run at the same time. This is safe, because the phase writes nothing
    to the database. Every database and library-file change occurs in
    apply_tmdb_refresh. The single sql worker serializes those changes.
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

            current_app.sql_queue.enqueue(
                "app.videos.apply_tmdb_refresh",
                kwargs={
                    "library": library,
                    "id": id,
                    "tmdb_id": tmdb_id,
                    "tmdb_payload": tmdb_payload,
                    "notify_if_missing": notify_if_missing,
                },
                job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
                description=description,
            )

        except Exception:
            current_app.logger.error(traceback.format_exc())
            return False

        else:
            return True


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

                # Keep the original movie_id field.

                original_movie_id = movie.id

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
                        sleep_duration = random.randint(5, 15)
                        current_app.logger.warning(
                            f"'{movie.title} ({movie.year})' A file is locked "
                            f"by another task, returning the TMDB refresh to "
                            f"the queue after {sleep_duration} minutes"
                        )
                        current_app.sql_queue.enqueue_in(
                            timedelta(minutes=sleep_duration),
                            "app.videos.apply_tmdb_refresh",
                            library=library,
                            id=id,
                            tmdb_id=tmdb_id,
                            tmdb_payload=tmdb_payload,
                            notify_if_missing=notify_if_missing,
                            job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
                            job_id=safe_job_id(
                                f"retry:apply_tmdb_refresh:{library}:{id}"
                            ),
                            result_ttl=86400,
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

                try:
                    db.session.commit()

                except Exception:
                    current_app.logger.error(traceback.format_exc())
                    db.session.rollback()

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
                    aws_untouched_key = os.path.join(
                        current_app.config["AWS_UNTOUCHED_PREFIX"],
                        sanitize_s3_key(f.untouched_basename),
                    )

                    # WEBDL-rebuild scaffolding (#158): approximately 1,000
                    # rows were flipped to WEBRip on purpose. Their archive
                    # keys stay WEBDL-named until a real WEB-DL replaces
                    # them. The keys are Deep Archive. Thus, a "rename" of
                    # one key means an upload of the multi-gigabyte library
                    # copy again, and the retirement of the scaffold key.
                    # The 2026-08-29 genre backfill started to do exactly
                    # that. Leave those keys alone. The old key still names
                    # a real object. Thus, the archive invariant holds.

                    if "[WEBDL-" in (f.aws_untouched_key or "") and (
                        "[WEBRip-" in aws_untouched_key
                    ):
                        current_app.logger.info(
                            f"'{f.untouched_basename}' keeps its WEBDL-named "
                            f"archive key (rebuild scaffolding, #158)"
                        )
                        continue

                    if f.aws_untouched_key != aws_untouched_key and os.path.exists(
                        os.path.join(current_app.config["LIBRARY_DIR"], f.file_path)
                    ):
                        # This moves the S3 object. The field changes only
                        # when the object really moved. An object that
                        # cannot be copied server-side (Deep Archive) needs
                        # an upload of the library copy instead. That is
                        # too big for the budget of this queue. Thus,
                        # defer_upload gives that to the file queue (#231).
                        try:
                            rename_untouched_object(
                                f, aws_untouched_key, defer_upload=True
                            )
                        except Exception:
                            current_app.logger.error(traceback.format_exc())

                try:
                    db.session.commit()

                except Exception:
                    current_app.logger.error(traceback.format_exc())
                    db.session.rollback()

                if updated_movie_id != original_movie_id:

                    # Migrate the reviews to the new movie if the movie_id changed.

                    reviews = UserMovieReview.query.filter_by(
                        movie_id=original_movie_id
                    ).all()
                    for review in reviews:
                        review.movie_id = movie.id

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
            current_app.request_queue.enqueue(
                "app.videos.refresh_tmdb_info",
                args=("TV Shows", tv.id, tv.tmdb_id),
                job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
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
