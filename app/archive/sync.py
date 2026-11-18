"""The weekly audit of the bucket against the library."""

import csv
import io
import os
import traceback
from collections import namedtuple
from datetime import datetime, timedelta, timezone
import botocore
from rq import get_current_job
from rq.registry import StartedJobRegistry
from flask import current_app
from markupsafe import escape
from werkzeug.local import LocalProxy
from app.tasks import enqueue_task, live_jobs, report_progress, requeue
from app import db, get_app
from app.email import notify_admin, notify_admin_text
from app.archive import s3_layout
from app.queues import roster_queues
from app.media.file_ranking import tv_best_holder_ids, ranked_movie_files
from app.models import ArchivedOriginal, File, RefQuality
from app.media import videos
from app.archive import archive_keys, aws_clients, moves, restores

app = LocalProxy(get_app)


# The S3 listing of the archive prefixes that the sync works from.

Listing = namedtuple("Listing", "prefixes sizes modified keys")

# These are the library files that are not videos. The import of the
# uncataloged files skips them.

ARTWORK_PREFIXES = ("cover", "default", "folder", "movie", "poster")
ARTWORK_SUFFIXES = ("jpg", "jpeg", "png", "tbn")


# A library volume that drops during the file pass books the sync again
# after this wait, up to MAX_SYNC_VOLUME_DROPS times (follow-up #431).

SYNC_VOLUME_DROP_WAIT = timedelta(minutes=30)
MAX_SYNC_VOLUME_DROPS = 6


def sync_aws_s3_storage_task(volume_drops=0):
    """Add the files to AWS, and remove the files that are not in the library.

    The sync runs in phases (#358). Each phase has its own error
    handling. A failed phase is logged. The best-effort phases (the
    inventory, the pending moves, the reports, the mails, the import of
    the uncataloged files) still run. The prune deletes objects. Thus, it runs only
    after a clean listing and a clean pass over the file records. After
    a failed phase, the task raises at the end. Then the job is in the
    FailedJobRegistry, and it does not report a success."""

    with app.app_context():
        job = get_current_job()
        if _sync_deferred(job, volume_drops):
            return True

        failures = []
        dropped = []

        def phase(name, func, *args):
            """Run 1 phase. Return (ok, result). Log and record a failure."""

            try:
                return True, func(*args)
            except LibraryVolumeDropped as error:
                db.session.rollback()
                dropped.append(error)
                return False, None
            except Exception as error:
                db.session.rollback()
                current_app.logger.error(
                    f"S3 sync: the {name} phase failed: " + traceback.format_exc()
                )
                failures.append((name, error))
                return False, None

        listed, listing = phase("listing", _list_archives)
        orphaned_files = []
        unreferenced_files = []
        if listed:
            reconciled, result = phase("file records", _reconcile_files, listing, job)
            if dropped:
                return _retry_after_volume_drop(dropped[0], volume_drops)
            if reconciled:
                inventory_export, orphaned_files = result
                phase("inventory", _put_inventory, inventory_export)
            phase("dangling claims", _report_dangling_claims, listing)
            if reconciled:
                phase("prune", _prune_unclaimed, listing, job, unreferenced_files)

        # The moves change keys. Thus, they go to the queue after the
        # prune, which reads the listing.

        phase("pending moves", _request_pending_moves)
        phase("reports", _mail_reports, unreferenced_files, orphaned_files)
        phase(
            "uncataloged files",
            _import_uncataloged,
            (current_app.config["MOVIE_LIBRARY"], current_app.config["TV_LIBRARY"]),
        )

        if failures:
            names = ", ".join(name for name, _ in failures)
            raise RuntimeError(f"S3 sync: the {names} phase(s) failed") from failures[
                0
            ][1]
        return True


