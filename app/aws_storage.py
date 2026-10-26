"""Talk to AWS storage. This is the first strangler slice out of videos.py.

This module holds each function that talks to S3 and SQS. These are
the untouched-original archive uploads, the restores, and the
downloads. They also include the weekly storage audit and the SQS
restore-notification poller. The key functions (etags, key sanitizing,
the untouched-key handoff guard) are here too.

The rq job names are strings that live in Redis. Thus, app.videos
EXPORTS each name here again. The enqueue sites and the stored jobs
continue to say "app.videos.upload_task", and they continue to
resolve. The names that still live in app.videos are imported lazily
inside functions, never at module level. Thus, the import direction
stays videos to aws_storage.
"""

import csv
import email.utils
import hashlib
import io
import json
import os
import re
import threading
import time
import traceback
import urllib.parse
import uuid

from collections import namedtuple
from datetime import datetime, timedelta, timezone

import boto3
import botocore
import requests
import rq

from botocore.client import Config
from rq import get_current_job
from rq.registry import ScheduledJobRegistry, StartedJobRegistry
from s3transfer.utils import ChunksizeAdjuster

from flask import current_app
from sqlalchemy import event as sa_event
from sqlalchemy.orm import Session as OrmSession
from werkzeug.local import LocalProxy

from app.tasks import enqueue_task, report_progress, requeue
from app import db, get_app
from app.email import notify_admin, notify_admin_text
from app import s3_layout
from app import naming
from app.queues import QUEUE_NAME, roster_queues
from app.settings import get_setting
from app.models import (
    ArchivedOriginal,
    File,
    RefQuality,
    ranked_movie_files,
    ranked_tv_files,
)

EIGHT_MEGABYTES = 8388608


def aws_s3_client(with_retries=False):
    """Build an S3 client using the application credentials."""

    kwargs = {
        "aws_access_key_id": current_app.config["AWS_ACCESS_KEY"],
        "aws_secret_access_key": current_app.config["AWS_SECRET_KEY"],
    }
    if with_retries:
        kwargs["config"] = Config(
            connect_timeout=20, retries={"mode": "standard", "max_attempts": 10}
        )
    return boto3.client("s3", **kwargs)


def aws_sqs_client():
    """Build an SQS client using the application credentials."""

    return boto3.client(
        "sqs",
        aws_access_key_id=current_app.config["AWS_ACCESS_KEY"],
        aws_secret_access_key=current_app.config["AWS_SECRET_KEY"],
        region_name="us-east-1",
    )


# A second receive of a message gives it a new receipt handle. The poll
# records it here for the download job of the key. A running job saves its
# own meta for its progress. Thus, the handle has its own key.

SQS_HANDLE_KEY = "fitzflix:sqs:handle:{job_id}"
SQS_HANDLE_SECONDS = 14 * 86400


def _latest_receipt_handle(receipt_handle, job_id=None):
    """Return the newest receipt handle of the message of a download job.

    SQS can ignore a delete with an older handle."""

    if job_id is None:
        job = rq.get_current_job()
        job_id = job.id if job else None
    if job_id is None:
        return receipt_handle
    latest = current_app.redis.get(SQS_HANDLE_KEY.format(job_id=job_id))
    return latest.decode() if latest else receipt_handle


def delete_sqs_message(sqs_client, receipt_handle, note="message"):
    """Delete a message from the SQS queue. Return False if the delete fails."""

    receipt_handle = _latest_receipt_handle(receipt_handle)
    try:
        response = sqs_client.delete_message(
            QueueUrl=current_app.config["AWS_SQS_URL"],
            ReceiptHandle=receipt_handle,
        )
        current_app.logger.debug(f"SQS delete_message response: {response}")

    except:
        current_app.logger.warning(
            f"Unable to delete message '{receipt_handle}' from SQS"
        )
        return False

    current_app.logger.info(f"Deleted {note} '{receipt_handle}' from SQS")
    return True


# The time of each restore request. A Deep Archive restore takes up to 12
# hours, and the restored copy stays for some days. During that time, a
# new download of the same title can replace the file. Then the restored
# copy is old, and an import of it would replace the new file. The entry
# expires after the restored copy does.

RESTORE_REQUESTED_KEY = "fitzflix:restore-requested:{}"
RESTORE_REQUEST_TTL = timedelta(days=7)


def record_restore_request(redis, key, requested=None):
    """Record the time of a restore request for an S3 key."""

    requested = requested or datetime.now(timezone.utc)
    redis.set(
        RESTORE_REQUESTED_KEY.format(key),
        requested.isoformat(),
        ex=int(RESTORE_REQUEST_TTL.total_seconds()),
    )


def restore_requested_at(redis, key):
    """Return the time of the restore request for an S3 key, or None."""

    raw = redis.get(RESTORE_REQUESTED_KEY.format(key))
    if not raw:
        return None
    return datetime.fromisoformat(raw.decode() if isinstance(raw, bytes) else raw)


def overtaken_by_import(redis, file, key):
    """Return True if an import replaced the file after its restore request.

    The record must claim the key, and its library file must be present.
    A restore of a present file that was imported before the request is
    on purpose (for example, a replacement of a bad copy). Thus, it
    continues.
    """

    requested = restore_requested_at(redis, key)
    if file is None or requested is None or file.date_localized is None:
        return False
    localized = file.date_localized
    if localized.tzinfo is None:
        localized = localized.replace(tzinfo=timezone.utc)
    if localized <= requested:
        return False
    return os.path.isfile(
        os.path.join(current_app.config["LIBRARY_DIR"], file.file_path)
    )


class UploadProgressPercentage(object):
    """Report the upload progress as a callback during an upload to AWS S3."""

    def __init__(self, file_path):
        self._file_path = file_path
        self._size = float(os.path.getsize(file_path))
        self._seen_so_far = 0
        self._previous_percent = None
        self._lock = threading.Lock()
        self._job = rq.get_current_job()

    def __call__(self, bytes_amount):
        with self._lock:
            self._seen_so_far += bytes_amount

            # Report a zero-byte file as complete. Do not divide by zero.

            percent = int((self._seen_so_far / self._size) * 100) if self._size else 100

            # The transfer callback runs much more frequently than the tool
            # writes an output line. Thus, the log line and the job-meta
            # write occur only when the percentage changes.

            if percent == self._previous_percent:
                return
            self._previous_percent = percent

            app.logger.info(
                f"'{os.path.basename(self._file_path)}' Uploading to AWS: {percent}%"
            )
            report_progress(
                self._job,
                os.path.basename(self._file_path),
                "Uploading to AWS",
                percent,
            )


class DownloadProgressPercentage(object):
    """Report the download progress as a callback during a download from S3."""

    def __init__(self, client, bucket, key, basename):
        self._file_path = basename
        head = client.head_object(Bucket=bucket, Key=key)
        self.size = head.get("ContentLength", 0)
        self.etag = head.get("ETag")
        app.logger.info(f"'{basename}' Download size: {self.size} bytes")
        self._seen_so_far = 0
        self._previous_percent = None
        self._lock = threading.Lock()
        self._job = rq.get_current_job()

    def __call__(self, bytes_amount):
        with self._lock:
            self._seen_so_far += bytes_amount

            # Report a zero-byte object as complete. Do not divide by zero.

            percent = int((self._seen_so_far / self.size) * 100) if self.size else 100

            # The transfer callback runs much more frequently than the tool
            # writes an output line. Thus, the log line and the job-meta
            # write occur only when the percentage changes.

            if percent == self._previous_percent:
                return
            self._previous_percent = percent

            app.logger.info(
                f"'{os.path.basename(self._file_path)}' Downloading from AWS: {percent}%"
            )
            report_progress(
                self._job,
                os.path.basename(self._file_path),
                "Downloading from AWS",
                percent,
            )


# The S3 listing of the archive prefixes that the sync works from.

Listing = namedtuple("Listing", "prefixes sizes modified keys")

# These are the library files that are not videos. The import of the
# uncataloged files skips them.

ARTWORK_PREFIXES = ("cover", "default", "folder", "movie", "poster")
ARTWORK_SUFFIXES = ("jpg", "jpeg", "png", "tbn")


def sync_aws_s3_storage_task():
    """Add the files to AWS, and remove the files that are not in the library.

    The sync runs in phases (#358). Each phase has its own error
    handling. A failed phase is logged, and the best-effort phases (the
    inventory, the reports, the mails, the import of the uncataloged
    files) still run. The prune deletes objects. Thus, it runs only
    after a clean listing and a clean pass over the file records. After
    a failed phase, the task raises at the end. Then the job is in the
    FailedJobRegistry, and it does not report a success."""

    with app.app_context():
        job = get_current_job()
        if _sync_deferred(job):
            return True

        failures = []

        def phase(name, func, *args):
            """Run 1 phase. Return (ok, result). Log and record a failure."""

            try:
                return True, func(*args)
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
            if reconciled:
                inventory_export, orphaned_files = result
                phase("inventory", _put_inventory, inventory_export)
            phase("dangling claims", _report_dangling_claims, listing)
            if reconciled:
                phase("prune", _prune_unclaimed, listing, job, unreferenced_files)
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


ARCHIVE_MOVE_TASKS = (
    "app.aws_storage.move_archive_object",
    "app.videos.rearchive_untouched_object",
)


def _scheduled_moves(queue):
    """Return the count of the archive moves in the ScheduledJobRegistry of a queue."""

    count = 0
    for job_id in ScheduledJobRegistry(queue=queue).get_job_ids():
        job = queue.fetch_job(job_id)
        if job is not None and job.func_name in ARCHIVE_MOVE_TASKS:
            count += 1
    return count


def _sync_deferred(job):
    """Return True if the sync must wait. Then a retry is booked.

    The sync runs only when each queue is idle. A file can be in an
    import, in an upload, or in a wait for database writes. Then it can
    exist at AWS without its final database record. The prune would see
    it as an extra file and delete it. Every queue of the roster counts.
    The maintenance queue runs the hourly import sweep. That sweep can
    feed new files into the import pipeline during the sync.

    An archive move that waits for a retry counts too (#369). It changes
    a key between the listing and the pass over the rows. Other
    scheduled retries do not count. A space wait has no limit, and it
    would hold the sync off for days (Oct 24 2026)."""

    busy = []
    for queue in roster_queues(current_app):
        queue_name = queue.name
        started = StartedJobRegistry(
            queue_name, connection=current_app.redis
        ).get_job_ids()

        # This task itself is in the started registry of maintenance
        # (#314). Each producer and each retry uses that queue.

        if job:
            started = [job_id for job_id in started if job_id != job.id]

        count = len(queue.job_ids) + len(started) + _scheduled_moves(queue)
        if count:
            busy.append(f"{queue_name}: {count}")

    if busy:
        requeue(
            "app.videos.sync_aws_s3_storage_task",
            key=None,
            delay=timedelta(minutes=5),
            reason=f"waits for the other tasks to finish ({', '.join(busy)})",
            level="info",
        )
        return True

    # A dead library volume makes every one of its files look missing.
    # The sync would then request a paid restore of each, and the direct
    # restores would overwrite the files after the remount.

    from app.videos import _dead_volumes

    dead = _dead_volumes(
        [current_app.config["MOVIE_LIBRARY"], current_app.config["TV_LIBRARY"]]
    )
    if dead:
        requeue(
            "app.videos.sync_aws_s3_storage_task",
            key=None,
            delay=timedelta(minutes=30),
            reason=f"Library volume(s) unavailable ({', '.join(dead)})",
        )
        return True
    return False


