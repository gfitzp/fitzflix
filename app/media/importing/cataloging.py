"""The third job of an import: write the records and give the file its final name."""

import os
import re
import traceback
from datetime import datetime, timedelta, timezone
from functools import partial
from rq import get_current_job
from rq.timeouts import BaseTimeoutException
from sqlalchemy.exc import DBAPIError
from flask import current_app, render_template
from werkzeug.local import LocalProxy
from app.tasks import enqueue_task, report_progress, requeue
from app import db, get_app, makedirs_on_mounted_volume
from app.archive.moves import (
    archive_moves_after_unlock,
    clear_archive_target,
    request_archive_move,
)
from app.archive.uploads import mark_archive_stale
from app.archive.archive_keys import pending_archive_of
from app.criterion.criterion_catalog import (
    assign_criterion_release,
    criterion_release_lookups,
    stored_criterion_releases,
)
from app.email import admin_email
from app.email import send_email
from app.models import (
    MOVIES,
    TV_SHOWS,
    ArchivedOriginal,
    File,
    HeldImport,
    Movie,
    RefFeatureType,
    RefQuality,
    TVSeries,
)
from app.media.sdr_conversion import archived_original_from_file
from app.media.file_ranking import find_worse_files
from app.media.tracks import (
    LibraryFileChanged,
    _rescan_after_a_failed_edit,
    replace_track_rows,
    record_filesize,
    refuse_changed_library_file,
)
from app.media import videos
from app.media.importing import chain, filenames, follow_ups, localization, space

app = LocalProxy(get_app)


# A just-written library copy that the mount loses is copied again from
# staging at most this many times. Then the import fails as before.

MAX_VANISHED_RETRIES = 2

# The wait of a cataloging for Sonarr, and the limit of the waits (#372).
# 12 waits of 5 minutes are 1 hour.

SONARR_WAIT = timedelta(minutes=5)
MAX_SONARR_WAITS = 12


def _copy_vanished(hidden_output_file, staged_output, vanished_retries):
    """Return True if the hidden library copy is gone and staging can replace it."""

    return (
        bool(staged_output)
        and vanished_retries < MAX_VANISHED_RETRIES
        and os.path.exists(staged_output)
        and not os.path.exists(hidden_output_file)
    )


def _remove_staged_output(staged_output):
    """Remove the staging copy of an output after the import is complete."""

    if staged_output:
        try:
            os.remove(staged_output)
        except OSError:
            pass