def _sync_deferred(job, volume_drops=0):
    """Return True if the sync must wait. Then a retry is booked.

    The sync runs only when each queue is idle. A file can be in an
    import, in an upload, or in a wait for database writes. Then it can
    exist at AWS without its final database record. The prune would see
    it as an extra file and delete it. Each live job of tasks.live_jobs
    counts, on every queue of the roster (#616).
    The maintenance queue runs the hourly import sweep. That sweep can
    feed new files into the import pipeline during the sync.

    An archive move that waits for a retry counts too (#369). It changes
    a key between the listing and the pass over the rows. Other
    scheduled retries do not count. A space wait has no limit, and it
    would hold the sync off for days (Oct 24 2026).

    The check reads the queue registries, never the status in the job
    hash (#655). The Criterion poll books its next run under its own id
    while it runs. When that run ends, RQ writes "finished" over the
    hash of the booked run. A status check then counts the booked run
    as a live job, and the sync waits forever.

    The retry keeps volume_drops (#457). Without it, a defer reset the
    count, and MAX_SYNC_VOLUME_DROPS never stopped the sync."""

    drops = {"volume_drops": volume_drops} if volume_drops else None

    # This task itself is in the started registry of maintenance (#314).
    # Each producer and each retry uses that queue.

    own_id = job.id if job else None
    busy = []
    for queue in roster_queues(current_app):
        active = set(queue.job_ids)
        active.update(queue.intermediate_queue.get_job_ids())
        active.update(StartedJobRegistry(queue=queue).get_job_ids())
        count = sum(
            1
            for live in live_jobs([queue])
            if live.id != own_id
            and (
                live.id in active
                or live.func_name == "app.archive.moves.move_archive_object"
            )
        )
        if count:
            busy.append(f"{queue.name}: {count}")

    if busy:
        requeue(
            "app.archive.sync.sync_aws_s3_storage_task",
            key=None,
            delay=timedelta(minutes=5),
            reason=f"waits for the other tasks to finish ({', '.join(busy)})",
            level="info",
            counters=drops,
        )
        return True

    # A dead library volume makes every one of its files look missing.
    # The sync would then request a paid restore of each, and the direct
    # restores would overwrite the files after the remount.

    dead = videos._dead_volumes(
        [current_app.config["MOVIE_LIBRARY"], current_app.config["TV_LIBRARY"]]
    )
    if dead:
        requeue(
            "app.archive.sync.sync_aws_s3_storage_task",
            key=None,
            delay=timedelta(minutes=30),
            reason=f"Library volume(s) unavailable ({', '.join(dead)})",
            counters=drops,
        )
        return True
    return False


def _list_archives():
    """List the archive prefixes of the bucket. Return a Listing.

    The archives live in 3 prefixes (#299): untouched/ (the rips),
    modified/ (library files after a change), and transcoded/ (the SDR
    copies of the conversions). The sizes map each key to its object
    size. Thus, the file records can get the exact size that AWS bills
    for a restore."""

    prefixes = [
        f"{s3_layout.prefix_value(entry)}/"
        for entry in s3_layout.PREFIXES
        if entry.lifecycle == "archive" or entry.synced
    ]
    listed = [
        entry
        for prefix in prefixes
        for entry in aws_clients.get_matching_s3_objects(
            app.config["AWS_BUCKET"], prefix=prefix
        )
    ]
    sizes = {entry["Key"]: entry["Size"] for entry in listed}
    modified = {entry["Key"]: entry.get("LastModified") for entry in listed}
    return Listing(prefixes, sizes, modified, set(sizes))


class LibraryVolumeDropped(RuntimeError):
    """A library volume dropped or was mounted again during the file pass (#431)."""


def _retry_after_volume_drop(error, volume_drops):
    """Book the sync again after a volume drop in its file pass. Return True.

    A short NFS drop must not cost the weekly sync (follow-up #431). The
    retry comes after SYNC_VOLUME_DROP_WAIT. After MAX_SYNC_VOLUME_DROPS
    drops, the sync fails loudly."""

    if volume_drops + 1 >= MAX_SYNC_VOLUME_DROPS:
        current_app.logger.error(
            f"{error}. That was drop {volume_drops + 1} of "
            f"{MAX_SYNC_VOLUME_DROPS}. The sync stops"
        )
        raise error
    requeue(
        "app.archive.sync.sync_aws_s3_storage_task",
        key=None,
        delay=SYNC_VOLUME_DROP_WAIT,
        reason=f"stopped its file pass ({error})",
        counters={"volume_drops": volume_drops + 1},
    )
    return True


def _library_devices():
    """Return the device id of each library root, or None for a root with no stat."""

    devices = {}
    for key in ("MOVIE_LIBRARY", "TV_LIBRARY"):
        try:
            devices[key] = os.stat(current_app.config[key]).st_dev
        except OSError:
            devices[key] = None
    return devices