def _list_archives():
    """List the archive prefixes of the bucket. Return a Listing.

    The archives live in 3 prefixes (#299): untouched/ (the rips),
    modified/ (library files after a change), and transcoded/ (the 1080p
    copies of the conversions). The sizes map each key to its object
    size. Thus, the file records can get the exact size that AWS bills
    for a restore."""

    prefixes = [
        f"{s3_layout.prefix_value(entry)}/"
        for entry in s3_layout.PREFIXES
        if entry.lifecycle == "archive"
    ]
    listed = [
        entry
        for prefix in prefixes
        for entry in get_matching_s3_objects(app.config["AWS_BUCKET"], prefix=prefix)
    ]
    sizes = {entry["Key"]: entry["Size"] for entry in listed}
    modified = {entry["Key"]: entry.get("LastModified") for entry in listed}
    return Listing(prefixes, sizes, modified, set(sizes))


def _reconcile_files(listing, job):
    """Compare each file record with the listing. Return (inventory, orphans).

    The pass queues the uploads of the missing archives, requests the
    restores of the missing best files, and stores the object sizes.
    The inventory lists the best files for a bulk restore."""

    s3_objects = listing.sizes
    s3_keys = listing.keys

    movie_rank = ranked_movie_files()

    tv_rank = ranked_tv_files()

    files = (
        db.session.query(
            File,
            db.case(
                (movie_rank.c.rank == 1, 1), (tv_rank.c.rank == 1, 1), else_=0
            ).label("rank"),
        )
        .join(RefQuality, (RefQuality.id == File.quality_id))
        .outerjoin(movie_rank, (movie_rank.c.id == File.id))
        .outerjoin(tv_rank, (tv_rank.c.id == File.id))
        .order_by(RefQuality.preference.asc(), File.aws_key.asc())
        .all()
    )

    current_app.logger.info(f"Evaluating {len(files)} files for S3 sync")

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
                "app.videos.upload_task",
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
                    aws_restore(file.aws_transcoded_key, tier="Bulk")

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
                "app.videos.upload_task",
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
            if file.aws_filesize_bytes != remote_size:
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
                    aws_restore(file.aws_key, tier="Bulk")

        # The file is not in S3 and does not exist in the filesystem.
        elif file.aws_key not in s3_keys and not os.path.isfile(file_path):

            # Flag the file as an orphaned file.

            current_app.logger.info(f"'{file.aws_key}' has no associated files")
            orphaned_files.append([file.id, file.untouched_basename])

    # Store the AWS object sizes that this filled in.

    db.session.commit()

    current_app.logger.info(f"Orphaned files: {orphaned_files}")
    return inventory_export, orphaned_files


def _appeared_after_listing(key, uploaded):
    """Return True if an archived key that the listing did not show exists now.

    An archive move (#369) can change the key of a row after the
    listing. Then an upload would replace the moved rip with the library
    file. A HEAD tells. A key with no upload date has no object to find."""

    if not key or uploaded is None:
        return False
    try:
        aws_s3_client(with_retries=True).head_object(
            Bucket=current_app.config["AWS_BUCKET"], Key=key
        )
    except botocore.exceptions.ClientError as error:
        if not _is_missing(error):
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
        client = aws_s3_client(with_retries=True)
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


def _report_dangling_claims(listing):
    """Mail the admin the claimed keys that have no object in S3.

    This is the rename-skew tripwire. An ACTIVE claim with no matching
    object means that a restore would return a 404. The 2026-08-17 audit
    found 1,184 cases of this silent damage. The sync reports it loudly
    each week. Thus, it can never accumulate."""

    s3_key_set = listing.keys
    dangling_claims = sorted(
        key
        for (key,) in db.session.query(File.aws_key)
        .filter(File.aws_key.isnot(None))
        .filter(File.aws_date_deleted.is_(None))
        .union(db.session.query(ArchivedOriginal.aws_key))
        .union(
            db.session.query(File.aws_transcoded_key).filter(
                File.aws_transcoded_key.isnot(None),
                File.aws_transcoded_date_uploaded.isnot(None),
            )
        )
        if key not in s3_key_set
    )
    if dangling_claims:
        notify_admin_text(
            "Archive keys missing from AWS S3!",
            "These file records claim untouched archive keys "
            "that don't exist in S3 — a restore would fail:\n\n"
            + "\n".join(dangling_claims),
            "<p>These file records claim untouched archive "
            "keys that don't exist in S3 — a restore would "
            "fail:</p><ul>"
            + "".join(f"<li>{key}</li>" for key in dangling_claims)
            + "</ul>",
        )


def _prune_unclaimed(listing, job, unreferenced_files):
    """Delete the S3 objects that no record claims. Add each key to unreferenced_files.

    The caller owns the list. Thus, the report names the deleted keys
    also after a failure in the middle of the prune. An import that
    waits for library space has no File row yet. Its wait has no limit.
    Its marker names its source, and the key of its untouched/ upload
    comes from that name."""

    s3_keys = listing.keys
    s3_modified = listing.modified
    archive_prefixes = listing.prefixes
    aws_keys = _claimed_keys()

    from app.importing import SPACE_WAIT_KEY

    marker_prefix = SPACE_WAIT_KEY.format("")
    waiting_keys = {
        os.path.join(
            current_app.config["AWS_UNTOUCHED_PREFIX"],
            sanitize_s3_key(marker.decode()[len(marker_prefix) :]),
        )
        for marker in current_app.redis.scan_iter(f"{marker_prefix}*")
    }

    for i, remote_key in enumerate(s3_keys):
        if remote_key in waiting_keys:
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

        if remote_key not in aws_keys and remote_key not in archive_prefixes:
            aws_delete(remote_key)
            unreferenced_files.append(remote_key)


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


def _import_uncataloged(roots):
    """Queue the files of the library folders that have no record for an import."""

    from app.importing import enqueue_localization

    library = []
    for root in roots:
        for path, subdirs, local_files in os.walk(root):
            for name in local_files:
                if name.startswith(ARTWORK_PREFIXES) and name.endswith(
                    ARTWORK_SUFFIXES
                ):
                    continue
                if not name.startswith(".") and "@eaDir" not in path:
                    library_file = os.path.join(path, name)
                    file_path = os.path.relpath(
                        library_file, current_app.config["LIBRARY_DIR"]
                    )
                    library.append((library_file, file_path))

    for library_file, file_path in library:
        if not File.query.filter_by(file_path=file_path).first() and os.path.isfile(
            library_file
        ):
            if enqueue_localization(library_file):
                current_app.logger.info(
                    f"'{library_file}' isn't in library; added to import queue"
                )


def download_task(key, basename, sqs_receipt_handle=None, transient_retries=0):
    """Download a file from AWS S3 storage."""

    # The retry constants still live in app.videos. Import them lazily.
    # Thus, the module import direction stays videos to aws_storage.

    from app.videos import MAX_TRANSIENT_RETRIES, TRANSIENT_COPY_ERRNOS

    with app.app_context():
        job = get_current_job()

        # A modified/ or transcoded/ object is a library file (#299). It
        # goes straight to the library path of its record, with no import.

        file = File.query.filter_by(aws_key=key).first()
        if file is None:
            file = File.query.filter_by(aws_transcoded_key=key).first()
        place_at = None
        if file is not None and restores_directly(key):
            basename = file.basename
            place_at = os.path.join(current_app.config["LIBRARY_DIR"], file.file_path)
        elif file:
            basename = file.untouched_basename
        else:
            # An archive-only original (#292) keeps its untouched name. The
            # key is sanitized. Thus, it can differ from the name.
            archived = ArchivedOriginal.query.filter_by(aws_key=key).first()
            if archived:
                basename = archived.untouched_basename

        # An import replaced the file after the restore request. The
        # download would replace the new file with the old one, and an
        # object in cold storage would start a new restore. Thus, drop
        # the message and download nothing.

        if overtaken_by_import(current_app.redis, file, key):
            current_app.logger.info(
                f"'{basename}' was imported again after its restore request, "
                f"skipping the download"
            )
            if sqs_receipt_handle:
                return delete_sqs_message(
                    aws_sqs_client(), sqs_receipt_handle, note="overtaken restore"
                )
            return True

        report_progress(job, basename, "Downloading from AWS", percent=None)

        try:
            current_app.logger.info(
                f"Starting download of '{basename}' from AWS S3 storage"
            )
            downloaded = aws_download(
                key, basename, sqs_receipt_handle, place_at=place_at
            )

        except OSError as e:
            if (
                e.errno in TRANSIENT_COPY_ERRNOS
                and transient_retries < MAX_TRANSIENT_RETRIES
            ):
                # The import volume had a fault during the download or the
                # rename. The S3 object and the SQS message are not affected.
                # Thus, retry after the mount settles.

                # The retry is a new job. It carries the newest handle.

                sqs_receipt_handle = _latest_receipt_handle(sqs_receipt_handle)
                requeue(
                    "app.videos.download_task",
                    key=key,
                    delay=timedelta(minutes=5),
                    reason=(
                        f"Download failed with a transient I/O error ({e}) "
                        f"(attempt {transient_retries + 1} of {MAX_TRANSIENT_RETRIES})"
                    ),
                    counters={"transient_retries": transient_retries + 1},
                    args=(key, basename, sqs_receipt_handle),
                    description=f"'{basename}' — Downloading from AWS",
                    meta={"sqs_receipt_handle": sqs_receipt_handle},
                )
                return False
            current_app.logger.error(traceback.format_exc())

        except Exception:
            current_app.logger.error(traceback.format_exc())

        else:
            # A truthy status means that the SQS message was handled. The
            # file arrived (DOWNLOAD_COMPLETE), or there was nothing to
            # download (object missing, restore pending). False means that
            # the retry budget was used up, or that the message could not be
            # deleted.

            if not downloaded:
                current_app.logger.error(
                    f"'{basename}' download from AWS S3 storage failed"
                )
                return False
            return True