def finalize_localization(
    file_path,
    file_details,
    lock,
    hidden_output_file,
    inspection,
    staged_output=None,
    vanished_retries=0,
    sonarr_waits=0,
    lock_waits=0,
):
    """Add a localized file to the database and move it into position.

    The task runs in 3 phases. _build_records writes the File, Movie or
    TVSeries, quality, and track rows to the session, and finds the
    worse files. _place_file renames the hidden library copy into place,
    or gives the copy back to move_localized_file. Then the task commits.
    _after_commit does the follow-up work. Each follow-up is best effort.
    Each read of the database after the commit is in a follow-up. Thus,
    a failure after the commit never moves the file, and never stops the
    next follow-up. Only the time limit of rq stops them. It ends the job.

    hidden_output_file is the hidden library copy that move_localized_file
    made. inspection is the media details that it read from the file.

    staged_output is the same file on staging, when the library copy
    crossed volumes. The Movies NFS mount can drop at the end of a large
    write. Then macOS remounts it, and the just-written hidden copy is
    gone (Fort Apache, 2026-10-15). If the rename finds no hidden copy,
    the task gives the copy back to move_localized_file, which copies
    again from staged_output. MAX_VANISHED_RETRIES limits the returns.
    The task removes staged_output after the rename and the commit.

    An episode finds its series first, before any record (#372). When
    Sonarr does not answer and a Sonarr rename can explain a new title,
    the task waits SONARR_WAIT, up to MAX_SONARR_WAITS times. Then it
    makes the new series.

    Each run first extends its title lock, as the move does (#520). If
    another task took the lock meanwhile, the task waits for it. It
    writes no row and moves no file of the title until then.
    """

    # The archive move of the import takes the title lock. Thus, it goes
    # to the queue after the release of the lock, as the upload does
    # (#664).

    with app.app_context(), archive_moves_after_unlock():
        # The queue page lists this job with a banner and a row. Name the
        # stage there, as the other stages of the pipeline do.

        basename = file_details.get("basename")
        report_progress(get_current_job(), basename, "Cataloging")

        output_directory = os.path.join(
            current_app.config["LIBRARY_DIR"], file_details.get("dirname")
        )
        counters = {
            "vanished_retries": vanished_retries,
            "sonarr_waits": sonarr_waits,
            "lock_waits": lock_waits,
        }

        # A failure before the commit must not leave the new file in the
        # library with no record (#316). The handler needs to know if the
        # rename happened, and if it replaced a file at that path. None
        # means that the rename did not happen. Each booking goes through
        # handoff. Thus, the 1 HandoffFailed handler below cleans up after
        # each of them (#623).

        placement = None
        try:
            # A dead volume, or a Sonarr wait, can requeue this task for
            # longer than the lock TTL. Then a new import of the same title
            # can take the lock. This check comes first. Thus, each retry
            # extends the lock, or waits for the other task (#520). A Redis
            # fault of the extension is a failed hand-off too (#568).

            if not localization.handoff(chain._hold_title_lock, lock, basename):
                localization.handoff(
                    chain.defer_for_title_lock,
                    "app.media.importing.cataloging.finalize_localization",
                    (
                        file_path,
                        file_details,
                        lock,
                        hidden_output_file,
                        inspection,
                        staged_output,
                    ),
                    counters,
                    file_path,
                    basename,
                )
                return False

            # Defer if a necessary volume is dead. Do this before the
            # records. Then the task keeps the title lock for the retry and
            # does not release it.

            dead = videos._dead_volumes([output_directory, os.path.dirname(file_path)])
            if dead:
                localization.handoff(
                    requeue,
                    "app.media.importing.cataloging.finalize_localization",
                    key=file_path,
                    delay=timedelta(minutes=5),
                    reason=f"Volumes unavailable ({', '.join(dead)}) for the cataloging",
                    # The Sonarr wait count goes with the retry. Otherwise each
                    # volume drop starts the limit of the waits again (#402).
                    counters=counters,
                    args=(file_path, file_details, lock, hidden_output_file),
                    kwargs={"inspection": inspection, "staged_output": staged_output},
                    description=f"'{file_details.get('basename')}'",
                )
                return False

            output_file = os.path.join(
                current_app.config["LIBRARY_DIR"], file_details.get("file_path")
            )
            series_id = None
            if file_details.get("media_library") == TV_SHOWS:
                verdict, series_id = resolve_import_series(file_details)
                # End the read snapshot before the records. The Sonarr call
                # stays out of the write transaction.
                db.session.rollback()
                if verdict == "wait" and sonarr_waits < MAX_SONARR_WAITS:
                    localization.handoff(
                        requeue,
                        "app.media.importing.cataloging.finalize_localization",
                        key=file_path,
                        delay=SONARR_WAIT,
                        reason="waits for Sonarr, because a Sonarr rename can "
                        "own this series",
                        counters={**counters, "sonarr_waits": sonarr_waits + 1},
                        args=(file_path, file_details, lock, hidden_output_file),
                        kwargs={
                            "inspection": inspection,
                            "staged_output": staged_output,
                        },
                        description=f"'{file_details.get('basename')}'",
                    )
                    return False
            records = _build_records(
                file_details,
                inspection,
                os.path.basename(hidden_output_file),
                series_id,
            )
            placement = _place_file(
                file_path,
                file_details,
                lock,
                hidden_output_file,
                output_file,
                output_directory,
                staged_output,
                vanished_retries,
            )

            # A copy that goes back to move_localized_file keeps the title
            # lock for the retried chain.

            if placement == "handed_off":
                return False

            file, worse_local_copies, worse_aws_keys, kept_archives, emails = records
            _commit_records(file, kept_archives, file_path)

        except LibraryFileChanged:
            # An other program put a new download at the path of the
            # source during the chain (#742). The localized copy is of the
            # old download. It goes, with its upload, and the record does
            # not change. The new file is imported after this job.

            db.session.rollback()
            current_app.logger.warning(
                f"'{file_path}' An other program changed or removed this file "
                f"during its import. The localized copy of the old file goes"
            )
            localization.abandon_import(
                file_path,
                lock,
                strays=(hidden_output_file, staged_output),
                reservation=file_details.get("untouched_basename"),
                keep_source=True,
                pending_archive=pending_archive_of(file_details),
            )
            follow_ups._import_changed_source(file_path)
            return False

        except localization.HandoffFailed:
            # A lock extension, a retry or the hand-off to the move could
            # not be booked (#437, #521, #568). The source is good. It stays
            # for the hourly sweep, and the lock and the copies go.

            db.session.rollback()
            _remove_staged_output(hidden_output_file)
            localization.abandon_handoff(
                file_path,
                lock,
                staged_output,
                pending_archive=pending_archive_of(file_details),
            )
            return False

        except BaseException as error:
            # After an outage of the database, the rollback can fail too.
            # The lock and the source must not depend on it (#681).

            try:
                db.session.rollback()
            except Exception:
                current_app.logger.warning(traceback.format_exc())

            # The source goes to rejects, unless the new file is over it.
            # The hidden library copy goes, and a renamed file goes. A
            # file that replaced an other file stays at its path, also
            # when no record points to it. The weekly sync imports a file
            # with no record. A file over the source stays only when a
            # record points to it. A stop signal or an outage of the
            # database keeps the source (#497, #681).

            # The rename can have put the new file over the source (the
            # name that an arr gave is the library name). Then the file at
            # that path is not the rip.

            overwritten = placement is not None and file_path == output_file
            stray = None
            if placement is None:
                stray = hidden_output_file
            elif placement == "renamed" and not overwritten:
                stray = output_file
            # A fault in the lookup or in its repair must not skip the
            # release of the lock and the clean-up below. Then the file
            # stays, as for a lookup that failed.

            # Invariant: the import does not delete the upload of a
            # download that its localized file replaced (#727). The file
            # over the source lost the tracks that the localization
            # removes. Then the run-scoped upload holds the only copy of
            # the original, and it stays. A record at the path takes it as
            # its archive. With no record, the sync prunes it after its
            # grace, and a person can get it before that.

            pending = pending_archive_of(file_details)
            only_copy = _upload_columns(file_details) if overwritten else None
            claimed = False
            if placement == "replaced" or overwritten:
                try:
                    claimed = _claim_at_path(
                        file_details.get("file_path"), output_file, only_copy
                    )
                except Exception:
                    current_app.logger.warning(traceback.format_exc())
                    claimed = True
            _release_title_lock(lock)
            if only_copy and not claimed:
                current_app.logger.warning(
                    f"'{output_file}' The file at this path was not the "
                    f"download, and no record points to it"
                    + _unclaimed_upload_text(only_copy)
                )

            # Invariant: a file that a record points to stays at its path
            # (#688). The repair of _claim_at_path covers it, for each failure.
            # A file over the source with no record is a localized copy. A
            # kept file would import again as an original. Thus, it goes
            # to rejects (#424, #681).

            localization.abandon_failed_stage(
                error,
                file_path,
                None,
                may_keep=not overwritten,
                later_stage=True,
                strays=(stray, staged_output),
                pending_archive=None if overwritten else pending,
                keep_source=overwritten and claimed,
            )
            return None

        # Each follow-up guards itself. This guard is for the code between
        # them. Nothing after the commit moves the file. The time limit of
        # rq ends the job, as in _follow_up (#376, #689).

        archive_due = False
        try:
            archive_due = follow_ups._after_commit(
                file,
                file_path,
                file_details,
                output_file,
                output_directory,
                staged_output,
                worse_local_copies,
                worse_aws_keys,
                emails,
                inspection["audio_tracks"],
            )
        except BaseTimeoutException:
            raise
        except Exception:
            current_app.logger.error(traceback.format_exc())
            db.session.rollback()
            current_app.logger.warning(
                f"'{output_file}' Was imported, but a follow-up step failed"
            )
        else:
            current_app.logger.info(f"'{file_path}' processed as '{output_file}'")
        finally:
            _release_title_lock(lock)

            # The import is done. Its staging reservation goes now, not
            # at the grace sweep of a later reservation (#461).

            try:
                space.release_staging_reservation(os.path.basename(file_path))
            except Exception:
                current_app.logger.warning(traceback.format_exc())

        # The upload takes the title lock. Thus, it is queued after the
        # release. Otherwise it waits for this task (#615 follow-up).

        # The id and the name are reads of the row. Thus, they are in the
        # step too (#689).

        if archive_due:
            follow_ups._follow_up(
                output_file,
                "transcoded archive",
                lambda: enqueue_task(
                    "app.archive.uploads.upload_task",
                    file.id,
                    description=f"'{file.basename}'",
                ),
            )

        # A file that replaced a library file of the same film or episode
        # is a new part of an item that Plex has. Plex sends no webhook
        # for it. The analyze after the next scan of Plex aligns the
        # stream selection of each account (#661). It never raises.

        if placement == "replaced" or worse_local_copies:
            from app.plex.plex_library import (
                REPLACEMENT_ANALYZE_DELAY,
                enqueue_plex_analyze,
            )

            enqueue_plex_analyze(output_file, delay=REPLACEMENT_ANALYZE_DELAY)

        # The scan of Plex does not see an extra that lands in an extras
        # folder that exists already. A refresh of its film does (#775).
        # The extra is in <film folder>/<type>/. It never raises.

        if file_details.get("feature_type_name"):
            from app.plex.plex_library import enqueue_plex_film_refresh

            enqueue_plex_film_refresh(os.path.dirname(output_directory))
        return None