def _check_library_volumes(devices_before):
    """Raise LibraryVolumeDropped if a library volume dropped after the pass started.

    A dead volume makes each of its files look absent. A remount gives
    the root a new device id. In both cases, an absent file is not
    evidence. The sync must not restore it or report it as orphaned."""

    config = current_app.config
    dead = videos._dead_volumes([config["MOVIE_LIBRARY"], config["TV_LIBRARY"]])
    if dead:
        reason = f"{', '.join(dead)} not mounted"
    elif _library_devices() != devices_before:
        reason = "a library volume was mounted again"
    else:
        return
    raise LibraryVolumeDropped(
        f"S3 sync: {reason} during the file pass. The pass stops, and nothing "
        f"is pruned"
    )


def _reconcile_files(listing, job):
    """Compare each file record with the listing. Return (inventory, orphans).

    The pass queues the uploads of the missing archives, requests the
    restores of the missing best files, and stores the object sizes.
    The inventory lists the best files for a bulk restore.

    The pass requests no restore of a TV file whose series has the
    Sonarr conflict marker (review of #758). After a refused Sonarr
    rename, the records name the folder that Sonarr moved away. Then
    each best copy of the series looks absent, and the files are in the
    Sonarr folder. The markers are read 1 time for the pass. A Redis
    fault there holds the restore of each TV file for this run.

    A library volume can drop during the pass (#431). Then each later
    file looks absent, and the pass would request a paid restore of
    each. Thus, the pass checks the volumes and the device ids of the
    library roots at each absent file and after the last file. A drop
    raises LibraryVolumeDropped. Then the phase fails, and the prune and
    the inventory do not run."""

    s3_objects = listing.sizes
    s3_keys = listing.keys
    devices_before = _library_devices()

    movie_rank = ranked_movie_files()

    # A TV file is a best copy when it is the best holder of at least 1
    # episode number (#479). A single that a better span holds is not.
    # The sync never pays to restore it.

    tv_best = tv_best_holder_ids()

    files = [
        (file, 1 if rank == 1 or file.id in tv_best else 0)
        for file, rank in db.session.query(
            File,
            db.case((movie_rank.c.rank == 1, 1), else_=0).label("rank"),
        )
        .join(RefQuality, (RefQuality.id == File.quality_id))
        .outerjoin(movie_rank, (movie_rank.c.id == File.id))
        .order_by(RefQuality.preference.asc(), File.aws_key.asc())
        .all()
    ]

    current_app.logger.info(f"Evaluating {len(files)} files for S3 sync")

    held_restores = _HeldRestores({file.series_id for file, _rank in files})
    inventory_export = []
    orphaned_files = []

    for i, (file, rank) in enumerate(files):
        if job:
            report_progress(
                job,
                None,
                "Queuing local files for S3 upload",
                int((i / len(files)) * 100),
            )

        file_path = os.path.join(current_app.config["LIBRARY_DIR"], file.file_path)

        # The transcoded/ copy of a conversion (#299). Upload it when
        # it is missing. When the library file is missing, restore
        # this copy, not the original. It goes straight into the
        # library, with its track edits and no new conversion.

        # The stale flag of a conversion is about this copy. The
        # original in untouched/ never changes. Thus, a stale copy
        # uploads again, and the sync never restores it.

        local = os.path.isfile(file_path)
        if not local:
            _check_library_volumes(devices_before)
        use_transcoded = bool(
            file.converted_from_quality
            and file.aws_transcoded_key in s3_keys
            and file.aws_transcoded_date_uploaded
            and not file.aws_stale
        )
        if file.converted_from_quality and not use_transcoded and local:
            current_app.logger.info(
                f"'{file.basename}' Queuing the transcoded copy for upload"
            )
            enqueue_task(
                "app.archive.uploads.upload_task",
                file.id,
                description=f"'{file.basename}'",
            )
        if use_transcoded:
            remote_size = s3_objects[file.aws_transcoded_key]
            if file.aws_transcoded_filesize_bytes != remote_size:
                file.aws_transcoded_filesize_bytes = remote_size
            if rank == 1:
                inventory_export.append(
                    [current_app.config["AWS_BUCKET"], file.aws_transcoded_key]
                )
                if not local:
                    current_app.logger.info(
                        f"'{file.aws_transcoded_key}' does not exist in "
                        f"the local library"
                    )
                    held_restores.restore(file, file.aws_transcoded_key)

        # The next branches test the state of the archive of the file.

        # The archive is not in S3, but the library file exists. The
        # archive of a conversion is its original (#292). The library
        # file cannot take its place. For a normal file, the upload
        # puts the library file in modified/ (#299).
        if (
            (file.aws_key not in s3_keys or file.aws_date_uploaded == None)
            and os.path.isfile(file_path)
            and not file.archive_is_library_copy
        ):
            current_app.logger.warning(
                f"'{file.aws_key}' The original of this "
                f"conversion is not in AWS S3. The library file is not "
                f"the original. Thus, it is not uploaded"
            )

        elif (
            (
                file.aws_key not in s3_keys
                and not _appeared_after_listing(file.aws_key, file.aws_date_uploaded)
            )
            or file.aws_date_uploaded == None
            or (file.aws_stale and file.archive_is_library_copy)
        ) and os.path.isfile(file_path):

            # Queue the file for an upload to S3.

            current_app.logger.info(f"'{file.aws_key}' Queuing for upload to AWS")

            enqueue_task(
                "app.archive.uploads.upload_task",
                file.id,
                description=f"'{file.basename}'",
            )

        # The file exists in S3.
        elif file.aws_key in s3_keys:

            # Add the file to the inventory.

            current_app.logger.info(f"'{file.aws_key}' Exists in AWS S3; rank {rank}")

            # Record the real size of the object if the record does
            # not have it, or if the size changed after the record.

            remote_size = s3_objects[file.aws_key]
            if file.aws_filesize_bytes != remote_size and not _replaced_after_upload(
                file, remote_size, listing.modified.get(file.aws_key)
            ):
                file.aws_filesize_bytes = remote_size

            if rank == 1 and not use_transcoded:
                inventory_export.append(
                    [current_app.config["AWS_BUCKET"], file.aws_key]
                )

                # Queue the file for a restore if it does not exist
                # locally.

                if not local:
                    current_app.logger.info(
                        f"'{file.aws_key}' does not exist in the local library"
                    )
                    held_restores.restore(file, file.aws_key)

        # The file is not in S3 and does not exist in the filesystem.
        elif file.aws_key not in s3_keys and not local:

            # Flag the file as an orphaned file.

            current_app.logger.info(f"'{file.aws_key}' has no associated files")
            orphaned_files.append([file.id, file.untouched_basename])

    # A drop after the last absent file can still hide a later absent
    # file. Check again before the result counts.

    _check_library_volumes(devices_before)

    # Store the AWS object sizes that this filled in.

    db.session.commit()
    held_restores.report()

    current_app.logger.info(f"Orphaned files: {orphaned_files}")
    return inventory_export, orphaned_files