def sqs_retrieve_task():
    """Poll AWS SQS for files that can be ready for download."""

    with app.app_context():
        sqs_client = aws_sqs_client()
        s3_client = aws_s3_client(with_retries=True)

        # Extend the timeout and the restore period of a message whose
        # download or archive move (#369) runs or waits in the file queue.

        # The download job of each key that waits, runs, or waits for a
        # retry. A message that comes back for such a key gets no second
        # download. Its new receipt handle goes to that job. The move jobs
        # are keyed the same way, by the key that they move.

        downloads_by_key = {}
        moves_by_key = {}
        # The message stays hidden for SQS_VISIBILITY_SECONDS. That is
        # longer than the hourly poll. Thus, a message is still in flight
        # here, and the change succeeds. A failure on 1 message must not
        # stop the poll (MessageNotInflight, an expired receipt handle).

        for job in _file_queue_jobs():
            restore_key = None
            handle = job.meta.get("sqs_receipt_handle")
            if job.func_name == "app.videos.download_task" and job.args:
                downloads_by_key[job.args[0]] = job
                restore_key = job.args[0]
            elif job.func_name == "app.aws_storage.move_archive_object" and job.args:
                # A lock retry of a move keeps its handle in its kwargs.
                handle = handle or (job.kwargs or {}).get("sqs_receipt_handle")
                restore_key = job.meta.get("archive_key")
                if restore_key is None:
                    moving = db.session.get(File, int(job.args[0]))
                    restore_key = move_source(moving) if moving else None
                if restore_key:
                    moves_by_key[restore_key] = job
            if handle and restore_key:
                job_description = job.meta.get("description", job.description)
                try:
                    sqs_client.change_message_visibility(
                        QueueUrl=current_app.config["AWS_SQS_URL"],
                        ReceiptHandle=_latest_receipt_handle(handle, job_id=job.id),
                        VisibilityTimeout=SQS_VISIBILITY_SECONDS,
                    )
                    current_app.logger.info(
                        f"'{job_description}' Extending SQS message timeout by "
                        f"{SQS_VISIBILITY_SECONDS} seconds"
                    )
                    # S3 counts the days of a new request from now. They
                    # do not add to the old expiry. "Days": 1 cut the
                    # 2-day copy of aws_restore to 1 day (#320).

                    s3_client.restore_object(
                        Bucket=current_app.config["AWS_BUCKET"],
                        Key=restore_key,
                        RestoreRequest={
                            "Days": RESTORE_DAYS,
                            "GlacierJobParameters": {"Tier": "Standard"},
                        },
                    )
                    current_app.logger.info(
                        f"'{restore_key}' Renewing the restored copy for "
                        f"{RESTORE_DAYS} days from now"
                    )
                except botocore.exceptions.ClientError as error:
                    current_app.logger.warning(
                        f"'{job_description}' Could not extend the SQS message "
                        f"or the restore: {error}"
                    )

        response = sqs_client.receive_message(
            QueueUrl=current_app.config["AWS_SQS_URL"],
            AttributeNames=["SentTimestamp"],
            MaxNumberOfMessages=1,
            MessageAttributeNames=["All"],
            VisibilityTimeout=SQS_VISIBILITY_SECONDS,
            WaitTimeSeconds=0,
        )

        while response.get("Messages"):
            response_body = json.loads(response["Messages"][0]["Body"])
            receipt_handle = response["Messages"][0]["ReceiptHandle"]

            # S3 sends an s3:TestEvent with no Records each time a bucket
            # notification is written. On 2026-10-22, 3 of them stopped
            # this poll for 2 days. A message that is not a restore event
            # is deleted.

            records = response_body.get("Records") or []
            key = None
            if records and "s3" in records[0]:
                key = urllib.parse.unquote_plus(records[0]["s3"]["object"]["key"])
            if key is None:
                current_app.logger.info(
                    f"Deleting an SQS message that is not a restore event: "
                    f"{response_body.get('Event') or list(response_body)[:3]}"
                )
                sqs_client.delete_message(
                    QueueUrl=current_app.config["AWS_SQS_URL"],
                    ReceiptHandle=receipt_handle,
                )
            elif key in downloads_by_key or key in moves_by_key:
                waiting = downloads_by_key.get(key) or moves_by_key[key]
                current_app.redis.set(
                    SQS_HANDLE_KEY.format(job_id=waiting.id),
                    receipt_handle,
                    ex=SQS_HANDLE_SECONDS,
                )
                current_app.logger.info(
                    f"'{key}' Already has a job. Recorded the new receipt "
                    f"handle of its message"
                )
            else:
                _route_restore(
                    sqs_client, key, receipt_handle, downloads_by_key, moves_by_key
                )

            response = sqs_client.receive_message(
                QueueUrl=current_app.config["AWS_SQS_URL"],
                AttributeNames=["SentTimestamp"],
                MaxNumberOfMessages=1,
                MessageAttributeNames=["All"],
                VisibilityTimeout=SQS_VISIBILITY_SECONDS,
                WaitTimeSeconds=0,
            )

        return True


def _file_claiming(key):
    """Return the File row whose archive or transcoded/ copy is key, or None."""

    return (
        File.query.filter_by(aws_key=key).first()
        or File.query.filter_by(aws_transcoded_key=key).first()
    )


def _route_restore(sqs_client, key, receipt_handle, downloads_by_key, moves_by_key):
    """Queue the job of a completed restore of key.

    A row with a pending move of key gets the move (#369). The copy then
    happens while the restored copy is readable. A key that no record
    claims gets nothing. A moved archive leaves such a key, and a
    download of it would import an old file under its key name. The
    message is deleted. Each other key gets a download."""

    file = _file_claiming(key)
    if file is not None and file.aws_key_target and move_source(file) == key:
        moves_by_key[key] = enqueue_task(
            "app.aws_storage.move_archive_object",
            file.id,
            sqs_receipt_handle=receipt_handle,
            description=f"'{file.basename}' — Moving the archive",
            meta={"sqs_receipt_handle": receipt_handle, "archive_key": key},
        )
        current_app.logger.info(
            f"'{key}' Restored for the archive move to '{file.aws_key_target}'"
        )
        return
    if file is None and ArchivedOriginal.query.filter_by(aws_key=key).first() is None:
        current_app.logger.info(
            f"'{key}' No record claims this key. Deleting its restore message"
        )
        sqs_client.delete_message(
            QueueUrl=current_app.config["AWS_SQS_URL"],
            ReceiptHandle=receipt_handle,
        )
        return
    downloads_by_key[key] = enqueue_task(
        "app.videos.download_task",
        key,
        os.path.basename(key),
        receipt_handle,
        description=f"'{os.path.basename(key)}' — Downloading from AWS",
        meta={"sqs_receipt_handle": receipt_handle},
    )


def upload_task(file_id):
    """Upload the library file of a record to its AWS S3 archive."""

    with app.app_context():
        try:
            # Get the record of the file for the upload to AWS S3 storage.

            file = File.query.filter_by(id=file_id).first()
            file_path = os.path.join(app.config["LIBRARY_DIR"], file.file_path)

            # The library file goes to the archive of the record: modified/
            # for a normal file, transcoded/ for a conversion (#299). A rip
            # in untouched/ is never overwritten.

            archive_library_file(file, file_path)
            file.date_updated = (
                file.aws_transcoded_date_uploaded
                if file.converted_from_quality
                else file.aws_date_uploaded
            )
            db.session.commit()

        except Exception:
            current_app.logger.error(traceback.format_exc())
            db.session.rollback()

            # The library file changed, and its archive did not follow.
            # The sync uploads a stale archive again.

            mark_archive_stale(file_id, reason="failed upload")

        else:
            return True


# Supporting functions


def _in_prefix(key, prefix):
    """Return True if an S3 key is below a prefix such as "modified"."""

    return bool(key) and key.startswith(f"{prefix}/")


def restores_directly(key):
    """Return True if a restore of this key goes straight into the library (#299).

    A modified/ or transcoded/ object is a library file. An untouched/
    object is a rip, and it goes through the import."""

    config = current_app.config
    return _in_prefix(key, config["AWS_MODIFIED_PREFIX"]) or _in_prefix(
        key, config["AWS_TRANSCODED_PREFIX"]
    )


def _archive_prefix_of(key):
    """Return the archive prefix (untouched, modified, transcoded) that holds key, or None."""

    entry = s3_layout.prefix_of(key)
    if entry is None or entry.lifecycle != "archive":
        return None
    return s3_layout.prefix_value(entry)


def key_claims():
    """Return {key: set of claimant ids} for each archive key that a record claims.

    A File row claims its aws_key, its aws_transcoded_key and its pending
    aws_key_target. An archive-only original claims its key with the id
    None. The backfill reads this 1 time for all rows."""

    claims = {}
    for row in db.session.query(
        File.id, File.aws_key, File.aws_transcoded_key, File.aws_key_target
    ).all():
        for key in row[1:]:
            if key:
                claims.setdefault(key, set()).add(row[0])
    for (key,) in db.session.query(ArchivedOriginal.aws_key).all():
        claims.setdefault(key, set()).add(None)
    return claims


def _claimed_by_another(file, key, claims=None):
    """Return True if a record other than file claims key."""

    if claims is not None:
        return bool(claims.get(key, set()) - {file.id})
    return (
        File.query.filter(
            File.id != file.id,
            db.or_(
                File.aws_key == key,
                File.aws_transcoded_key == key,
                File.aws_key_target == key,
            ),
        ).first()
        is not None
        or ArchivedOriginal.query.filter_by(aws_key=key).first() is not None
    )


def archive_key_for(file, prefix, claims=None):
    """Return the archive key of a file under prefix (#369).

    The key is the prefix and the sanitized untouched name of the file.
    The untouched name holds the title of the film or the series. The
    basename of an extra is not unique, because many films have a
    "Trailer.mkv". A conversion keeps the quality of its original in its
    untouched name. Its transcoded/ copy has the quality of its library
    file. Thus, the tag changes for each prefix but untouched/.

    A key that the file holds already is its own. If another record
    claims the key, the record id goes into the name. claims is the
    result of key_claims, for a pass over many rows."""

    config = current_app.config
    name = sanitize_s3_key(file.untouched_basename or file.basename)
    if (
        file.converted_from_quality
        and file.quality
        and prefix != config["AWS_UNTOUCHED_PREFIX"]
    ):
        name = naming.retag_quality(
            name, file.converted_from_quality, file.quality.quality_title
        )
    key = os.path.join(prefix, name)
    if key in (file.aws_key, file.aws_transcoded_key, file.aws_key_target):
        return key
    if _claimed_by_another(file, key, claims):
        stem, extension = os.path.splitext(name)
        key = os.path.join(prefix, f"{stem} ({file.id}){extension}")
    return key


def archive_drift(file, claims=None):
    """Return (key, rule key) of the first archive of a file that has the wrong key.

    The archive in aws_key comes first. Then the transcoded/ copy of a
    conversion. Return None when each archive has the key of its
    untouched name. A deleted archive, or one with no upload, does not
    count."""

    config = current_app.config
    if file.aws_key and file.aws_date_uploaded and file.aws_date_deleted is None:
        prefix = _archive_prefix_of(file.aws_key)
        if prefix:
            wanted = archive_key_for(file, prefix, claims)
            if wanted != file.aws_key:
                return file.aws_key, wanted
    if (
        file.converted_from_quality
        and file.aws_transcoded_key
        and file.aws_transcoded_date_uploaded
        and _in_prefix(file.aws_transcoded_key, config["AWS_TRANSCODED_PREFIX"])
    ):
        wanted = archive_key_for(file, config["AWS_TRANSCODED_PREFIX"], claims)
        if wanted != file.aws_transcoded_key:
            return file.aws_transcoded_key, wanted
    return None


def _target_column(file):
    """Return the name of the key column that the pending target of a file replaces."""

    if _in_prefix(file.aws_key_target, current_app.config["AWS_TRANSCODED_PREFIX"]):
        return "aws_transcoded_key"
    return "aws_key"


def move_source(file):
    """Return the current key of the archive that the pending move of a file moves."""

    if not file.aws_key_target:
        return None
    return getattr(file, _target_column(file))


# The time of the first request of each pending move, by file id. The
# System page shows the oldest. The row has no column for it.

ARCHIVE_MOVE_REQUESTED_KEY = "fitzflix:archive-move:requested"

# The session info key of the moves that wait for the commit of the
# session. A move job that starts before the commit reads no target.

_MOVES_AFTER_COMMIT = "fitzflix_archive_moves"


def clear_archive_target(file):
    """Clear the pending target of a file. The caller commits."""

    file.aws_key_target = None
    try:
        current_app.redis.hdel(ARCHIVE_MOVE_REQUESTED_KEY, str(file.id))
    except Exception:
        current_app.logger.warning(traceback.format_exc())


def request_archive_move(file, claims=None):
    """Request the move of the archive of a file to the key of its untouched name (#369).

    Set aws_key_target, and queue move_archive_object after the next
    commit of the session. The caller commits. A pending target is
    replaced. The job reads the current target when it runs. A file
    whose archives have their keys loses its pending target. Return True
    if a move is pending."""

    drift = archive_drift(file, claims)
    if drift is None:
        if file.aws_key_target:
            clear_archive_target(file)
        return False
    source, target = drift
    if file.aws_key_target != target:
        current_app.logger.info(
            f"'{file.basename}' The archive '{source}' moves to '{target}'"
        )
    file.aws_key_target = target
    if claims is not None:
        claims.setdefault(target, set()).add(file.id)
    db.session.info.setdefault(_MOVES_AFTER_COMMIT, {})[file.id] = file.basename

    # The request time is only for the System page. A Redis fault must not
    # stop the move.

    try:
        current_app.redis.hsetnx(
            ARCHIVE_MOVE_REQUESTED_KEY,
            str(file.id),
            datetime.now(timezone.utc).isoformat(),
        )
    except Exception:
        current_app.logger.warning(traceback.format_exc())
    return True


# The ids of the File rows that this session deleted. Their request
# times leave the Redis hash at the commit.