def _release_title_lock(lock):
    """Release the title lock of a finished cataloging and log it."""

    current_app.lock_manager.unlock(lock)
    current_app.logger.info(f"Removed lock {lock}")


def _build_records(file_details, inspection, output_name, series_id=None):
    """Write the records of a localized file to the session. Commit nothing.

    Return (file, worse_local_copies, worse_aws_keys, kept_archives,
    pending_emails). worse_local_copies holds (path, file id) of each
    worse library file. worse_aws_keys holds the archive keys to delete
    after the commit. kept_archives holds the archive-only originals to
    add with the commit. pending_emails holds the sends that wait for the
    commit. This function reads no file. output_name is for the log.
    series_id is the series of an episode that the cataloging found
    before the records."""

    # Look for this File record in the database. If it does not exist,
    # make a new one. If it exists, update that record.

    file = _record_at_path(file_details.get("file_path"))

    # Invariant: after the import, the record names the exact path of
    # the new file (#739). The lookup ignores letter case and accents on
    # MariaDB, and the library shares do not. A record with an other
    # spelling takes the path of the new file. A second record is not
    # possible, because the unique key of the path ignores them too. The
    # gate of the localization rejects the new file while a file is at
    # the old spelling (#749). Thus, no file is there now: Radarr or
    # Sonarr replaced it. The old path goes to the follow-up work, which
    # moves the thumb of an episode.

    if file and file.file_path != file_details.get("file_path"):
        file_details["respelled_from"] = os.path.join(
            current_app.config["LIBRARY_DIR"], file.file_path
        )
        current_app.logger.warning(
            f"{file} The record has the path '{file.file_path}'. It takes the "
            f"path of the new file, '{file_details.get('file_path')}'"
        )
        for column in ("file_path", "dirname", "basename", "plex_title", "edition"):
            if column in file_details:
                setattr(file, column, file_details[column])

    # A real rip that replaces a conversion keeps the original of the
    # conversion in S3, as an archive-only original (#292).

    kept_archives = []
    if not file:
        file = File.from_details(file_details)
        current_app.logger.debug(vars(file))
        current_app.logger.info(f"{file} Creating File record")
        db.session.add(file)

    else:
        current_app.logger.info(f"{file} Existing File record found")
        if (
            file.converted_from_quality
            and not file_details.get("converted_from_quality")
            and file.aws_key
            and file.aws_date_uploaded
        ):
            kept_archives.append(
                archived_original_from_file(file, "replaced by a real rip")
            )

        # The track rows of the existing record go in the
        # replace_track_rows call below. The new file replaces the old
        # one. A retry of an edit of the old file then finds none of its
        # track UIDs, and it stops (#611).

        file.date_updated = datetime.now(timezone.utc)

    if file.media_library == MOVIES:
        _attach_movie(file, file_details)
    elif file.media_library == TV_SHOWS:
        _attach_tv_series(file, file_details, series_id)

    # Set the file quality details.

    quality = RefQuality.query.filter_by(
        quality_title=file_details.get("quality_title")
    ).first()
    file.quality = quality
    current_app.logger.info(f"{file} Setting file_quality {quality}")

    # The conversion fields (#292). A real rip clears them on a reused
    # record.

    file.converted_from_quality = file_details.get("converted_from_quality")
    file.converted_from_format = file_details.get("converted_from_format")

    # A file that is not a conversion has no transcoded/ copy. The weekly
    # S3 sync prunes the copy of a reused record (#299).

    if not file.converted_from_quality:
        file.aws_transcoded_key = None
        file.aws_transcoded_date_uploaded = None
        file.aws_transcoded_filesize_bytes = None

    # A reused record gets the untouched name of the new source. A
    # conversion that reuses the record of a real rip must not keep the
    # name of the rip (#292).

    if file_details.get("untouched_basename"):
        file.untouched_basename = file_details["untouched_basename"]

    # The media details arrive precomputed from move_localized_file. Set
    # the file video track info.

    for field, value in inspection["video"].items():
        setattr(file, field, value)

    # Write the track rows. The flag pass inside marks the forced state of
    # a suspicious subtitle track as unknown. The candidates query decides
    # later if the file needs triage. Imported content is NEW evidence. A
    # re-imported file first deletes an earlier reviewed verdict and the
    # stale aids. A replacement can have a forced track that the original
    # did not have.

    from app.media.triage import reset_triage_state

    reset_triage_state(file)
    replace_track_rows(file, inspection["audio_tracks"], inspection["subtitle_tracks"])

    # Set the localized date. A new localization ends any pending restore
    # of the row (#620). The download of that restore then drops, and it
    # never puts the old archive over the new file.

    file.date_localized = datetime.now(timezone.utc)
    file.restore_requested_at = None

    # Set the AWS archived fields if the task uploaded the file to AWS S3
    # storage. A reused record can have the archive of its old file under
    # a different key. No record claims that key after the commit. A later
    # restore of that key would import the old file under the old name
    # (Sapphire, 2026-10-20). Thus, the key goes with the archives of the
    # worse files.

    replaced_key = None
    if (
        file.aws_date_uploaded
        and file.aws_key
        and file.aws_key != file_details.get("aws_key")
    ):
        replaced_key = file.aws_key

    file.aws_key = file_details.get("aws_key")
    file.aws_date_uploaded = file_details.get("aws_date_uploaded")
    file.aws_filesize_bytes = file_details.get("aws_filesize_bytes")

    # The new library file is a fresh localization of this archive. Thus,
    # the archive is current. A stale mark from a refused archive of the
    # old file must go. Otherwise the next sync uploads the new file to
    # modified/ and deletes the restored rip in untouched/ (#610).

    file.aws_stale = False

    # A pending move of the old archive does not apply to the new one
    # (#369). A reused conversion can keep a transcoded/ copy under the
    # old name. Only the move of that copy is requested here. The key of
    # the original can lag its name (#678). Only a converge run moves it
    # (#683). The move goes to the queue after the release of the title
    # lock (#664).

    if file.aws_key_target:
        clear_archive_target(file)
    if file.converted_from_quality and file.aws_transcoded_key:
        request_archive_move(file, column="aws_transcoded_key")

    record_filesize(file, inspection["filesize_bytes"])
    current_app.logger.info(f"'{output_name}' {file.filesize_bytes} bytes")

    # replace_existing is the "Import" choice of an admin for a held
    # conversion.

    worse_local_copies, worse_aws_keys, pending_emails = _dispose_of_worse_files(
        file, kept_archives, file_details.get("replace_existing", False)
    )
    if replaced_key:
        worse_aws_keys.insert(0, replaced_key)
    return file, worse_local_copies, worse_aws_keys, kept_archives, pending_emails