class _HeldRestores:
    """Hold the sync restores of the TV series with a Sonarr conflict (review of #758).

    The markers of all series are read 1 time, with 1 MGET. A Redis
    fault does not stop the sync. The safe side is no restore of a TV
    file in this run, because a paid restore cannot be taken back. The
    films are not touched."""

    def __init__(self, series_ids):
        from app.media.series_rename import series_in_sonarr_conflict

        self.fault = None
        self.counts = {}
        try:
            self.held = series_in_sonarr_conflict(
                {series_id for series_id in series_ids if series_id is not None}
            )
        except Exception as error:
            self.held = None
            self.fault = f"{type(error).__name__}: {error}"

    def restore(self, file, key):
        """Request the restore of a key, or hold it for the series of the file."""

        series_id = file.series_id
        if series_id is not None and (self.held is None or series_id in self.held):
            self.counts[series_id] = self.counts.get(series_id, 0) + 1
            return
        _sync_restore(key)

    def report(self):
        """Write 1 warning for each held series, or 1 warning after a Redis fault."""

        if self.fault is not None:
            current_app.logger.warning(
                f"S3 sync: the Sonarr conflict markers could not be read "
                f"({self.fault}). Thus, the sync requested no restore of "
                f"{sum(self.counts.values())} TV file(s)"
            )
            return
        if not self.counts:
            return
        from app.models import TVSeries

        titles = dict(
            db.session.query(TVSeries.id, TVSeries.title).filter(
                TVSeries.id.in_(self.counts)
            )
        )
        for series_id, count in sorted(self.counts.items()):
            current_app.logger.warning(
                f"S3 sync: requested no restore of {count} file(s) of the series "
                f"{titles.get(series_id, series_id)!r}, because the Sonarr path "
                f"check found a conflict for the series"
            )


