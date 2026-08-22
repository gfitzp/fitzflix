"""The TMDb refresh pair (the strangler split from app.videos).

refresh_tmdb_info fetches a record's canonical TMDb payload on the
network queue; apply_tmdb_refresh applies it on the single-worker sql
queue — record updates, file renames, duplicate merges, and the
untouched-key handoff in S3. find_or_create_tmdb_movie is the shared
record-creation door every review/watchlist surface walks through.

app.videos re-exports every name here, so stored rq job strings
("app.videos.refresh_tmdb_info") and import sites keep resolving; the
filename plumbing lives in app.importing and is imported lazily,
keeping the module import direction one-way.
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
    """(movie, created): the record for a TMDb film — reusing an existing
    row by tmdb id, or a colliding canonical title+year record, before
    creating a review-only one. The movie may have appeared since the
    caller's redirect check (an import or a concurrent log). Callers
    commit and, when created, enqueue the standard TMDb refresh.

    The caller's live TMDb payload (details) primes the display fields
    — title, date, overview, poster, runtime — so the movie page the
    redirect lands on isn't bare while the queued refresh completes;
    tmdb_data_as_of stays unset until the full refresh stamps it.
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
        # Title and date prime together — display code treats a set
        # tmdb_title as a promise that the release date exists
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
    """Every title-lock resource an import of these movies could hold.

    Covers the identifier of each existing file, plus each movie's base
    main-feature identifier so a brand-new first file of the title arriving
    mid-refresh is serialized too. Sorted, so two refreshes acquiring locks
    for overlapping movies can't deadlock each other.
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
    """Network phase of a TMDb refresh: query TMDb, then hand the payload
    to apply_tmdb_refresh on the sql queue.

    This phase runs on the user-request queue, where several jobs may run
    concurrently — safe, because it writes nothing to the database. Every
    database and library-file change happens in apply_tmdb_refresh,
    serialized through the single sql worker.
    """

    with app.app_context():
        try:

            if library == "Movies":
                movie = Movie.query.filter_by(id=id).first()
                if movie is None:
                    # e.g. merged into another record by an earlier job in
                    # a bulk refresh
                    current_app.logger.warning(
                        f"Movie id {id} no longer exists, skipping TMDb refresh"
                    )
                    return False
                description = f"Updating '{movie.title} ({movie.year})' with TMDb data"
                current_app.logger.info(f"tmdb_id: {tmdb_id}")
                tmdb_info = movie.tmdb_movie_fetch(tmdb_id)

            elif library == "TV Shows":
                tv_show = TVSeries.query.filter_by(id=id).first()
                if tv_show is None:
                    current_app.logger.warning(
                        f"TV series id {id} no longer exists, skipping TMDb refresh"
                    )
                    return False

                # Search under the canonical record's title if this series
                # already shares a tmdb_id with one

                if tv_show.tmdb_id != None:
                    existing_series = TVSeries.query.filter_by(
                        tmdb_id=tv_show.tmdb_id
                    ).first()
                    if existing_series:
                        tv_show = existing_series
                description = f"Updating '{tv_show.title}' with TMDb data"
                tmdb_info = tv_show.tmdb_tv_fetch(tmdb_id)

            else:
                return False

            # Compress the payload for its trip through Redis; a details
            # response is small, but a bulk refresh can have thousands of
            # these queued at once

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
    """Write a payload whose apply raised beside the log, so a transient
    upstream glitch can be examined after the fact.

    The 2026-08-22 overnight TV refresh failed on 14 series because TMDb
    served malformed aggregate credits for a few seconds; by the time
    anyone looked, the live payloads were clean again and the bad shape
    was gone — the apply had logged only the traceback. The dump is
    named under the log file (LOG_FILE.tmdb-payload.<library>-<id>.
    <stamp>.json.gz) so rotate_logs' retention glob prunes it with the
    archives. Returns the path, or None when there was nothing to save.
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
            f"Could not save the failed TMDb payload to {path}: "
            f"{traceback.format_exc()}"
        )
        return None
    return path