def _file_at_other_spelling(source_path, file_details):
    """Return the library path of an other file whose record has this path in an other spelling.

    The result is None when no record has an other spelling, or when
    its file is not on the disk. It is None too when its path names the
    source itself. That is a source at the old spelling of the record,
    or a source in the library on a volume that ignores letter case."""

    record = _record_at_path(file_details.get("file_path"))
    if record is None or record.file_path == file_details.get("file_path"):
        return None
    other = os.path.join(current_app.config["LIBRARY_DIR"], record.file_path)
    if not os.path.isfile(other) or follow_ups._same_file(other, source_path):
        return None
    return other


def _record_at_path(library_file_path):
    """Return the File row that MariaDB finds for a library path, or None.

    The path column ignores letter case, accents and spaces at the end.
    Thus, the row can have an other spelling than the path (#739). The
    test database compares the exact text."""

    return File.query.filter_by(file_path=library_file_path).first()


def _attach_movie(file, file_details):
    """Find or make the Movie of a film file and attach it, with its feature type."""

    current_app.logger.info(
        f"{file} Searching in Movies table using "
        f"title='{file_details.get('title')}', year='{file_details.get('year')}'"
    )
    movie = Movie.query.filter_by(
        title=file_details.get("title"), year=file_details.get("year")
    ).first()
    if not movie:
        movie = Movie(title=file_details.get("title"), year=file_details.get("year"))
        current_app.logger.info(f"{file} Creating {movie}")

        # Check the new movie against the stored Criterion list. The
        # import never queries Wikidata (#283). The monthly refresh
        # updates the movie later.

        try:
            criterion_collection = stored_criterion_releases()
        except Exception:
            current_app.logger.warning(traceback.format_exc())
            criterion_collection = []

        assign_criterion_release(
            movie, *criterion_release_lookups(criterion_collection)
        )

        db.session.add(movie)

    file.movie = movie
    current_app.logger.info(f"{file} Associating with {movie}")

    # Set the special feature type if the file is a special feature.

    if file_details.get("feature_type_name"):
        feature_type = RefFeatureType.query.filter_by(
            feature_type=file_details.get("feature_type_name")
        ).first()
        file.feature_type = feature_type
        current_app.logger.info(f"{file} Marking as {feature_type}")