def _sync_restore(key):
    """Request a Bulk restore of a missing best copy from the S3 sync (#610).

    The request writes the pending restore of the row in its own session.
    The sync session read that row earlier. A later write of the row from
    the sync session then fails with MariaDB error 1020. Thus, the sync
    session commits first, and its next read starts a new snapshot. The
    commit keeps the loaded rows. A reload of each row would cost 1 query
    per file, and a row that another worker deleted would raise."""

    session = db.session()
    expire_on_commit = session.expire_on_commit
    session.expire_on_commit = False
    try:
        session.commit()
    finally:
        session.expire_on_commit = expire_on_commit
    restores.aws_restore(key, tier="Bulk")


def _replaced_after_upload(file, remote_size, modified):
    """Return True, and warn, if the claimed object of a file changed after its upload (#430).

    Each upload of Fitzflix records its date and size. An object with a
    new size and a later date than the record holds bytes that the
    record did not upload. The record keeps its size as the evidence.
    Thus, the warning comes again at each sync until an admin looks. A
    server-side move keeps the size, so it never warns."""

    uploaded = file.aws_date_uploaded
    if file.aws_filesize_bytes is None or uploaded is None or modified is None:
        return False
    if uploaded.tzinfo is None:
        uploaded = uploaded.replace(tzinfo=timezone.utc)
    if modified.tzinfo is None:
        modified = modified.replace(tzinfo=timezone.utc)
    if modified <= uploaded:
        return False
    current_app.logger.warning(
        f"'{file.aws_key}' The object changed after the upload of its record "
        f"({file.aws_filesize_bytes} bytes on {uploaded.isoformat()}). It now has "
        f"{remote_size} bytes from {modified.isoformat()}. Another upload can "
        f"have replaced the archive of '{file.basename}'"
    )
    return True


def _appeared_after_listing(key, uploaded):
    """Return True if an archived key that the listing did not show exists now.

    An archive move (#369) can change the key of a row after the
    listing. Then an upload would replace the moved rip with the library
    file. A HEAD tells. A key with no upload date has no object to find."""

    if not key or uploaded is None:
        return False
    try:
        aws_clients.aws_s3_client(with_retries=True).head_object(
            Bucket=current_app.config["AWS_BUCKET"], Key=key
        )
    except botocore.exceptions.ClientError as error:
        if not aws_clients._is_missing(error):
            current_app.logger.warning(f"'{key}' Could not check the key: {error}")
        return False
    except Exception as error:
        current_app.logger.warning(f"'{key}' Could not check the key: {error}")
        return False
    current_app.logger.info(f"'{key}' Arrived in S3 after the listing")
    return True


def _put_inventory(inventory_export):
    """Upload the CSV of the best files to the S3 bucket.

    If a bulk restore of the library is necessary, this file can drive a
    restore of all the best files through an S3 Batch Operation."""

    if inventory_export:
        f = io.StringIO()
        inventory_writer = csv.writer(f, quoting=csv.QUOTE_ALL)
        for file_object in inventory_export:
            inventory_writer.writerow(file_object)
        inventory_file = bytes(f.getvalue(), encoding="utf-8")
        f.close()
        client = aws_clients.aws_s3_client(with_retries=True)
        inventory_key = s3_layout.key_under("inventory", s3_layout.INVENTORY_BASENAME)
        client.put_object(
            Body=inventory_file,
            Bucket=current_app.config["AWS_BUCKET"],
            Key=inventory_key,
        )

        # Each upload replaces the 1 object. Remove the old
        # versions (#318). A failure here is not a sync failure.

        try:
            s3_layout.prune_noncurrent(
                client, current_app.config["AWS_BUCKET"], inventory_key
            )
        except Exception as e:
            current_app.logger.warning(
                f"Could not remove the old versions of '{inventory_key}': {e}"
            )