_PURGED_FILES = "fitzflix_purged_files"


@sa_event.listens_for(File, "after_delete")
def _note_purged_file(mapper, connection, target):
    """Record a deleted File row. Its move request time goes at the commit."""

    session = db.inspect(target).session
    if session is not None:
        session.info.setdefault(_PURGED_FILES, set()).add(target.id)


@sa_event.listens_for(OrmSession, "after_commit")
def _enqueue_requested_moves(session):
    """Queue the archive moves that request_archive_move recorded in this session.

    Also remove the request times of the deleted File rows. Thus, the
    oldest age on the System page cannot come from a purged row."""

    purged = session.info.pop(_PURGED_FILES, None)
    if purged:
        try:
            current_app.redis.hdel(
                ARCHIVE_MOVE_REQUESTED_KEY, *[str(file_id) for file_id in purged]
            )
        except Exception:
            current_app.logger.warning(traceback.format_exc())
    pending = session.info.pop(_MOVES_AFTER_COMMIT, None)
    for file_id, basename in sorted((pending or {}).items()):
        try:
            _enqueue_move_once(file_id, basename)
        except Exception:
            current_app.logger.error(traceback.format_exc())


def _enqueue_move_once(file_id, basename):
    """Queue the move job of a file, unless a move of the file waits in the queue (#384).

    The id is "archive-move-", the file id, and a random tag. A queued
    move reads the target of the file when it runs. Thus, a second
    request adds nothing. A running move does not count. It can hold an
    older target. Each id is new, so no job takes the expiry of an old
    finished job."""

    from app.tasks import TASKS, resolve_queue

    name = "app.aws_storage.move_archive_object"
    prefix = f"archive-move-{int(file_id)}-"
    queue = resolve_queue(TASKS[name].queue)
    if any(job_id.startswith(prefix) for job_id in queue.job_ids):
        return None
    return enqueue_task(
        name,
        file_id,
        description=f"'{basename}' — Moving the archive",
        job_id=prefix + uuid.uuid4().hex[:12],
    )


@sa_event.listens_for(OrmSession, "after_rollback")
def _forget_requested_moves(session):
    """Drop the archive moves of a transaction that rolled back."""

    session.info.pop(_MOVES_AFTER_COMMIT, None)
    session.info.pop(_PURGED_FILES, None)


def pending_archive_moves():
    """Return the count of the pending archive moves and the oldest request time."""

    ids = [
        file_id
        for (file_id,) in db.session.query(File.id)
        .filter(File.aws_key_target.isnot(None))
        .all()
    ]
    oldest = None
    if ids:
        stamps = current_app.redis.hmget(
            ARCHIVE_MOVE_REQUESTED_KEY, [str(file_id) for file_id in ids]
        )
        times = [
            datetime.fromisoformat(
                stamp.decode() if isinstance(stamp, bytes) else stamp
            )
            for stamp in stamps
            if stamp
        ]
        oldest = min(times) if times else None
    return {"count": len(ids), "oldest": oldest}


def archive_drifts(claims=None):
    """Return (file, key, rule key, bytes) for each archive that has the wrong key.

    This is the census of the backfill (flask aws converge-keys)."""

    claims = claims if claims is not None else key_claims()
    drifted = []
    for file in (
        File.query.filter(
            db.or_(File.aws_key.isnot(None), File.aws_transcoded_key.isnot(None))
        )
        .order_by(File.id)
        .all()
    ):
        drift = archive_drift(file, claims)
        if drift is None:
            continue
        source, target = drift
        size = (
            file.aws_transcoded_filesize_bytes
            if source == file.aws_transcoded_key
            else file.aws_filesize_bytes
        )
        drifted.append((file, source, target, size or 0))
    return drifted


def archive_library_file(file, path, delete_replaced=True):
    """Archive the current library file of a record, and commit (#299).

    path holds the bytes of the library file: the library file itself,
    or the same file on staging. A conversion uploads to transcoded/. Its
    original in untouched/ never changes. A normal file uploads to
    modified/. The first time, the untouched/ rip is deleted after the
    commit. Versioning keeps it for 180 days. Later changes overwrite the
    modified/ object. Return the new key.

    A pending move of the same archive (#369) ends here. The upload goes
    to the key of the new name, the old key is deleted, and the target
    is cleared. Thus, an edit completes the move."""

    config = current_app.config
    converted = bool(file.converted_from_quality)
    if converted:
        prefix = config["AWS_TRANSCODED_PREFIX"]
        existing = file.aws_transcoded_key
        column = "aws_transcoded_key"
    else:
        prefix = config["AWS_MODIFIED_PREFIX"]
        existing = file.aws_key if _in_prefix(file.aws_key, prefix) else None
        column = "aws_key"
    pending = bool(file.aws_key_target) and _target_column(file) == column
    if existing and not pending:
        key_prefix, key_name = os.path.split(existing)
    else:
        key_prefix, key_name = prefix, os.path.basename(archive_key_for(file, prefix))

    key, uploaded, size = aws_upload(
        path, key_prefix, key_name=key_name, force_upload=True, ignore_etag=True
    )

    # The upload can take an hour. The transaction of this session started
    # before it. MariaDB refuses an UPDATE of a row that a different worker
    # changed after that start (error 1020). Thus, end the old transaction
    # here. The record then loads again from a new snapshot. Each caller
    # commits its own changes before this function.

    db.session.commit()

    replaced = getattr(file, column)
    if replaced == key:
        replaced = None
    if converted:
        file.aws_transcoded_key = key
        file.aws_transcoded_date_uploaded = uploaded
        file.aws_transcoded_filesize_bytes = size
        file.aws_stale = False
    else:
        file.aws_key = key
        file.aws_date_uploaded = uploaded
        file.aws_filesize_bytes = size
        file.aws_stale = False
    # The upload took time. A second rename can have set a newer target
    # meanwhile. The commit above loaded it. Thus, the target is computed
    # again, not cleared. A target that the upload completed goes.

    if file.aws_key_target and _target_column(file) == column:
        request_archive_move(file)
    db.session.commit()
    current_app.logger.info(f"'{file.basename}' Archived the library file as '{key}'")

    if replaced and delete_replaced and not untouched_key_still_claimed(replaced):
        enqueue_task(
            "app.videos.aws_delete",
            replaced,
            description=f"Deleting '{replaced}' from AWS",
        )
    return key


def move_archive_to_modified_task(file_id):
    """Move the replaced untouched/ archive of a normal file to modified/ (#299 backfill).

    Before #299, a change of the library file uploaded it over the rip in
    untouched/. Fitzflix selects these files by date and size. But a TMDb
    rename before 2026 also reset the upload date of a rip. Thus, the task
    deletes the old object only when the S3 objects prove that it is a
    copy of the library file:

    1. The size of the library file must equal the size of the old
       object. If not, the task changes nothing.
    2. The task uploads the library file to modified/, with the name of
       the untouched/ key. The ETags of the old and the new object must
       be equal. If not, the task deletes the new object and changes
       nothing else.
    3. Then the record points to modified/, and the old version is
       deleted permanently. A delete marker would keep a copy of the
       library file for 180 days, for no use.

    The task holds the lock of the title. Thus, a track edit cannot change
    the file during the upload. Return True when the archive is in
    modified/."""

    from app.videos import acquire_lock_or_defer

    with app.app_context():
        config = current_app.config
        file = db.session.get(File, int(file_id))
        if file is None or file.converted_from_quality:
            return False
        if not _in_prefix(file.aws_key, config["AWS_UNTOUCHED_PREFIX"]):
            return _in_prefix(file.aws_key, config["AWS_MODIFIED_PREFIX"])

        lock = acquire_lock_or_defer(
            file.file_identifier(),
            config["UPLOAD_TASK_TIMEOUT"] * 1000,
            current_app.file_queue,
            "app.aws_storage.move_archive_to_modified_task",
            minutes=(5, 15),
            timeout=config["UPLOAD_TASK_TIMEOUT"],
            description=f"Move the archive of {file.basename} to modified/ (#299)",
            args=(file_id,),
            key=int(file_id),
        )
        if not lock:
            return False

        try:
            db.session.refresh(file)
            old_key = file.aws_key
            if not _in_prefix(old_key, config["AWS_UNTOUCHED_PREFIX"]):
                return _in_prefix(old_key, config["AWS_MODIFIED_PREFIX"])
            path = os.path.join(config["LIBRARY_DIR"], file.file_path)
            if not os.path.isfile(path):
                current_app.logger.warning(
                    f"'{file.basename}' No local file. The archive stays in "
                    f"'{old_key}'"
                )
                return False

            s3_client = aws_s3_client(with_retries=True)
            bucket = config["AWS_BUCKET"]
            old = s3_client.head_object(Bucket=bucket, Key=old_key)
            local_size = os.path.getsize(path)
            if local_size != old["ContentLength"]:
                current_app.logger.warning(
                    f"'{file.basename}' The library file has {local_size} bytes "
                    f"and '{old_key}' has {old['ContentLength']}. The sizes "
                    f"differ. Thus, the archive stays in untouched/"
                )
                return False

            prefix = config["AWS_MODIFIED_PREFIX"]
            key, uploaded, size = aws_upload(
                path,
                prefix,
                key_name=os.path.basename(archive_key_for(file, prefix)),
                force_upload=True,
                ignore_etag=True,
            )
            new = s3_client.head_object(Bucket=bucket, Key=key)
            if new["ETag"] != old["ETag"]:
                current_app.logger.warning(
                    f"'{file.basename}' The ETag of '{key}' ({new['ETag']}) "
                    f"differs from the ETag of '{old_key}' ({old['ETag']}). The "
                    f"bytes differ. Thus, the archive stays in untouched/, and "
                    f"the new object is deleted"
                )
                s3_layout.delete(s3_client, bucket, key, new.get("VersionId"))
                return False

            file.aws_key = key
            file.aws_date_uploaded = uploaded
            file.aws_filesize_bytes = size
            file.aws_stale = False
            if file.aws_key_target and _target_column(file) == "aws_key":
                clear_archive_target(file)
            db.session.commit()
            if not untouched_key_still_claimed(old_key):
                # The old version goes permanently. The table entry of
                # untouched/ in app/s3_layout.py records why.
                s3_layout.delete(s3_client, bucket, old_key, old.get("VersionId"))
            current_app.logger.info(
                f"'{file.basename}' Moved the archive to '{key}' and deleted "
                f"'{old_key}'. The ETags are equal"
            )
            return True
        finally:
            current_app.lock_manager.unlock(lock)


def untouched_key_still_claimed(key):
    """Return True if a surviving file record still claims this S3 key.

    This applies to the untouched key. Different records can share a
    key. For example, a replaced file can get a new key after a rename,
    or a second import can arrive on the same basename. A delete of a
    claimed key would strand the archive of the survivor behind a
    delete marker (the Bambi II incident, 2026-08). The caller must
    check AFTER its own deletes commit. Thus, the purged rows no longer
    count. An archive-only original (#292) claims its key too. So does
    the target of a pending move (#369)."""

    return (
        db.session.query(File.id)
        .filter(
            File.aws_key == key,
            File.aws_date_deleted.is_(None),
        )
        .first()
        is not None
        or db.session.query(ArchivedOriginal.id)
        .filter(ArchivedOriginal.aws_key == key)
        .first()
        is not None
        or db.session.query(File.id).filter(File.aws_transcoded_key == key).first()
        is not None
        or db.session.query(File.id).filter(File.aws_key_target == key).first()
        is not None
    )


# The status of a move that waits for a restore of its object. It is
# truthy, as the pending status of a download is. The job ran, and
# nothing failed. The restore notification starts the move again.

MOVE_WAITING = "waiting"


def _is_missing(error):
    """Return True if a botocore ClientError says that the object does not exist."""

    code = str(error.response.get("Error", {}).get("Code", ""))
    status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in ("404", "NoSuchKey", "NotFound") or status == 404