def _existing_series(file_details):
    """Return the TVSeries that the parsed details of an episode name, or None.

    The TMDB id of an id tag comes first. Then the parsed title. Then
    the title of the record that the parsed title names now. A series
    rename can change the title after the parse (#370)."""

    title = file_details.get("title")
    if file_details.get("tmdb_id"):
        found = TVSeries.query.filter_by(tmdb_id=file_details["tmdb_id"]).all()
        if len(found) == 1:
            return found[0]
    tv_series = TVSeries.query.filter_by(title=title).first()
    if tv_series is None and title:
        tv_series = TVSeries.query.filter_by(
            title=filenames._canonical_series_title(title)
        ).first()
    return tv_series


def _show_folder(file_details):
    """Return the show folder of the dirname of an episode, or None."""

    parts = (file_details.get("dirname") or "").split("/")
    return parts[1] if len(parts) > 1 and parts[0] == TV_SHOWS else None


def _rename_plausible(title, folder):
    """Return True if a Sonarr rename can explain a new series title.

    The title has a year, a series with the bare title exists, and no
    file of Fitzflix is in the show folder yet."""

    year_form = re.fullmatch(r"(?P<base>.+) \((?P<year>\d{4})\)", title or "")
    if not year_form or not folder:
        return False
    if TVSeries.query.filter_by(title=year_form.group("base")).first() is None:
        return False
    prefix = f"{TV_SHOWS}/{folder}"
    return (
        File.query.filter(
            db.or_(File.dirname == prefix, File.dirname.like(f"{prefix}/%"))
        ).first()
        is None
    )


def resolve_import_series(file_details):
    """Return (verdict, series id) for the series of an episode import (#372).

    The cataloging calls this function before it writes any record.
    Thus, the Sonarr call does not hold the write transaction. The
    verdict is "ok" with the id of the series, or with None for a new
    series. A series that the name does not give can be the series of a
    rename to the title that waits. It can be the series that Sonarr
    keeps in the show folder of the file. The verdict is "wait" when
    Sonarr did not answer and a Sonarr rename is possible. A new series
    then would split the show."""

    from app.media.series_rename import series_for_import

    found = _existing_series(file_details)
    title = file_details.get("title")
    if found is not None or not title:
        return "ok", found.id if found is not None else None
    folder = _show_folder(file_details)
    found, ok = series_for_import(title, folder)
    if found is not None:
        current_app.logger.info(
            f"'{file_details.get('basename')}' The show folder {folder!r} "
            f"belongs to {found}"
        )
        return "ok", found.id
    if not ok and _rename_plausible(title, folder):
        return "wait", None
    return "ok", None


def _attach_tv_series(file, file_details, series_id=None):
    """Find or make the TVSeries of an episode file and attach it.

    series_id is the series that resolve_import_series found before the
    records. Else the details name the series (_existing_series). Only
    after that does the import make a new series. Else a Sonarr rename
    that adds a year splits the show (#372)."""

    title = file_details.get("title")
    current_app.logger.info(f"{file} Searching in TVSeries table using title='{title}")
    tv_series = db.session.get(TVSeries, series_id) if series_id else None
    if tv_series is None:
        tv_series = _existing_series(file_details)
    if not tv_series:
        tv_series = TVSeries(title=file_details.get("title"))
        current_app.logger.info(f"{file} Creating {tv_series}")
        db.session.add(tv_series)

    file.tv_series = tv_series
    current_app.logger.info(f"{file} Associating with {tv_series}")