def _claimed_keys():
    """Return the archive keys that a record claims.

    The archive-only originals (#292) have no File row. Their keys are
    claimed too. So is the target of a pending move (#369). The copy is
    at the target before the row names it."""

    return (
        {aws_key for (aws_key,) in db.session.query(File.aws_key).all()}
        | {key for (key,) in db.session.query(ArchivedOriginal.aws_key).all()}
        | {
            key
            for (key,) in db.session.query(File.aws_transcoded_key)
            .filter(File.aws_transcoded_key.isnot(None))
            .all()
        }
        | {
            key
            for (key,) in db.session.query(File.aws_key_target)
            .filter(File.aws_key_target.isnot(None))
            .all()
        }
    )


def _request_pending_moves():
    """Request the move of each live archive under pending-untouched/ that has no job (#690, #707).

    Invariant: after this step of a sync, a record whose archive is
    under pending-untouched/ has a move job. A key in the record of the
    lost uploads (mark_archive_lost) is the exception. An import
    requests the move after its
    commit. The request can fail, and the job can fail after the record
    got its target. Each case leaves the row on a key that the lifecycle
    rule of the bucket expires after 60 days. Thus, a row with no target
    gets a request. A row with a target gets one when no job of its move
    waits or runs. An import during the sync books its move for the end
    of the sync. That row has its job, and it gets no request.
    A key that the sync reported as lost has no object to move, and it
    gets no request. The commit queues the moves. Return the count of
    the requests."""

    name = "app.archive.moves.move_archive_object"
    lost = {
        key.decode() if isinstance(key, bytes) else key
        for key in current_app.redis.hkeys(moves.ARCHIVE_LOST_KEY)
    }
    moving = {
        int(job.args[0])
        for job in live_jobs(["file"])
        if job.func_name == name and job.args
    }
    rows = (
        File.query.filter(
            File.aws_key.like(f"{archive_keys.PENDING_ARCHIVE_PREFIX}/%"),
        )
        .order_by(File.id)
        .all()
    )
    requested = 0
    for file in rows:
        if file.aws_key in lost:
            continue
        if file.aws_key_target and file.id in moving:
            continue
        if moves.request_archive_move(file):
            requested += 1
            current_app.logger.warning(
                f"'{file.aws_key}' This archive had no job for the move to "
                f"its final key. The move is requested now"
            )
    db.session.commit()
    return requested


def _report_dangling_claims(listing):
    """Mail the admin the claimed keys that have no object in S3.

    This is the rename-skew tripwire. A claim of an uploaded archive
    with no matching object means that a restore would return a 404. The
    2026-08-17 audit
    found 1,184 cases of this silent damage. The sync reports it loudly
    each week. Thus, it can never accumulate."""

    s3_key_set = listing.keys
    dangling_claims = sorted(
        key
        for (key,) in db.session.query(File.aws_key).filter(File.aws_key.isnot(None))
        # A row whose first upload never ran claims no object yet (#534).
        # The transcoded union has the same rule.
        .filter(File.aws_date_uploaded.isnot(None))
        .union(db.session.query(ArchivedOriginal.aws_key))
        .union(
            db.session.query(File.aws_transcoded_key).filter(
                File.aws_transcoded_key.isnot(None),
                File.aws_transcoded_date_uploaded.isnot(None),
            )
        )
        if key not in s3_key_set
    )

    # A lost pending upload (follow-up #455) is named in a section of
    # its own, 1 time after each record of its loss (mark_archive_lost).
    # It needs a re-rip, not a key repair. Later reports leave it out. A
    # move that runs again records the loss again. A key that is no
    # longer dangling leaves the record.

    lost = {
        field.decode(): value.decode()
        for field, value in current_app.redis.hgetall(moves.ARCHIVE_LOST_KEY).items()
    }
    dangling_set = set(dangling_claims)
    gone = [key for key in lost if key not in dangling_set]
    if gone:
        current_app.redis.hdel(moves.ARCHIVE_LOST_KEY, *gone)
    new_lost = sorted(
        key for key in dangling_claims if key in lost and lost[key] != "reported"
    )
    dangling_claims = [key for key in dangling_claims if key not in lost]
    if new_lost:
        notify_admin_text(
            "Rip archives lost before their move",
            "The pending uploads of these records are gone. Each record "
            "needs a re-rip. Later reports leave these keys out:\n\n"
            + "\n".join(new_lost),
            "<p>The pending uploads of these records are gone. Each record "
            "needs a re-rip. Later reports leave these keys out:</p><ul>"
            + "".join(f"<li>{escape(key)}</li>" for key in new_lost)
            + "</ul>",
        )
        current_app.redis.hset(
            moves.ARCHIVE_LOST_KEY, mapping={key: "reported" for key in new_lost}
        )
    if dangling_claims:
        notify_admin_text(
            "Archive keys missing from AWS S3!",
            "These file records claim untouched archive keys "
            "that do not exist in S3. A restore of them would fail:\n\n"
            + "\n".join(dangling_claims),
            "<p>These file records claim untouched archive "
            "keys that do not exist in S3. A restore of them would "
            "fail:</p><ul>"
            + "".join(f"<li>{escape(key)}</li>" for key in dangling_claims)
            + "</ul>",
        )