def _part_count(etag):
    """Return the part count of a multipart ETag, or None for a single-part object."""

    etag = (etag or "").strip('"')
    if "-" not in etag:
        return None
    try:
        return int(etag.rsplit("-", 1)[1])
    except ValueError:
        return None


def _copy_config(s3_client, bucket, key, head):
    """Return the TransferConfig that copies key in the part layout of its upload.

    The ETag of a multipart object depends on its parts. A copy with the
    same part size has the same ETag. Thus, the ETag proves the copy. A
    single-part object (5 GB or less) goes in 1 CopyObject request. The
    first part of a multipart object gives the part size. If S3 does not
    give it, the default layout applies, and only the size proves the
    copy."""

    from boto3.s3.transfer import TransferConfig

    size = head["ContentLength"]
    if _part_count(head.get("ETag")) is None:
        return TransferConfig(multipart_threshold=size + 1)
    try:
        part_size = s3_client.head_object(Bucket=bucket, Key=key, PartNumber=1)[
            "ContentLength"
        ]
    except Exception as error:
        current_app.logger.warning(
            f"'{key}' S3 did not give the part size ({error}). The copy has the "
            f"default part layout. Only the size can prove it"
        )
        return TransferConfig()
    return TransferConfig(
        multipart_threshold=min(part_size, size), multipart_chunksize=part_size
    )


def _copy_problem(original, copied):
    """Return the text of a difference between an object and its copy, or None.

    The sizes must be equal. The ETags must be equal when the 2 objects
    have the same part count."""

    if copied.get("ContentLength") != original.get("ContentLength"):
        return (
            f"has {copied.get('ContentLength')} bytes, not "
            f"{original.get('ContentLength')}"
        )
    if _part_count(copied.get("ETag")) == _part_count(original.get("ETag")) and (
        copied.get("ETag") != original.get("ETag")
    ):
        return f"has the ETag {copied.get('ETag')}, not {original.get('ETag')}"
    return None


def _drop_message(sqs_receipt_handle, note):
    """Delete the SQS message of a move, if the move came from a notification."""

    if sqs_receipt_handle:
        delete_sqs_message(aws_sqs_client(), sqs_receipt_handle, note=note)


def _file_queue_jobs():
    """Return the jobs of the file queue that wait, run, or wait for a retry."""

    queue = current_app.file_queue
    job_ids = (
        StartedJobRegistry(
            QUEUE_NAME["file"], connection=current_app.redis
        ).get_job_ids()
        + list(queue.job_ids)
        + ScheduledJobRegistry(queue=queue).get_job_ids()
    )
    jobs = []
    for job_id in job_ids:
        job = queue.fetch_job(job_id)
        if job is not None:
            jobs.append(job)
    return jobs


def _download_job_for(key):
    """Return True if a download of key waits, runs, or waits for a retry."""

    return any(
        job.func_name == "app.videos.download_task" and job.args and job.args[0] == key
        for job in _file_queue_jobs()
    )


def _download_after_move(file, source, target):
    """Download the moved archive if a restore for a download preceded the move.

    The restore notification of the old key went to the move. A request
    for a download records its time. If the library file is still
    missing, the download of the new key starts now. The copy is in
    STANDARD storage. Thus, it needs no restore."""

    requested = restore_requested_at(current_app.redis, source)
    if requested is None:
        return
    if os.path.isfile(os.path.join(current_app.config["LIBRARY_DIR"], file.file_path)):
        return
    record_restore_request(current_app.redis, target, requested)
    enqueue_task(
        "app.videos.download_task",
        target,
        os.path.basename(target),
        description=f"'{os.path.basename(target)}' — Downloading from AWS",
    )


def move_archive_object(file_id, sqs_receipt_handle=None):
    """Move the archive of a file to its pending target key (#369).

    The job holds the lock of the title. It reads the row and computes
    the key again. A later rename can have changed the target. Then the
    newest target applies.

    - The old object is missing: the target is cleared. The S3 sync
      reports the key. Return False.
    - The old object is in Deep Archive with no restored copy, or the
      copy expired: a Bulk restore is requested. Its notification starts
      this job again. Return MOVE_WAITING.
    - A download of the old key is queued, or the weekly S3 sync runs: the
      job waits 30 minutes. Return MOVE_WAITING.
    - The old object is readable: a server-side copy goes to the target.
      The size and the ETag of the copy are verified. Then the row names
      the new key, and the old key is deleted. Return True.

    aws_key names a real object during the whole move. sqs_receipt_handle
    is the restore notification that started the job. The job deletes it
    when the notification is handled."""

    from app.videos import acquire_lock_or_defer

    with app.app_context():
        config = current_app.config
        file = db.session.get(File, int(file_id))
        if file is None:
            current_app.logger.warning(
                f"File id {file_id} no longer exists. Its archive move stops"
            )
            _drop_message(sqs_receipt_handle, "archive move of a deleted record")
            return False
        if not file.aws_key_target and archive_drift(file) is None:
            current_app.logger.info(
                f"'{file.basename}' No archive move is pending. The job stops"
            )
            _drop_message(sqs_receipt_handle, "archive move with no target")
            return True

        # A row with no target and a key that differs from its rule lost
        # its target to a race. The job computes the target again.

        # The poll records the handle of a later message for this job.
        sqs_receipt_handle = _latest_receipt_handle(sqs_receipt_handle)
        lock = acquire_lock_or_defer(
            file.file_identifier(),
            config["UPLOAD_TASK_TIMEOUT"] * 1000,
            current_app.file_queue,
            "app.aws_storage.move_archive_object",
            minutes=(5, 15),
            timeout=config["UPLOAD_TASK_TIMEOUT"],
            description=f"'{file.basename}' — Moving the archive",
            args=(file.id,),
            kwargs=(
                {"sqs_receipt_handle": sqs_receipt_handle}
                if sqs_receipt_handle
                else None
            ),
            key=file.id,
        )
        if not lock:
            return False

        try:
            return _move_archive(file, sqs_receipt_handle)
        finally:
            current_app.lock_manager.unlock(lock)


def _sync_is_running():
    """Return True if the weekly S3 sync job runs on the maintenance queue."""

    queue = current_app.maintenance_queue
    for job_id in StartedJobRegistry(queue=queue).get_job_ids():
        job = queue.fetch_job(job_id)
        if job is not None and job.func_name == "app.videos.sync_aws_s3_storage_task":
            return True
    return False


def _wait_move(file, source, sqs_receipt_handle, waits_for):
    """Book the archive move again in 30 minutes, and return MOVE_WAITING.

    The retry keeps the receipt handle of the restore notification. Then
    the poll can extend the restore and the message, and the retry
    deletes the message when the move is done."""

    requeue(
        "app.aws_storage.move_archive_object",
        key=file.id,
        delay=timedelta(minutes=30),
        reason=f"waits for {waits_for}",
        args=(file.id,),
        kwargs=(
            {"sqs_receipt_handle": sqs_receipt_handle} if sqs_receipt_handle else None
        ),
        meta=(
            {"sqs_receipt_handle": sqs_receipt_handle, "archive_key": source}
            if sqs_receipt_handle
            else None
        ),
        description=f"'{file.basename}' — Moving the archive",
        level="info",
    )
    return MOVE_WAITING


def _move_archive(file, sqs_receipt_handle):
    """Do the work of move_archive_object under the lock of the title."""

    config = current_app.config
    db.session.refresh(file)
    drift = archive_drift(file)
    if drift is None:
        if file.aws_key_target:
            clear_archive_target(file)
            db.session.commit()
        current_app.logger.info(
            f"'{file.basename}' Each archive has the key of its name already"
        )
        _drop_message(sqs_receipt_handle, "archive move with no work")
        return True
    source, target = drift
    if file.aws_key_target != target:
        current_app.logger.info(
            f"'{file.basename}' The archive '{source}' now moves to '{target}'"
        )
        file.aws_key_target = target
    column = _target_column(file)

    # This commit records the newest target. It also ends the snapshot of
    # this session before the slow copy (MariaDB error 1020).

    db.session.commit()

    # A download of the old key reads it in parts. A delete in the middle
    # breaks the download. Thus, the move waits.

    if _download_job_for(source):
        return _wait_move(
            file, source, sqs_receipt_handle, f"the download of '{source}'"
        )

    # The weekly sync lists the bucket, then compares the rows with the
    # listing. A key that changes between the 2 steps looks absent or
    # extra. The sync waits for queued moves. A move that starts after
    # the idle check of the sync waits for the sync in return.

    if _sync_is_running():
        return _wait_move(file, source, sqs_receipt_handle, "the S3 sync")

    s3_client = aws_s3_client(with_retries=True)
    bucket = config["AWS_BUCKET"]
    try:
        head = s3_client.head_object(Bucket=bucket, Key=source)
    except botocore.exceptions.ClientError as error:
        if not _is_missing(error):
            raise
        current_app.logger.warning(
            f"'{file.basename}' The archive '{source}' is not in S3. The move to "
            f"'{target}' stops. The S3 sync reports the key"
        )
        clear_archive_target(file)
        db.session.commit()
        _drop_message(sqs_receipt_handle, "archive move of a missing object")
        return False

    if not _object_is_readable(head):
        if 'ongoing-request="true"' in (head.get("Restore") or ""):
            current_app.logger.info(
                f"'{file.basename}' The move of '{source}' waits for the "
                f"restore in progress"
            )
        else:
            current_app.logger.info(
                f"'{file.basename}' The archive '{source}' is in "
                f"{head.get('StorageClass') or 'cold storage'}. A Bulk restore "
                f"is requested for the move to '{target}'"
            )
            aws_restore(source, tier="Bulk", record_request=False)
        _drop_message(sqs_receipt_handle, "stale message")
        return MOVE_WAITING

    report_progress(get_current_job(), file.basename, "Moving the archive", None)
    current_app.logger.info(
        f"'{file.basename}' Copying the archive '{source}' to '{target}'"
    )
    s3_client.copy(
        {"Bucket": bucket, "Key": source},
        bucket,
        target,
        Config=_copy_config(s3_client, bucket, source, head),
    )
    copied = s3_client.head_object(Bucket=bucket, Key=target)
    problem = _copy_problem(head, copied)
    if problem:
        # The job fails loudly. The FailedJobRegistry and the badge of the
        # System page show it. The target stays. flask aws converge-keys
        # --apply requests the move again.
        s3_layout.delete(s3_client, bucket, target, copied.get("VersionId"))
        raise RuntimeError(
            f"'{file.basename}' The copy of '{source}' at '{target}' {problem}. "
            f"The archive keeps its key, and the copy is deleted"
        )

    # An edit can archive the file during the copy. It clears the target.
    # Read the row again from a new snapshot. If the archive changed, the
    # copy is not needed. Its own version goes, and an object of the edit
    # at the same key stays.

    db.session.commit()
    db.session.refresh(file)
    if getattr(file, column) != source or file.aws_key_target != target:
        current_app.logger.warning(
            f"'{file.basename}' The archive changed during the copy to "
            f"'{target}'. The copy is deleted"
        )
        if copied.get("VersionId") or not untouched_key_still_claimed(target):
            s3_layout.delete(s3_client, bucket, target, copied.get("VersionId"))
        return False

    proven = copied.get("ETag") == head.get("ETag")
    setattr(file, column, target)
    clear_archive_target(file)
    db.session.commit()
    current_app.logger.info(
        f"'{file.basename}' Moved the archive '{source}' to '{target}'. "
        + ("The ETags are equal" if proven else "The sizes are equal")
    )

    # A plain delete keeps the old version for the noncurrent window of
    # 180 days. That window rescued a lost archive before. Only the own
    # failed copy of a move goes by its version id.

    if not untouched_key_still_claimed(source):
        s3_layout.delete(s3_client, bucket, source)
    _download_after_move(file, source, target)
    _drop_message(sqs_receipt_handle, "archive move")

    # A conversion has a second archive. Its move comes next.

    if request_archive_move(file):
        db.session.commit()
    return True