def apply_tmdb_refresh(
    library, id, tmdb_id=None, tmdb_payload=None, notify_if_missing=False
):
    """Database phase of a TMDb refresh: apply a payload fetched by
    refresh_tmdb_info, rewrite file paths, and merge duplicate records.

    Runs on the single-worker sql queue so refreshes are serialized
    against each other and all other database writes. Movie refreshes
    additionally hold the affected titles' locks for the duration, so
    they can't interleave with an import of the same title. With
    notify_if_missing (used for new imports), an email goes out if the
    movie still has no TMDb match after the payload is applied.
    """

    # Filename plumbing lives in app.importing; lazy so the module
    # import direction stays one-way

    from app.importing import evaluate_filename, reconstruct_filename

    with app.app_context():
        locks = []
        try:
            tmdb_info = None
            if tmdb_payload:
                tmdb_info = json.loads(zlib.decompress(tmdb_payload).decode("utf-8"))

            if library == "Movies":
                # Get the Movie record to be updated

                movie = Movie.query.filter_by(id=id).first()
                if movie is None:
                    current_app.logger.warning(
                        f"Movie id {id} no longer exists, skipping TMDb refresh"
                    )
                    return False

                # Make a note of the original movie_id field.

                original_movie_id = movie.id

                # See if the requested tmdb_id already exists in the Movie table.
                # If so, we'll use that existing Movie record.

                existing_movie = None
                if tmdb_id != None:
                    existing_movie = (
                        Movie.query.filter_by(tmdb_id=tmdb_id)
                        .order_by(Movie.date_created.asc())
                        .first()
                    )

                # This task rewrites file paths and — when the TMDb id
                # reveals a duplicate — merges two movie records, so it must
                # not interleave with a localization chain holding one of
                # these titles' locks. Take every lock an import of either
                # movie could hold (in sorted order, so concurrent refreshes
                # can't deadlock); if any is busy, retry later.

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
                            f"by another task, returning the TMDb refresh to "
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
                                f"with TMDb data"
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
                        "Fitzflix - Added a movie without a TMDb ID",
                        sender=("Fitzflix", current_app.config["SERVER_EMAIL"]),
                        recipients=[admin_user.email],
                        text_body=render_template(
                            "email/no_tmdb_id.txt", user=admin_user.email, movie=movie
                        ),
                        html_body=render_template(
                            "email/no_tmdb_id.html", user=admin_user.email, movie=movie
                        ),
                    )

                # Make a note of the updated movie_id field.

                updated_movie_id = movie.id

                # update files to the new movie record

                old_files = File.query.filter_by(movie_id=original_movie_id).all()

                for old_record in old_files:
                    old_record.movie_id = updated_movie_id

                try:
                    db.session.commit()

                except Exception:
                    current_app.logger.error(traceback.format_exc())
                    db.session.rollback()

                # Reconstruct untouched filenames using the new movie details

                files = File.query.filter_by(movie_id=updated_movie_id).all()

                for f in files:
                    untouched_basename = reconstruct_filename(f.id)
                    f.untouched_basename = untouched_basename
                    current_app.logger.info(
                        f"New untouched basename: '{untouched_basename}'"
                    )

                    aws_untouched_key = os.path.join(
                        current_app.config["AWS_UNTOUCHED_PREFIX"],
                        sanitize_s3_key(untouched_basename),
                    )
                    if f.aws_untouched_key != aws_untouched_key and os.path.exists(
                        os.path.join(current_app.config["LIBRARY_DIR"], f.file_path)
                    ):
                        # Moves the S3 object (or deliberately declines,
                        # for Deep Archive) — the field only changes when
                        # the object really moved
                        try:
                            rename_untouched_object(f, aws_untouched_key)
                        except Exception:
                            current_app.logger.error(traceback.format_exc())

                try:
                    db.session.commit()

                except Exception:
                    current_app.logger.error(traceback.format_exc())
                    db.session.rollback()

                # Create new directories and move files if necessary

                files = File.query.filter_by(movie_id=updated_movie_id).all()

                for f in files:
                    if tmdb_id != None:
                        file_details = evaluate_filename(
                            f.untouched_basename, tmdb_id=tmdb_id
                        )
                    else:
                        file_details = evaluate_filename(f.untouched_basename)

                    new_relative = file_details.get("file_path")
                    old_file = os.path.join(
                        current_app.config["LIBRARY_DIR"], f.file_path
                    )
                    old_directory = os.path.dirname(old_file)
                    new_file = os.path.join(
                        current_app.config["LIBRARY_DIR"], new_relative
                    )

                    # A merge can land this rename on a path the target
                    # movie already owns (the 25 Cats incident:
                    # os.rename silently overwrote the sibling's file,
                    # then the path UPDATE died on the unique index).
                    # Refuse loudly and leave both records untouched —
                    # the admin deletes one deliberately instead. The
                    # one benign shape — old file gone, new file already
                    # in place, no sibling row — falls through so an
                    # interrupted rename can heal its record.

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
                            f"then re-assign the TMDb id."
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
                                f"the TMDb id to finish the rename."
                            ),
                            html_body=(
                                f"<p>Renaming '{f.basename}' (file #{f.id}) "
                                f"to '{new_relative}' was refused: {detail}."
                                f"</p><p>Delete one of the copies, then "
                                f"re-assign the TMDb id to finish the "
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

                    # Database first, disk second: the path update
                    # flushes inside a savepoint so a unique-index
                    # conflict surfaces BEFORE the file moves, and a
                    # failed move rolls the record straight back

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

                    # delete any old local assets
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

                    try:
                        # delete the old directory tree if it's empty
                        os.removedirs(old_directory)

                    except OSError:
                        pass

                    # The path fields were already updated inside the
                    # savepoint, before the physical rename

                    try:
                        db.session.commit()

                    except Exception:
                        current_app.logger.error(traceback.format_exc())
                        db.session.rollback()

                if updated_movie_id != original_movie_id:

                    # Migrate reviews to the new movie if the movie_id changed

                    reviews = UserMovieReview.query.filter_by(
                        movie_id=original_movie_id
                    ).all()
                    for review in reviews:
                        review.movie_id = movie.id

                    # Delete the old movie record from the database

                    original_movie_record = Movie.query.filter_by(
                        id=original_movie_id
                    ).first()
                    db.session.delete(original_movie_record)

            elif library == "TV Shows":
                # Get the TVSeries record to be updated

                tv_show = TVSeries.query.filter_by(id=id).first()
                if tv_show is None:
                    current_app.logger.warning(
                        f"TV series id {id} no longer exists, skipping TMDb refresh"
                    )
                    return False

                # See if the requested tmdb_id already exists in the TVSeries table.
                # If so, we'll use that existing TVSeries record.

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
                    f"TMDb payload that failed to apply saved to {saved}"
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
    """Nightly sweep: re-enqueue the standard TMDb refresh for
    every series still in production, so new episodes and season counts
    stay current without a manual bulk refresh.

    Ended and canceled series change rarely; they are covered by the
    refresh-on-import trigger and the maintenance page's bulk refresh.
    A NULL status counts as in-production — it just means the series
    hasn't been refreshed since before statuses were stored.
    """

    with app.app_context():
        series = (
            TVSeries.query.filter(TVSeries.tmdb_id != None)
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
            f"Queued TMDb refreshes for {len(series)} in-production TV series"
        )
        return len(series)


# This process's app instance, resolved lazily so importing this module from
# a process that already has an application doesn't build a second one

app = LocalProxy(get_app)