def _dispose_of_worse_files(file, kept_archives, replace_existing=False):
    """Delete the rows of the worse files from the session. Return what waits for the commit.

    Return (worse_local_copies, worse_aws_keys, pending_emails). Their
    library copies are deleted only after the new file is in place and
    the commit succeeds. A rollback (an NFS hand-off, a lost copy, a
    failed rename) keeps their rows. Their files must still be there.
    The disposal emails also wait for the commit. An archive-only
    original of a worse conversion goes in kept_archives."""

    worse_files = find_worse_files(file, replace_existing)
    current_app.logger.info(f"{file} worse files: {worse_files}")

    worse_local_copies = []
    worse_aws_keys = []
    pending_emails = []
    for worse in worse_files:
        worse_local_copies.append(
            (os.path.join(current_app.config["LIBRARY_DIR"], worse.file_path), worse.id)
        )

        if (
            worse.converted_from_quality
            and not file.converted_from_quality
            and worse.aws_key
            and worse.aws_date_uploaded
        ):
            kept_archives.append(
                archived_original_from_file(worse, "replaced by a real rip")
            )

        # The library copy of each worse file goes after the commit.
        #
        # If the new file is from digital media, the record and the
        # archive of a worse physical-media file stay. Thus, the rip can
        # come back from S3. The record and the archive of a worse
        # digital-media file go.
        #
        # If the new file is from physical media, the record and the
        # archive of each worse file go. The media source is not
        # important.

        if (
            worse.quality.physical_media == file.quality.physical_media
            or file.quality.physical_media == True
        ):
            if worse.aws_date_uploaded:
                # Record the key now. Delete it from AWS only after the
                # database commit succeeds. Then a failed commit cannot
                # cost the backup of a record that rolled back.
                worse_aws_keys.append(worse.aws_key)

            db.session.delete(worse)

        # The disc of a converted original stays on the shelf (#292).
        # Thus, Fitzflix sends no disposal email when a conversion
        # replaces a file or a file replaces a conversion.

        if (
            worse.quality.physical_media == True
            and file.quality.physical_media == True
            and not worse.converted_from_quality
            and not file.converted_from_quality
        ):
            pending_emails.extend(_disposal_emails(file, worse))
    return worse_local_copies, worse_aws_keys, pending_emails


def _disposal_emails(file, worse):
    """Return the sends that tell the admin and the to-do list about a replaced disc.

    The bodies render now, while the worse row is still in the session.
    The sends wait for the commit."""

    sends = []
    send = admin_email(
        "Replaced a physical media file",
        "replaced_physical_media",
        file=file,
        worse=worse,
    )
    if send is not None:
        sends.append(send)

    if current_app.config["TODO_EMAIL"]:
        todo_email = current_app.config["TODO_EMAIL"]
        sends.append(
            partial(
                send_email,
                f"Find and dispose of the media for '{worse.untouched_basename}'",
                sender=("Fitzflix", current_app.config["SERVER_EMAIL"]),
                recipients=[todo_email],
                text_body=render_template(
                    "email/replaced_physical_media.txt",
                    user=todo_email,
                    file=file,
                    worse=worse,
                ),
                html_body=render_template(
                    "email/replaced_physical_media.html",
                    user=todo_email,
                    file=file,
                    worse=worse,
                ),
            )
        )
    return sends


def _place_file(
    file_path,
    file_details,
    lock,
    hidden_output_file,
    output_file,
    output_directory,
    staged_output,
    vanished_retries,
):
    """Rename the hidden library copy into place. Return "renamed", "replaced", or "handed_off".

    move_localized_file already put the file on the destination volume.
    Thus, this is an immediate rename. "replaced" means that the rename
    replaced a file at that path. The file can be on another volume, or
    the mount can lose the copy. Then the session rolls back, and the
    copy goes back to the file-operation queue. That is "handed_off".
    Long file work never runs on the sql queue."""

    makedirs_on_mounted_volume(output_directory)
    if (
        os.stat(os.path.dirname(hidden_output_file)).st_dev
        != os.stat(output_directory).st_dev
    ):
        current_app.logger.warning(
            f"'{file_details.get('basename')}' isn't at the library "
            f"volume yet; re-queueing the library copy"
        )
        db.session.rollback()

        # The caller keeps the lock only after the enqueue returns. If
        # the enqueue fails, the caller releases the lock and keeps the
        # source (#340, #521).

        localization.handoff(
            enqueue_task,
            "app.media.importing.library_move.move_localized_file",
            file_path,
            file_details,
            lock,
            hidden_output_file,
            description=f"'{file_details.get('basename')}'",
        )
        return "handed_off"

    # Invariant: the import places its result only over the file that it
    # read (#742). A source in the library that an other program changed
    # during the chain is a new download. It is not replaced.

    if file_path == output_file:
        refuse_changed_library_file(file_details.get("source_stamp"), file_path)

    replaced_existing = os.path.exists(output_file)
    try:
        videos._rename_with_retries(hidden_output_file, output_file)
    except OSError:
        if not _copy_vanished(hidden_output_file, staged_output, vanished_retries):
            raise

        # The mount lost the hidden copy, but the output is still on
        # staging. Copy it again. Keep the title lock. Wait 1 minute.
        # Thus, the mount is back before the copy starts.

        db.session.rollback()

        # The copy starts a new chain of the move. Its other counters
        # start at 0. The vanished count keeps the ids of the new chain
        # apart from the ids of the old one.

        localization.handoff(
            requeue,
            "app.media.importing.library_move.move_localized_file",
            key=file_path,
            delay=timedelta(minutes=1),
            reason=(
                f"The library copy disappeared from "
                f"'{os.path.dirname(hidden_output_file)}'. The copy from "
                f"staging starts again (attempt {vanished_retries + 1} of "
                f"{MAX_VANISHED_RETRIES})"
            ),
            counters={
                "transient_retries": 0,
                "space_waits": 0,
                "vanished_retries": vanished_retries + 1,
                "lock_waits": 0,
            },
            args=(file_path, file_details, lock, staged_output),
            description=f"'{file_details.get('basename')}'",
        )
        return "handed_off"
    return "replaced" if replaced_existing else "renamed"