def rearchive_untouched_object(file_id, new_key=None, path_retries=0):
    """Request the archive move of a file. This is the old task of a rename.

    A job of the old task can still wait in a queue. It names the task
    by this name. The move job computes the new key from the row. Thus,
    new_key and path_retries are not read (#369)."""

    with app.app_context():
        file = db.session.get(File, int(file_id))
        if file is None:
            return False
        moving = request_archive_move(file)
        db.session.commit()
        return moving


def aws_delete(key):
    """Delete an object from AWS S3 storage."""

    # This needs app.app_context(). Thus, the web application can call it
    # directly.
    with app.app_context():
        current_app.logger.info(f"Preparing to delete '{key}' from AWS...")
        s3_client = aws_s3_client()
        s3_layout.delete(s3_client, current_app.config["AWS_BUCKET"], key)
        current_app.logger.info(f"'{key}' deleted from AWS S3 storage")
        return datetime.now(timezone.utc)


# These are the aws_download results. A failure is a plain False. Each
# success status is truthy. Thus, a caller that only needs to know if
# the SQS message was handled can boolean-test the result. A caller that
# needs to know if a file arrived compares the result with
# DOWNLOAD_COMPLETE.

DOWNLOAD_COMPLETE = "complete"
DOWNLOAD_OBJECT_MISSING = "object-missing"
# CloudFront refused the signed URL for an object that S3 can read. Only
# a change to the key pair, the key group, or the WAF allowlist clears
# it. The SQS message is dropped, so the fault cannot loop.
DOWNLOAD_ACCESS_DENIED = "access-denied"
DOWNLOAD_RESTORE_PENDING = "restore-pending"

MAX_DOWNLOAD_RETRIES = 10

# The hourly SQS poll hides a restore message for 2 hours, and each poll
# extends it while its download waits or runs. With 10 minutes, the
# message was visible again before the next poll. Then the extension
# failed (MessageNotInflight), and the poll received it a second time.

SQS_VISIBILITY_SECONDS = 7200

# The days that a restored copy stays in S3 Standard. The hourly SQS poll
# renews a pending copy for the same days.

RESTORE_DAYS = 2

# The weekly S3 sync does not prune an object younger than this. The
# untouched/ upload of an import has no claim until the import finishes.

PRUNE_GRACE_DAYS = 8

# These client errors fail in the same way on each attempt (credentials,
# permissions, a missing bucket). Thus, a retry only delays the failure
# report.

NON_RETRYABLE_DOWNLOAD_ERRORS = (
    "AccessDenied",
    "InvalidAccessKeyId",
    "SignatureDoesNotMatch",
    "NoSuchBucket",
    "403",
)

# This is a seam for the tests. The retry backoff sleeps through this
# module attribute.

DOWNLOAD_RETRY_SLEEP = time.sleep


def _spend_download_retry(retry):
    """Spend 1 in-place download retry, and wait before the next attempt.

    The wait grows exponentially (1s, 2s, 4s, ...) with a cap at 60s."""

    retry -= 1
    if retry > 0:
        DOWNLOAD_RETRY_SLEEP(min(2 ** (MAX_DOWNLOAD_RETRIES - 1 - retry), 60))
    return retry


# CloudFront answers with these codes for a fault that a later attempt can
# clear: throttling, and a fault at the edge or at the origin.

RETRYABLE_CDN_STATUS_CODES = (429, 500, 502, 503, 504)

# An object in one of these storage classes needs a restore before a GET
# can read it.

COLD_STORAGE_CLASSES = ("GLACIER", "DEEP_ARCHIVE")

CDN_CHUNK_SIZE = 1024 * 1024

# The connect timeout and the read timeout of one CloudFront request, in
# seconds. A read timeout counts the wait for the next chunk, not the
# whole transfer.

CDN_TIMEOUT = (10, 120)

# This is a seam for the tests. The CloudFront transport fetches through
# this module attribute.

CDN_HTTP_GET = requests.get


class CdnDownloadError(Exception):
    """CloudFront refused a signed-URL download with an HTTP error status."""

    def __init__(self, status_code):
        super().__init__(f"CloudFront answered HTTP {status_code}")
        self.status_code = status_code


def cdn_download_enabled():
    """Return True if the restore downloads must go through CloudFront."""

    return bool(get_setting("AWS_DOWNLOAD_VIA_CDN"))


def missing_cdn_settings():
    """Return the names of the CloudFront settings that are not set."""

    return [
        name
        for name in ("CDN_DOMAIN", "CDN_KEY_PAIR_ID", "CDN_PRIVATE_KEY")
        if not current_app.config.get(name)
    ]


def cdn_settings_problem():
    """Return what is wrong with the CloudFront settings, or None.

    A setting that is not set, or a private key that this process cannot
    read or parse, is a configuration fault. The caller fails fast on it.
    A retry cannot clear it, and the redaction filter blanks the path of
    the key from a traceback (its name ends in KEY)."""

    missing = missing_cdn_settings()
    if missing:
        return f"{', '.join(missing)} not set"
    path = current_app.config["CDN_PRIVATE_KEY"]
    try:
        _cdn_private_key(path)
    except OSError as error:
        return f"CDN_PRIVATE_KEY cannot be read ({error.strerror})"
    except ValueError as error:
        return f"CDN_PRIVATE_KEY does not parse as a PEM private key ({error})"
    return None


_CDN_KEY_CACHE = {}


def _cdn_private_key(path):
    """Return the parsed private key, read from disk one time per change.

    Each download attempt signs a new URL. The key is parsed again only
    when the file changes."""

    from cryptography.hazmat.primitives import serialization

    stamp = os.stat(path).st_mtime_ns
    cached = _CDN_KEY_CACHE.get(path)
    if cached and cached[0] == stamp:
        return cached[1]
    with open(path, "rb") as key_file:
        private_key = serialization.load_pem_private_key(key_file.read(), password=None)
    _CDN_KEY_CACHE[path] = (stamp, private_key)
    return private_key


def cdn_signed_url(key, expires_in=None):
    """Sign a CloudFront URL for one object key.

    The URL is valid for expires_in seconds (default CDN_URL_EXPIRY). The
    signature is a canned policy that the local private key signs. Thus,
    the key never leaves this machine. Each download attempt signs a new
    URL. Thus, a short expiry costs nothing.
    """

    from botocore.signers import CloudFrontSigner
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    if expires_in is None:
        expires_in = current_app.config["CDN_URL_EXPIRY"]

    private_key = _cdn_private_key(current_app.config["CDN_PRIVATE_KEY"])

    # CloudFront accepts only RSA-SHA1 (PKCS#1 v1.5) signatures on a
    # signed URL. The signature protects a URL with a short life. It does
    # not protect stored data.

    def rsa_signer(message):
        return private_key.sign(message, padding.PKCS1v15(), hashes.SHA1())

    signer = CloudFrontSigner(current_app.config["CDN_KEY_PAIR_ID"], rsa_signer)
    url = f"https://{current_app.config['CDN_DOMAIN']}/{urllib.parse.quote(key)}"
    return signer.generate_presigned_url(
        url,
        date_less_than=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
    )


def cdn_download(key, destination, progress, expected_size=0, etag=None):
    """Fetch one object through CloudFront, and write it to destination.

    The progress callback gets the size of each chunk, as the boto3
    transfer callback does. A partial file at the destination from an
    earlier attempt is resumed with a Range request when the S3 size is
    known. Thus, a transport fault does not restart a large object from
    byte zero. A sidecar file holds the ETag of the object that the
    partial file came from. A resume needs the same ETag. Thus, a partial
    file of an older version of the key starts again from byte zero.
    Raise CdnDownloadError for an HTTP error status. Raise
    RuntimeError if the body length differs from the Content-Length
    header or from expected_size (the object size from the S3 API).
    Raise RuntimeError if neither size is known. The requests transport
    errors escape as they are.
    """

    url = cdn_signed_url(key)
    have = _partial_size(destination)
    headers = {}
    same_object = bool(etag) and _partial_etag(destination) == etag
    if have and expected_size and have < expected_size and same_object:
        headers["Range"] = f"bytes={have}-"
    else:
        have = 0

    # The signed URL is a bearer credential while it is valid. No log
    # line here holds it. A transport error from requests names the URL
    # in its message. The SecretRedactor filter on the app logger blanks
    # the Signature parameter of each record.

    with CDN_HTTP_GET(
        url, stream=True, timeout=CDN_TIMEOUT, headers=headers
    ) as response:
        if response.status_code == 206 and have:
            mode, written = "ab", have
            progress(have)
        elif response.status_code == 200:
            mode, written, have = "wb", 0, 0
            _write_partial_etag(destination, etag)
        else:
            raise CdnDownloadError(response.status_code)
        content_length = int(response.headers.get("Content-Length") or 0)
        if content_length:
            content_length += have
        with open(destination, mode) as output:
            for chunk in response.iter_content(chunk_size=CDN_CHUNK_SIZE):
                output.write(chunk)
                written += len(chunk)
                progress(len(chunk))

    # A body that ends early without a transport error must not become a
    # visible import. The S3 size catches it when the header is absent.
    # With no size known at all, the body cannot be trusted.

    if not content_length and not expected_size:
        raise RuntimeError(f"CloudFront sent {written} bytes for '{key}', size unknown")
    for label, size in (("Content-Length", content_length), ("S3", expected_size)):
        if size and written != size:
            raise RuntimeError(
                f"CloudFront sent {written} bytes for '{key}', {label} says {size}"
            )
    _remove_partial_etag(destination)


def _partial_size(destination):
    """Return the size of a partial download at the destination, or 0."""

    try:
        return os.path.getsize(destination)
    except OSError:
        return 0


def _partial_etag(destination):
    """Return the ETag of the object that a partial download came from, or None."""

    try:
        with open(f"{destination}.etag") as sidecar:
            return sidecar.read().strip() or None
    except OSError:
        return None


def _write_partial_etag(destination, etag):
    """Record the ETag of the object that a download writes to destination."""

    if not etag:
        _remove_partial_etag(destination)
        return
    try:
        with open(f"{destination}.etag", "w") as sidecar:
            sidecar.write(etag)
    except OSError:
        pass


def _remove_partial_etag(destination):
    """Remove the ETag sidecar of a download, if there is one."""

    try:
        os.remove(f"{destination}.etag")
    except OSError:
        pass


def _discard_partial(destination):
    """Remove a partial download. The import scan skips dotfiles, so an
    abandoned partial would stay invisible in the import directory."""

    try:
        os.remove(destination)
    except OSError:
        pass
    _remove_partial_etag(destination)


def _object_is_readable(head_response):
    """Return True if a GET can read the object now.

    That is the case for an object in a warm storage class, and for a
    cold object with a restored copy that has not expired. S3 can keep
    the Restore header for a while after the copy is gone. Thus, a past
    expiry-date counts as expired."""

    restore_status = head_response.get("Restore") or ""
    if 'ongoing-request="false"' in restore_status:
        return not _restore_has_expired(restore_status)
    if 'ongoing-request="true"' in restore_status:
        return False
    storage_class = head_response.get("StorageClass") or "STANDARD"
    return storage_class not in COLD_STORAGE_CLASSES


def _restore_has_expired(restore_status):
    """Return True if the expiry-date in a Restore header is in the past.

    A header without a readable expiry-date counts as not expired."""

    match = re.search(r'expiry-date="([^"]+)"', restore_status)
    if not match:
        return False
    try:
        expiry = email.utils.parsedate_to_datetime(match.group(1))
    except (TypeError, ValueError):
        return False
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return expiry <= datetime.now(timezone.utc)