def _prune_unclaimed(listing, job, unreferenced_files):
    """Delete the S3 objects that no record claims. Add each key to unreferenced_files.

    The caller owns the list. Thus, the report names the deleted keys
    also after a failure in the middle of the prune. An import that
    waits for library space has no File row yet. Its wait has no limit.
    Its live job names its source and its upload key (#619). A same-name
    re-rip uploads to a run-scoped key below pending-untouched/ (#430)."""

    s3_keys = listing.keys
    s3_modified = listing.modified
    archive_prefixes = listing.prefixes
    aws_keys = _claimed_keys()

    from app.media.importing.chain import keys_held_by_pipeline

    waiting_keys = keys_held_by_pipeline()

    # A failed in-place import uploaded its rip to untouched/ directly
    # (#496). Its source went to rejects. That upload is the only copy of
    # the untouched bytes. A rejects directory that cannot be read keeps
    # every untouched/ key this time.

    rejected = _rejected_untouched_keys()

    untouched_prefix = current_app.config["AWS_UNTOUCHED_PREFIX"]
    for i, remote_key in enumerate(s3_keys):
        if remote_key in waiting_keys:
            continue
        if archive_keys._in_prefix(remote_key, untouched_prefix) and (
            rejected is None or remote_key in rejected
        ):
            continue
        if job:
            report_progress(
                job,
                None,
                "Pruning extra files from AWS S3 storage",
                int((i / len(s3_keys)) * 100),
            )

        # An import uploads its rip to untouched/ long before its
        # File row exists. A wait for library space has no limit.
        # Thus, a recent object is never pruned. A real leftover
        # goes at a later weekly sync.

        modified = s3_modified.get(remote_key)
        if modified is not None and modified > datetime.now(timezone.utc) - timedelta(
            days=PRUNE_GRACE_DAYS
        ):
            continue

        # The claim set above is exact in letter case, as S3 is. The
        # check of aws_delete folds case, and it would keep an orphan
        # whose key differs from a claimed key only by case (#686).

        if remote_key not in aws_keys and remote_key not in archive_prefixes:
            moves.aws_delete(remote_key)
            unreferenced_files.append(remote_key)


def _rejected_untouched_keys():
    """Return the untouched/ key of each file in the rejects directory, or None.

    None means that the directory could not be read (#496)."""

    rejects = current_app.config["REJECTS_DIR"]
    prefix = current_app.config["AWS_UNTOUCHED_PREFIX"]
    keys = set()
    if not os.path.isdir(rejects):
        return keys
    try:
        for _, dirnames, filenames in os.walk(rejects, onerror=_raise):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            keys.update(
                os.path.join(prefix, aws_clients.sanitize_s3_key(name))
                for name in filenames
                if not name.startswith(".")
            )
    except OSError:
        return None
    return keys


def _raise(error):
    """Raise the error of os.walk. A silent walk can hide a dead volume."""

    raise error


def _mail_reports(unreferenced_files, orphaned_files):
    """Mail the admin the pruned objects and the orphaned file records."""

    if unreferenced_files:
        notify_admin(
            "Deleted unreferenced AWS files",
            "unreferenced_files",
            unreferenced_files=unreferenced_files,
        )

    if orphaned_files:
        notify_admin(
            "Orphaned file records found!",
            "orphaned_files",
            orphaned_files=orphaned_files,
        )

    _report_lagging_keys()