def _upload_columns(file_details):
    """Return the archive columns of the upload of an import, or None (#727).

    The upload is at a run-scoped key when a record claims the normal
    key for different bytes. It is at the normal key when no record
    claims that key. That is the case for a record whose film was
    renamed, because its archive keeps the key of the old name (#734).
    The key can also be an older key that holds the same bytes
    (upload_untouched). None means that the import has no archive."""

    if not (file_details or {}).get("aws_key"):
        return None
    columns = ("aws_key", "aws_date_uploaded", "aws_filesize_bytes")
    return {
        **{column: file_details.get(column) for column in columns},
        "untouched_basename": file_details.get("untouched_basename"),
        "conversion": bool(file_details.get("converted_from_quality")),
    }


def _adopt_upload(row_id, upload, output_file):
    """Give a record the upload of a failed import as its archive (#727, #734).

    The localized file of the import replaced the file of the record,
    and the commit of the import failed. The upload holds the original
    of the file that is now at the path. Thus, the record claims it, as
    the commit would have done, and the archive is current. The weekly
    sync requests the move of a run-scoped upload to its final key. A
    pending move of the old archive does not apply to the new one. A
    pending restore of the old file ends, and the old archive goes.

    A conversion has a second archive in transcoded/ and columns of its
    own. The commit of the import writes them, and this repair does
    not. Thus, a record of a conversion does not adopt, and the upload
    of an import that is a conversion is not adopted. A warning names
    the upload. The sync prunes it after its grace, and the stale flag
    of the caller makes the sync upload the library file.

    upload is the result of _upload_columns, or None. Return True if
    the record claims the upload."""

    if not upload:
        return False
    try:
        file = db.session.get(File, row_id)
        if file is None:
            return False
        if file.converted_from_quality or upload.get("conversion"):
            current_app.logger.warning(
                f"'{output_file}' The record or the import is a conversion. "
                f"The record does not take '{upload['aws_key']}', the original "
                f"of the new file. No record claims it, and the weekly sync "
                f"can prune it"
            )
            return False
        replaced_key = None
        if (
            file.aws_date_uploaded
            and file.aws_key
            and file.aws_key != upload["aws_key"]
        ):
            replaced_key = file.aws_key
        file.aws_key = upload["aws_key"]
        file.aws_date_uploaded = upload["aws_date_uploaded"]
        file.aws_filesize_bytes = upload["aws_filesize_bytes"]

        # A record with no stale flag has the size of its library file
        # (#750, replaced_from_outside in app/media/tracks.py). With the old
        # size, the scan of the repair and the weekly sync read the
        # new file as a file from outside. Its import then deleted the
        # archive that the record takes here. If the share does not
        # give the size now, the record still takes the upload, and the
        # adoption sets the flag. Then the scan of the repair stores the
        # size.

        try:
            record_filesize(file, os.path.getsize(output_file))
            file.aws_stale = False
        except OSError:
            file.aws_stale = True

        # The commit of an import ends a pending restore of the record
        # (#620). The old archive must not come back over the new file.
        # The adoption does the same (#734).

        file.restore_requested_at = None
        if upload["untouched_basename"]:
            file.untouched_basename = upload["untouched_basename"]
        if file.aws_key_target:
            clear_archive_target(file)
        db.session.commit()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
        current_app.logger.warning(
            f"'{output_file}' The record could not take the original of the "
            f"failed import: " + traceback.format_exc()
        )
        return False

    # The archive of the old file has no record now. It goes, as after
    # the commit of an import. Then a download of that key that waits in
    # the queue finds no object, and it stops (#734). The adoption is
    # done. A fault of this step must not report it as failed.

    if replaced_key:
        try:
            follow_ups._delete_replaced_archive(replaced_key)
        except Exception:
            current_app.logger.warning(traceback.format_exc())
    current_app.logger.warning(
        f"'{output_file}' The commit of the import failed after the new file "
        f"replaced the old file. The record takes '{upload['aws_key']}', the "
        f"original of the new file, as its archive"
    )
    return True


def _unclaimed_upload_text(upload):
    """Return a sentence that names an upload that no record took, or an empty text."""

    if not upload:
        return ""
    return (
        f". Its original is in S3 as '{upload['aws_key']}'. No record "
        f"claims it, and the weekly sync can prune it"
    )


def _repair_replaced_record(row_id, output_file, upload):
    """Repair the record of a file that a failed import replaced (#592, #727).

    The track scan reads the rows again from the new file. The record
    takes the upload of the import as its archive, and the size of the
    new file with it. Without an upload, the stale flag makes the sync
    upload the new file, and the scan stores the size."""

    _rescan_after_a_failed_edit(row_id, f"'{output_file}'")
    if not _adopt_upload(row_id, upload, output_file):
        mark_archive_stale(
            row_id, reason="an import replaced the file and its commit failed"
        )