def _resolve_missing_object(sqs_client, basename, sqs_receipt_handle):
    """Report an object that is not at AWS, and drop its SQS message.

    Return DOWNLOAD_OBJECT_MISSING, or False if the delete failed."""

    current_app.logger.info(f"'{basename}' doesn't exist in AWS S3")
    if sqs_receipt_handle:
        if not delete_sqs_message(sqs_client, sqs_receipt_handle):
            return False
    return DOWNLOAD_OBJECT_MISSING


def _refuse_access_fault(sqs_client, basename, sqs_receipt_handle):
    """Report a CloudFront refusal of an object that S3 can read.

    Only a change to the key pair, the key group, or the WAF allowlist
    clears this. A retry cannot. If the SQS message stayed, each
    redelivery would request a new restore and send a new email. Thus,
    the message is dropped, and the operator requests the restore again
    after the fix. Return DOWNLOAD_ACCESS_DENIED, or False if the delete
    failed."""

    current_app.logger.error(
        f"'{basename}' CloudFront refused the signed URL, but the object is "
        f"readable at S3; check the key pair, the key group, and the WAF "
        f"allowlist, then request the restore again"
    )
    if sqs_receipt_handle:
        if not delete_sqs_message(
            sqs_client, sqs_receipt_handle, note="CloudFront access fault"
        ):
            return False
    return DOWNLOAD_ACCESS_DENIED


def _defer_to_restore(sqs_client, key, basename, sqs_receipt_handle, head_response):
    """Hand the download to the next restore-completed notification.

    The restored copy expired before the download. Thus, the object is
    back in cold storage, and this SQS message is stale. Request a new
    restore, unless one is already in progress. Its completion
    notification triggers the download again. Return
    DOWNLOAD_RESTORE_PENDING, or False if the message delete failed."""

    restore_status = head_response.get("Restore") or ""
    if 'ongoing-request="true"' in restore_status:
        current_app.logger.info(
            f"'{basename}' restore is already in progress, "
            f"waiting for its completion notification"
        )
    else:
        current_app.logger.info(
            f"'{basename}' restored copy expired before download, "
            f"requesting a new restore"
        )
        aws_restore(key, record_request=False)

    if sqs_receipt_handle:
        if not delete_sqs_message(sqs_client, sqs_receipt_handle, note="stale message"):
            return False
    return DOWNLOAD_RESTORE_PENDING


def place_in_library(downloaded, library_path, expected_size, name):
    """Move a restored library file from the import directory into the library (#299).

    The download is a modified/ or transcoded/ object: the library file
    itself. It needs no import. Its size must match the S3 object. The
    copy goes to a hidden name beside the library file. Then 1 rename
    puts it in place. The track rows and the triage verdicts of the
    record stay."""

    from app.plex_library import enqueue_plex_analyze
    from app.videos import copy_with_progress

    size = os.path.getsize(downloaded)
    if expected_size is not None and size != expected_size:
        os.remove(downloaded)
        raise OSError(
            f"'{name}' restored {size} bytes, but the S3 object has {expected_size}"
        )
    directory = os.path.dirname(library_path)
    os.makedirs(directory, exist_ok=True)
    hidden = naming.hidden(library_path)
    try:
        copy_with_progress(downloaded, hidden, None, name, "Moving into the library")
        os.replace(hidden, library_path)
    except BaseException:
        if os.path.exists(hidden):
            os.remove(hidden)
        raise
    os.remove(downloaded)
    current_app.logger.info(f"'{name}' Restored straight into the library")
    enqueue_plex_analyze(library_path)


def aws_download(key, basename, sqs_receipt_handle=None, place_at=None):
    """Download an object from AWS S3 storage.

    Return DOWNLOAD_COMPLETE if the file arrived in the import directory.
    Return DOWNLOAD_OBJECT_MISSING if there is no such object at AWS.
    Return DOWNLOAD_RESTORE_PENDING if the object is in cold storage.
    Then the completion notification of the restore triggers the
    download again. Return DOWNLOAD_ACCESS_DENIED if CloudFront refused
    an object that S3 can read. These 4 values are truthy. They mean
    that the SQS message was handled. Return False if the retry budget was used up,
    or if the SQS message could not be deleted.

    When AWS_DOWNLOAD_VIA_CDN is set, the bytes come through CloudFront
    with a signed URL. The object size check and the restore-state
    checks still use the S3 API.
    """

    # The retry constants still live in app.videos. Import them lazily.
    # Thus, the module import direction stays videos to aws_storage.

    from app.videos import TRANSIENT_COPY_ERRNOS

    retry = MAX_DOWNLOAD_RETRIES

    # Rename "(edition-foo bar baz)" to "{edition-foo bar baz}".
    if "(edition-" in basename:
        basename = re.sub(
            r"\(edition\-(?P<edition>.+)\)", "{edition-\\g<edition>}", basename
        )

    via_cdn = cdn_download_enabled()
    if via_cdn:
        problem = cdn_settings_problem()
        if problem:
            # A silent fallback to S3 egress would spend money that the
            # operator chose not to spend. The SQS message stays in the
            # queue for a later delivery.

            current_app.logger.error(
                f"'{basename}' CloudFront download is enabled, but "
                f"{problem}; giving up"
            )
            return False

    source = "AWS CloudFront" if via_cdn else "AWS S3 storage"
    current_app.logger.info(f"'{basename}' downloading from {source}")

    bucket = current_app.config["AWS_BUCKET"]
    # A restore into the library is not imported. Its library basename is
    # not unique: many films have a "Restoration Demonstration.mkv". The
    # untouched name is not sufficient either. The 2 archives of a
    # conversion share it. Thus, the staging name is the downloaded key,
    # with its prefix: 1 file for each object. An import download keeps
    # its name. The rename to that name starts the import.

    if place_at:
        staged_name = f".restore-{key.replace('/', '-')}"
    else:
        staged_name = naming.hidden(basename)
    destination = os.path.join(current_app.config["IMPORT_DIR"], staged_name)
    s3_client = aws_s3_client()
    sqs_client = aws_sqs_client()

    while retry > 0:
        try:
            progress = DownloadProgressPercentage(s3_client, bucket, key, basename)
            if via_cdn:
                cdn_download(
                    key,
                    destination,
                    progress,
                    expected_size=progress.size,
                    etag=progress.etag,
                )
            else:
                s3_client.download_file(bucket, key, destination, Callback=progress)

        # Do not resume if the file does not exist in AWS.
        except botocore.exceptions.ClientError as error:
            # boto3 signals a missing object through Error.Code ("404" or
            # "NoSuchKey"). Keep the HTTP status code check as a fallback.
            error_code = str(error.response.get("Error", {}).get("Code", ""))
            status_code = error.response.get("ResponseMetadata", {}).get(
                "HTTPStatusCode"
            )
            if error_code in ("404", "NoSuchKey") or status_code == 404:
                _discard_partial(destination)
                return _resolve_missing_object(sqs_client, basename, sqs_receipt_handle)

            elif error_code == "InvalidObjectState":
                try:
                    head_response = s3_client.head_object(Bucket=bucket, Key=key)
                except Exception:
                    # A failed status check spends a retry, as each other
                    # error does. It does not exit the loop.

                    current_app.logger.error(traceback.format_exc())
                    retry = _spend_download_retry(retry)
                    continue
                _discard_partial(destination)
                return _defer_to_restore(
                    sqs_client, key, basename, sqs_receipt_handle, head_response
                )

            elif error_code in NON_RETRYABLE_DOWNLOAD_ERRORS or status_code == 403:
                current_app.logger.error(traceback.format_exc())
                current_app.logger.error(
                    f"'{basename}' download failed with non-retryable error "
                    f"'{error_code or status_code}'; giving up"
                )
                retry = 0

            else:
                current_app.logger.error(traceback.format_exc())
                retry = _spend_download_retry(retry)

        except CdnDownloadError as error:
            if error.status_code == 404:
                _discard_partial(destination)
                return _resolve_missing_object(sqs_client, basename, sqs_receipt_handle)

            elif error.status_code == 403:
                # A 403 from CloudFront has 4 possible causes. The object
                # does not exist (the bucket policy grants no list right,
                # so S3 says 403, not 404). The object is back in cold
                # storage. The signature or the key group is wrong. The
                # WAF allowlist does not hold this address. The S3 API
                # tells the first two apart from the other two.

                try:
                    head_response = s3_client.head_object(Bucket=bucket, Key=key)
                except botocore.exceptions.ClientError as head_error:
                    head_code = str(
                        head_error.response.get("Error", {}).get("Code", "")
                    )
                    head_status = head_error.response.get("ResponseMetadata", {}).get(
                        "HTTPStatusCode"
                    )
                    if (
                        head_code in ("404", "NoSuchKey", "NotFound")
                        or head_status == 404
                    ):
                        _discard_partial(destination)
                        return _resolve_missing_object(
                            sqs_client, basename, sqs_receipt_handle
                        )
                    current_app.logger.error(traceback.format_exc())
                    retry = _spend_download_retry(retry)
                    continue
                except Exception:
                    current_app.logger.error(traceback.format_exc())
                    retry = _spend_download_retry(retry)
                    continue
                if _object_is_readable(head_response):
                    _discard_partial(destination)
                    return _refuse_access_fault(
                        sqs_client, basename, sqs_receipt_handle
                    )
                else:
                    _discard_partial(destination)
                    return _defer_to_restore(
                        sqs_client, key, basename, sqs_receipt_handle, head_response
                    )

            elif error.status_code in RETRYABLE_CDN_STATUS_CODES:
                current_app.logger.error(f"'{basename}' {error}, retrying")
                retry = _spend_download_retry(retry)

            else:
                current_app.logger.error(f"'{basename}' {error}; giving up")
                retry = 0

        except requests.exceptions.RequestException:
            # A transport fault (connection, timeout, a broken body) spends
            # a retry. The next attempt signs a new URL.

            current_app.logger.error(traceback.format_exc())
            retry = _spend_download_retry(retry)

        except OSError as e:
            if e.errno in TRANSIENT_COPY_ERRNOS:
                # A dead import volume fails immediately. Thus, the whole
                # in-place retry budget would be wasted on it. Delete the
                # partial download. Let the caller defer until the mount
                # settles.

                try:
                    os.remove(destination)
                except OSError:
                    pass
                raise
            current_app.logger.error(traceback.format_exc())
            retry = _spend_download_retry(retry)

        except Exception:
            current_app.logger.error(traceback.format_exc())
            retry = _spend_download_retry(retry)

        else:
            current_app.logger.info(f"'{basename}' downloaded from {source}")

            if place_at:
                place_in_library(destination, place_at, progress.size, basename)
            else:
                os.rename(
                    destination,
                    os.path.join(current_app.config["IMPORT_DIR"], f"{basename}"),
                )

            if sqs_receipt_handle:
                if not delete_sqs_message(sqs_client, sqs_receipt_handle):
                    return False

            return DOWNLOAD_COMPLETE

    current_app.logger.error(
        f"'{basename}' could not be downloaded from {source}; giving up"
    )

    _discard_partial(destination)
    return False