def _report_lagging_keys():
    """Mail the admin 1 line about the archive keys that lag their names (#668).

    A rename keeps the key. flask aws converge-keys moves the keys that
    lag. The mail comes only when at least 1 key lags. A fault only
    logs. Thus, the line never fails the sync."""

    try:
        drifted = moves.archive_drifts()
    except Exception:
        db.session.rollback()
        current_app.logger.warning(
            "S3 sync: could not count the archive keys that lag their names: "
            + traceback.format_exc()
        )
        return
    if not drifted:
        return
    total = sum(size for _, _, _, size in drifted)
    line = (
        f"{len(drifted)} archive key(s) lag their names: "
        f"{total / 1024**3:.1f} GiB. Run flask aws converge-keys to see "
        f"the list."
    )
    try:
        notify_admin_text(
            "Archive keys that lag their names",
            line,
            f"<p>{escape(line)}</p>",
        )
    except Exception:
        current_app.logger.warning(traceback.format_exc())


def _import_uncataloged(roots):
    """Queue the library files that have no record, or an other size than their record.

    Invariant: the walk never enters a hidden directory (#710). A
    download folder of Sonarr or Radarr in a library folder must be a
    hidden directory, for example .sabnzbd. A download there belongs
    to those apps and to the Manual imports page. Its name matches no
    naming format, and an import would move it to the rejects. The walk
    also stays out of the metadata directories of the NAS."""

    from app.media.importing.chain import enqueue_localization

    library = []
    for root in roots:
        for path, subdirs, local_files in os.walk(root):
            subdirs[:] = [
                name
                for name in subdirs
                if not name.startswith(".") and name != "@eaDir"
            ]
            for name in local_files:
                if name.startswith(ARTWORK_PREFIXES) and name.endswith(
                    ARTWORK_SUFFIXES
                ):
                    continue
                if not name.startswith("."):
                    library_file = os.path.join(path, name)
                    file_path = os.path.relpath(
                        library_file, current_app.config["LIBRARY_DIR"]
                    )
                    library.append((library_file, file_path))

    for library_file, file_path in library:
        row = (
            db.session.query(
                File.id, File.basename, File.filesize_bytes, File.aws_stale
            )
            .filter_by(file_path=file_path)
            .first()
        )
        if row is not None:
            _queue_changed_library_file(row, library_file)
        elif os.path.isfile(library_file) and enqueue_localization(library_file):
            current_app.logger.info(
                f"'{library_file}' isn't in library; added to import queue"
            )


def _queue_changed_library_file(row, library_file):
    """Queue the repair of a library file that has an other size than its record (#750).

    Invariant: the library file at the path of a record is the file of
    that record, or a job is queued that makes it so. Radarr and Sonarr
    upgrade a file at the same path when the quality name does not
    change. They send 1 event, and they do not send it again. The import
    of such a file is lost when the web process does not answer the
    event. It is also lost when a later stage of the import stops and
    keeps its source. No other job finds the file: the record has its
    path.

    The import gate of enqueue_localization skips a file that an import
    holds now. The rule takes a record with the stale flag to have a file
    that an edit of Fitzflix wrote. Its edit runs now, or it died after
    it set the flag. A scan stores its rows and its size
    (replaced_from_outside in app/media/tracks.py has the rule and its
    exceptions). An edit that stops because an other program changed the
    file puts the flag back to its value before the edit (#762)."""

    from app.media import tracks
    from app.media.importing.chain import enqueue_localization

    def differs(record):
        try:
            return record.filesize_bytes is not None and (
                os.path.getsize(library_file) != record.filesize_bytes
            )
        except OSError:
            return False

    if not differs(row):
        return

    # The walk reads its rows in 1 transaction, and the size on the disk
    # is from this moment. An edit can have stored its size and its
    # stale flag after the snapshot. Thus, a new transaction reads the
    # row again before the decision.

    db.session.rollback()
    row = (
        db.session.query(File.id, File.basename, File.filesize_bytes, File.aws_stale)
        .filter_by(id=row.id)
        .first()
    )
    if row is None or not differs(row):
        return
    if row.aws_stale:
        current_app.logger.info(
            f"'{library_file}' has an other size than its record after an edit; "
            f"added to the track scans"
        )
        tracks._rescan_after_a_failed_edit(
            row.id, f"'{row.basename}'", reason="The S3 sync found a changed file"
        )
    elif enqueue_localization(library_file):
        current_app.logger.warning(
            f"'{library_file}' has an other size than its record. Its import "
            f"did not run; added to import queue"
        )


# The weekly S3 sync does not prune an object younger than this. The
# untouched/ upload of an import has no claim until the import finishes.

PRUNE_GRACE_DAYS = 8