def _record_id_at_exact_path(library_file_path):
    """Return the id of the File row with exactly this library path, or None (#739).

    The path column ignores letter case and accents. A row with an other
    spelling is the record of an other file on the shares. The repair of
    a failed import must not give that record the archive of this file."""

    rows = (
        db.session.query(File.id, File.file_path)
        .filter(File.file_path == library_file_path)
        .order_by(File.id)
        .all()
    )
    return next((row_id for row_id, path in rows if path == library_file_path), None)


def _claim_at_path(library_file_path, output_file, upload=None):
    """Return True if a File row can point to a library path that a failed import wrote.

    A found row gets the repair of _repair_replaced_record. upload is
    the run-scoped upload of an import whose file replaced its own
    source (_upload_columns), or None.

    A lookup that fails gives True with a warning. A row can exist, and
    the database did not say. A rejected file leaves its row with no
    file (#688). The file stays, and repair_replaced_file_task is booked.
    That task makes the lookup again when the database answers. Without
    it, a row keeps the size, the tracks and the archive of the old
    file, and nothing finds that later."""

    if not library_file_path:
        return False
    try:
        row_id = _record_id_at_exact_path(library_file_path)
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
        current_app.logger.warning(
            f"'{output_file}' The lookup of its record failed. The file stays "
            f"at its path: " + traceback.format_exc()
        )
        _book_replaced_file_repair(library_file_path, output_file, 1, upload)
        return True
    if row_id is None:
        return False
    _repair_replaced_record(row_id, output_file, upload)
    return True


# The repair of a file that stayed at its path after a record lookup
# failed (#688). The task waits this long for the database, this many
# times.

REPLACED_REPAIR_DELAY = timedelta(minutes=10)
REPLACED_REPAIR_MAX_TRIES = 6


def _book_replaced_file_repair(library_file_path, output_file, tries, upload=None):
    """Book repair_replaced_file_task for a later time. Redis holds the booking.

    The booking needs no database. A fault of the booking is a warning.
    The failure handler of the cataloging calls this, and it must end.
    upload goes to the task (#727)."""

    name = "app.media.importing.cataloging.repair_replaced_file_task"
    try:
        requeue(
            name,
            key=library_file_path,
            delay=REPLACED_REPAIR_DELAY,
            reason=(
                f"waits for the database. Try {tries} of "
                f"{REPLACED_REPAIR_MAX_TRIES}"
            ),
            counters={"tries": tries},
            args=(library_file_path, output_file),
            kwargs={"upload": upload},
            description=f"'{os.path.basename(output_file)}' Checking the record",
            level="info",
        )
    except Exception:
        current_app.logger.warning(
            f"'{output_file}' The repair of its record could not be booked: "
            + traceback.format_exc()
        )


def repair_replaced_file_task(library_file_path, output_file, tries=1, upload=None):
    """Repair the record of a file that a failed import left at its path (#688).

    This is a task. The cataloging wrote the new file over the file of
    a record, its commit failed, and the lookup of the record failed
    too. The task makes the lookup again. A row that does not agree
    with the size of the file gets the repair of
    _repair_replaced_record. A row that agrees needs nothing. A later import or a
    rescan made it correct. With no row, the file has no record, and 1
    warning says so. The weekly sync imports such a file. While the
    database does not answer, the task books itself again. Return True
    if it queued a repair."""

    from app import get_app

    with get_app().app_context():
        try:
            row = next(
                (
                    found
                    for found in db.session.query(
                        File.id, File.filesize_bytes, File.file_path
                    )
                    .filter(File.file_path == library_file_path)
                    .order_by(File.id)
                    if found.file_path == library_file_path
                ),
                None,
            )
        except DBAPIError:
            try:
                db.session.rollback()
            except Exception:
                pass
            if tries >= REPLACED_REPAIR_MAX_TRIES:
                current_app.logger.warning(
                    f"'{output_file}' The database did not answer in "
                    f"{tries} tries. The record of this file is not checked"
                    + _unclaimed_upload_text(upload)
                )
                return False
            _book_replaced_file_repair(
                library_file_path, output_file, tries + 1, upload
            )
            return False
        if not os.path.exists(output_file):
            return False
        if row is None:
            current_app.logger.warning(
                f"'{output_file}' A failed import left this file, and no "
                f"record points to it" + _unclaimed_upload_text(upload)
            )
            return False
        if row.filesize_bytes == os.path.getsize(output_file):
            return False
        _repair_replaced_record(row.id, output_file, upload)
        return True


def _commit_records(file, kept_archives, file_path):
    """Commit the records of the import with its archive-only originals.

    The archive-only originals commit with the import. Their rows claim
    the keys. Thus, the delete of the worse keys skips them. An
    archive-only row for the key of the new file is no longer necessary.
    The held row of an "Import" choice is done."""

    for archived in kept_archives:
        db.session.add(archived)
    if file.aws_key:
        ArchivedOriginal.query.filter_by(aws_key=file.aws_key).delete()
    HeldImport.query.filter_by(held_path=file_path).delete()
    db.session.commit()