def aws_restore(key, tier="Standard", record_request=True):
    """Request a restore of a file at AWS from Glacier status for a download.

    The request time goes to Redis. Then download_task can tell if an
    import replaced the file during the restore. A request again for an
    expired restored copy keeps the time of the first request.
    """

    with app.app_context():
        try:
            s3_client = aws_s3_client(with_retries=True)

            # Make sure that the key exists in the AWS bucket.

            response = s3_client.list_objects(
                Bucket=current_app.config["AWS_BUCKET"], Prefix=key, MaxKeys=1
            )

            # The listing has no "Contents" key if nothing matches.

            contents = response.get("Contents")

            # The key exists.

            if contents and contents[0].get("Key"):
                head_response = s3_client.head_object(
                    Bucket=current_app.config["AWS_BUCKET"], Key=key
                )

                # S3 keeps the Restore header for a while after the
                # restored copy expires. A test of the header alone sent
                # an expired copy to a download that failed and asked for
                # a restore again, without a delay.

                # S3 leaves StorageClass out of a HEAD for some objects.
                # The listing has it.

                head_response = {
                    **head_response,
                    "StorageClass": head_response.get("StorageClass")
                    or contents[0].get("StorageClass"),
                }
                if contents[0].get("StorageClass") == "STANDARD" or (
                    _object_is_readable(head_response)
                ):
                    current_app.logger.info(
                        f"'{key}' doesn't need to be restored; attempting to download"
                    )
                    enqueue_task(
                        "app.videos.download_task",
                        key,
                        os.path.basename(key),
                        description=f"'{os.path.basename(key)}'",
                        at_front=True,
                    )
                    return

                else:
                    response = s3_client.restore_object(
                        Bucket=current_app.config["AWS_BUCKET"],
                        Key=key,
                        RestoreRequest={
                            "Days": RESTORE_DAYS,
                            "GlacierJobParameters": {"Tier": tier},
                        },
                    )
                    current_app.logger.info(
                        f"Requested '{key}' to be restored for {RESTORE_DAYS} "
                        f"day(s) using tier '{tier}'"
                    )
                    if record_request:
                        record_restore_request(current_app.redis, key)

            else:
                current_app.logger.warning(
                    f"'{key}' does not exist in AWS S3 storage, cannot restore"
                )

        # Only a botocore ClientError instance has a .response attribute.
        # Let each other exception propagate as it is.

        except botocore.exceptions.ClientError as e:
            if e.response["Error"]["Code"] == "RestoreAlreadyInProgress":
                current_app.logger.info(
                    f"'{key}' is already in process of being restored"
                )

            else:
                current_app.logger.error(e)
                raise

        else:
            return


def aws_upload(
    file_path,
    key_prefix="",
    key_name=None,
    force_upload=False,
    ignore_etag=False,
    storage_class="STANDARD",
):
    """Search for a file in AWS S3. Upload it if it is missing or different."""

    from app.videos import TRANSIENT_COPY_ERRNOS

    if not os.path.isfile(file_path):
        current_app.logger.error(
            f"'{file_path}' can't be uploaded to AWS since it's not a file!"
        )

        # Raise an error. Do not return None. Each caller unpacks the return
        # value as a (key, date_uploaded, filesize_bytes) tuple.

        raise FileNotFoundError(
            f"'{file_path}' can't be uploaded to AWS since it's not a file"
        )

    if key_name:
        key = sanitize_s3_key(key_name)

    else:
        key = sanitize_s3_key(os.path.basename(file_path))

    key = os.path.join(key_prefix, key)

    s3_client = aws_s3_client(with_retries=True)

    # Find out if the key already exists in the AWS bucket.

    response = s3_client.list_objects(
        Bucket=current_app.config["AWS_BUCKET"], Prefix=key, MaxKeys=1
    )

    # If the key already exists, compare the local and the remote ETags. If
    # the ETags match, the files are the same, and a new upload is not
    # necessary. If the IGNORE_ETAGS flag is set, compare only the file and
    # key names, not their data.

    if not force_upload and not current_app.config["FORCE_UPLOAD"]:
        # Look for an object with this exact key. The listing is a Prefix
        # search. Thus, it can return a different, longer key instead.

        remote_etag = None
        date_uploaded = None
        remote_size = None
        for object in response.get("Contents") or []:
            if object.get("Key") == key:
                remote_etag = object.get("ETag").replace('"', "")
                date_uploaded = object.get("LastModified")
                remote_size = object.get("Size")

        if remote_etag is not None:
            if ignore_etag or current_app.config["IGNORE_ETAGS"]:
                current_app.logger.info(
                    f"'{file_path}' matches '{key}' and ETags are ignored, "
                    f"no need to re-upload"
                )
                return key, date_uploaded, remote_size

            local_etag = calculate_etag(file_path)
            if local_etag == remote_etag:
                current_app.logger.info(
                    f"'{file_path}' is the same as '{key}', no need to re-upload"
                )
                return key, date_uploaded, remote_size

            current_app.logger.info(
                f"Local ETag '{local_etag}' ('{file_path}') "
                f"differs from remote ETag '{remote_etag}' ('{key}'), "
                f"re-uploading to AWS"
            )

        else:
            current_app.logger.info(
                f"'s3://{os.path.join(current_app.config['AWS_BUCKET'], key)}' "
                f"doesn't exist at AWS"
            )

    current_app.logger.info(
        f"Uploading '{file_path}' to "
        f"'s3://{os.path.join(current_app.config['AWS_BUCKET'], key)}'"
    )

    # Upload the file to AWS S3 storage.

    # The failure handling logic comes from
    # https://codeflex.co/python-s3-multipart-file-upload-with-metadata-and-progress-indicator/
    # That post shows that botocore.exceptions.ClientError and
    # boto3.exceptions.S3UploadFailedError return different error formats.

    MAX_RETRY_COUNT = 10
    retry = MAX_RETRY_COUNT

    while retry > 0:
        try:
            response = s3_client.upload_file(
                file_path,
                current_app.config["AWS_BUCKET"],
                key,
                ExtraArgs={"StorageClass": storage_class},
                Callback=UploadProgressPercentage(file_path),
            )
            retry = 0

        except boto3.exceptions.S3UploadFailedError as e:
            retry = retry - 1
            if "BadDigest" in str(e):
                current_app.logger.warning(e)
                current_app.logger.warning(
                    f"'{file_path}' Retrying upload, "
                    f"this is retry {MAX_RETRY_COUNT - retry} out of {MAX_RETRY_COUNT}"
                )

            else:
                current_app.logger.error(e)
                raise

        # A failed upload never moves the file. Many callers pass the
        # library file itself (an archive of a changed file, the #299
        # backfill). A network fault, an S3 error, or a job timeout would
        # move a film out of the library. The import catches the error
        # and rejects its own source. The archive callers mark the archive
        # stale for the S3 sync.

        except OSError as e:
            if e.errno in TRANSIENT_COPY_ERRNOS:
                current_app.logger.error(
                    f"'{file_path}' upload failed on a transient filesystem "
                    f"error ({e.strerror}); leaving the file where it is"
                )
            raise

        else:
            current_app.logger.info(f"Uploaded '{file_path}' to AWS")
            return key, datetime.now(timezone.utc), os.path.getsize(file_path)

    current_app.logger.error(
        f"Tried to upload '{file_path}' {str(MAX_RETRY_COUNT)} times but couldn't!"
    )

    # Raise an error at the end. Do not return None. Each caller unpacks
    # the return value as a (key, date_uploaded, filesize_bytes) tuple.

    raise RuntimeError(
        f"Unable to upload '{file_path}' to AWS after {MAX_RETRY_COUNT} attempts"
    )


def mark_archive_stale(file_id, reason=""):
    """Record that the S3 archive of a file is older than its local copy.

    This commits in its own transaction. Each caller is a failure path
    that rolls back. A rollback would discard the marker. Then the loss
    would stay as hidden as before. The key still exists, and its date
    is the date of the previous upload. Thus, nothing that inspects the
    row can tell that the archive is behind.
    """

    from app.models import File

    try:
        file = File.query.filter_by(id=file_id).first()
        if file is None:
            return False

        file.aws_stale = True
        db.session.commit()

    except Exception:
        db.session.rollback()
        current_app.logger.error(traceback.format_exc())
        return False

    current_app.logger.warning(
        f"The S3 archive for '{file.basename}' is now stale ({reason}); "
        f"marked for repair, which will run once the file is readable again"
    )
    return True


def calculate_etag(file_path):
    """Calculate the unique ETag for a local file."""

    basename = os.path.basename(file_path)
    current_app.logger.info(f"'{basename}' Calculating ETag")
    job = get_current_job()

    file_size = os.path.getsize(file_path)
    if file_size < EIGHT_MEGABYTES:
        # The file is smaller than 8 MB. Read it in 1 step. Return its MD5
        # hash.

        with open(file_path, "rb") as f:
            md5_hash = hashlib.md5(f.read())

        return md5_hash.hexdigest()

    else:
        md5_digests = []

        # Read the file in the part size of the upload. That is 8 MB, but
        # s3transfer doubles it for a file of more than 10,000 parts (78
        # GiB). With 8 MB parts, the ETag of such a file never matched, and
        # each check uploaded the whole file again.

        part_size = ChunksizeAdjuster().adjust_chunksize(EIGHT_MEGABYTES, file_size)
        with open(file_path, "rb") as f:
            previous_percent = None
            for chunk in iter(lambda: f.read(part_size), b""):
                # Collect the MD5 hashes.
                md5_digests.append(hashlib.md5(chunk).digest())
                percent = int((f.tell() / file_size) * 100)
                if previous_percent != percent:
                    current_app.logger.info(
                        f"'{basename}' Calculating ETag: {percent}%"
                    )
                    previous_percent = percent
                report_progress(job, basename, "Calculating ETag", percent)

        # Get an MD5 hash of the concatenated hashes. Append the number of
        # parts. For example, "c7c2300fd47954c421d5fe0bc7910ca3-64":
        # c7c2300fd47954c421d5fe0bc7910ca3 is the hash of the concatenated
        # MD5 hashes, and the uploaded file had 64 parts (MD5 hashes).

        return (
            hashlib.md5(b"".join(md5_digests)).hexdigest() + "-" + str(len(md5_digests))
        )


def get_matching_s3_objects(bucket, prefix="", suffix=""):
    """Iterate through the objects in S3 storage.

    https://alexwlchan.net/2019/07/listing-s3-keys/ (STE: exempt, licence
    notice)

    Copyright (c) 2012-2019 Alex Chan

    Permission is hereby granted, free of charge, to any person obtaining a
    copy of this software and associated documentation files (the "Software"),
    to deal in the Software without restriction, including without limitation
    the rights to use, copy, modify, merge, publish, distribute, sublicense,
    and/or sell copies of the Software, and to permit persons to whom the Software
    is furnished to do so, subject to the following conditions:

    The above copyright notice and this permission notice shall be included in
    all copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
    THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR
    OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE,
    ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
    OTHER DEALINGS IN THE SOFTWARE.
    """

    s3 = aws_s3_client()
    paginator = s3.get_paginator("list_objects_v2")
    kwargs = {"Bucket": bucket}
    if isinstance(prefix, str):
        prefixes = (prefix,)
    else:
        prefixes = prefix

    for key_prefix in prefixes:
        kwargs["Prefix"] = key_prefix
        for page in paginator.paginate(**kwargs):
            try:
                contents = page["Contents"]
            except KeyError:
                break
            for obj in contents:
                key = obj["Key"]
                if key.endswith(suffix):
                    yield obj


def sanitize_s3_key(key):
    """Sanitize the key name. Remove the characters that cause problems.

    See https://docs.aws.amazon.com/AmazonS3/latest/dev/UsingMetadata.html
    """

    from app.videos import sanitize_string

    # fmt: off
    aws_bad_chars  = [   "&",  "$",   "@",  "=", ";", ":", "+", ",", "?", "\\", "{", "^", "}", "%", "`", '"', ">", "~", "<", "#", "|"]
    aws_good_chars = [" and ",  "", " at ", "-", "-", "-", " ",  "",  "",  " ", "(",  "", ")",  "", "'",  "",  "", "-",  "",  "",  ""]
    # fmt: on

    key = os.path.normpath(key)
    key_components = key.split(os.sep)
    key = os.path.join(
        *[
            sanitize_string(component, aws_bad_chars, aws_good_chars)
            for component in key_components
        ]
    )
    return key


# This is the app instance of this process. Fitzflix resolves it lazily.
# Thus, a process that already has an application can import this module
# and not build a second application.

app = LocalProxy(get_app)
