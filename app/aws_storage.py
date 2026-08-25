"""The AWS storage layer (the first strangler slice out of videos.py).

Everything that talks to S3 and SQS: the untouched-original archive
uploads, restores and downloads, the weekly storage audit, the SQS
restore-notification poller, and the key plumbing (etags, key
sanitizing, the untouched-key handoff guard).

rq job names are strings and live in Redis, so app.videos RE-EXPORTS
every name here — enqueue sites and stored jobs keep saying
"app.videos.upload_task" and keep resolving. Names still living in
app.videos are imported lazily inside functions, never at module
level, so the import direction stays videos → aws_storage.
"""

import csv
import hashlib
import io
import json
import os
import re
import threading
import time
import traceback
import urllib.parse

from datetime import datetime, timedelta, timezone

import boto3
import botocore
import rq

from botocore.client import Config
from rq import get_current_job
from rq.registry import StartedJobRegistry

from flask import current_app, render_template
from werkzeug.local import LocalProxy

from app import db, get_app, retry_job_id, safe_job_id
from app.email import task_send_email as send_email
from app.models import (
    File,
    Movie,
    RefQuality,
    TVSeries,
    User,
    movie_file_rank,
    tv_file_rank,
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


def delete_sqs_message(sqs_client, receipt_handle, note="message"):
    """Delete a message from the SQS queue; returns False when deletion fails."""

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


class UploadProgressPercentage(object):
    """Return the upload progress as a callback when uploading a file to AWS S3."""

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

            # Report a zero-byte file as already complete rather than divide by zero

            percent = int((self._seen_so_far / self._size) * 100) if self._size else 100

            # Transfer callbacks fire far more often than tool output lines,
            # so both the log line and the job-meta write wait for the
            # percentage to actually change

            if percent == self._previous_percent:
                return
            self._previous_percent = percent

            app.logger.info(
                f"'{os.path.basename(self._file_path)}' Uploading to AWS: {percent}%"
            )
            if self._job:
                self._job.meta["description"] = (
                    f"'{os.path.basename(self._file_path)}' — Uploading to AWS"
                )
                self._job.meta["progress"] = percent
                self._job.save_meta()


class DownloadProgressPercentage(object):
    """Return the download progress as a callback when downloading a file from AWS S3."""

    def __init__(self, client, bucket, key, basename):
        self._file_path = basename
        self._size = client.head_object(Bucket=bucket, Key=key).get("ContentLength", 0)
        app.logger.info(f"'{basename}' Download size: {self._size} bytes")
        self._seen_so_far = 0
        self._previous_percent = None
        self._lock = threading.Lock()
        self._job = rq.get_current_job()

    def __call__(self, bytes_amount):
        with self._lock:
            self._seen_so_far += bytes_amount

            # Report a zero-byte object as already complete rather than divide by zero

            percent = int((self._seen_so_far / self._size) * 100) if self._size else 100

            # Transfer callbacks fire far more often than tool output lines,
            # so both the log line and the job-meta write wait for the
            # percentage to actually change

            if percent == self._previous_percent:
                return
            self._previous_percent = percent

            app.logger.info(
                f"'{os.path.basename(self._file_path)}' Downloading from AWS: {percent}%"
            )
            if self._job:
                self._job.meta["description"] = (
                    f"'{os.path.basename(self._file_path)}' — Downloading from AWS"
                )
                self._job.meta["progress"] = percent
                self._job.save_meta()


def sync_aws_s3_storage_task():
    """Add files to AWS, and remove files that aren't in the library."""

    with app.app_context():
        # Only sync when every queue is idle: a file that's mid-import,
        # mid-upload, or still waiting on database writes can exist at AWS
        # without its final database record, and the prune below would see it
        # as an extra file and delete it

        job = get_current_job()
        busy = []
        for queue_name, queue in (
            ("fitzflix-import", current_app.import_queue),
            ("fitzflix-transcode", current_app.transcode_queue),
            ("fitzflix-file-operation", current_app.file_queue),
            ("fitzflix-sql", current_app.sql_queue),
            ("fitzflix-user-request", current_app.request_queue),
            # The maintenance queue runs the hourly import sweep, which can
            # feed new files into the import pipeline mid-sync
            ("fitzflix-maintenance", current_app.maintenance_queue),
        ):
            started = StartedJobRegistry(
                queue_name, connection=current_app.redis
            ).get_job_ids()

            # This task itself is in the user-request started registry

            if job:
                started = [job_id for job_id in started if job_id != job.id]

            count = len(queue.job_ids) + len(started)
            if count:
                busy.append(f"{queue_name}: {count}")

        if busy:
            current_app.request_queue.enqueue_in(
                timedelta(minutes=5),
                "app.videos.sync_aws_s3_storage_task",
                job_timeout="24h",
                job_id=safe_job_id("retry:sync_aws_s3_storage_task"),
                result_ttl=86400,
                description="Syncing files with AWS S3 storage",
                at_front=True,
            )
            current_app.logger.info(
                f"Waiting 5 minutes for other tasks to finish "
                f"before attempting to sync ({', '.join(busy)})"
            )
            return True

        try:
            job = get_current_job()

            # Map each remote key to its object size, so file records can be
            # backfilled with the exact size that AWS bills for restores

            s3_objects = {
                object["Key"]: object["Size"]
                for object in get_matching_s3_objects(
                    app.config["AWS_BUCKET"],
                    prefix=f"{app.config['AWS_UNTOUCHED_PREFIX']}/",
                )
            }
            s3_keys = list(s3_objects)

            files = File.query.all()

            movie_rank = (
                db.session.query(
                    File.id,
                    movie_file_rank(),
                )
                .join(Movie, (Movie.id == File.movie_id))
                .join(RefQuality, (RefQuality.id == File.quality_id))
                .subquery()
            )

            tv_rank = (
                db.session.query(
                    File.id,
                    tv_file_rank(),
                )
                .join(TVSeries, (TVSeries.id == File.series_id))
                .join(RefQuality, (RefQuality.id == File.quality_id))
                .subquery()
            )

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
                .order_by(RefQuality.preference.asc(), File.aws_untouched_key.asc())
                .all()
            )

            current_app.logger.info(f"Evaluating {len(files)} files for S3 sync")

            inventory_export = []
            orphaned_files = []
            unreferenced_files = []

            for i, (file, rank) in enumerate(files):
                if job:
                    job.meta["description"] = "Queuing local files for S3 upload"
                    job.meta["progress"] = int((i / len(files)) * 100)
                    job.save_meta()

                file_path = os.path.join(
                    current_app.config["LIBRARY_DIR"], file.file_path
                )

                # If the file...

                # ...is not in S3 but exists in the filesystem...
                if (
                    file.aws_untouched_key not in s3_keys
                    or file.aws_untouched_date_uploaded == None
                    or file.aws_untouched_stale
                ) and os.path.isfile(file_path):

                    # ...then queue for upload to S3

                    current_app.logger.info(
                        f"'{file.aws_untouched_key}' Queuing for upload to AWS"
                    )

                    current_app.file_queue.enqueue(
                        "app.videos.upload_task",
                        args=(
                            file.id,
                            current_app.config["AWS_UNTOUCHED_PREFIX"],
                            True,
                        ),
                        job_timeout=current_app.config["LOCALIZATION_TASK_TIMEOUT"],
                        description=f"'{file.basename}'",
                    )

                # ...exists in s3...
                elif file.aws_untouched_key in s3_keys:

                    # ...then add it to the inventory...

                    current_app.logger.info(
                        f"'{file.aws_untouched_key}' Exists in AWS S3; rank {rank}"
                    )

                    # Record the object's actual size if we don't have it yet
                    # or if it has changed since it was recorded

                    remote_size = s3_objects[file.aws_untouched_key]
                    if file.aws_untouched_filesize_bytes != remote_size:
                        file.aws_untouched_filesize_bytes = remote_size

                    if rank == 1:
                        inventory_export.append(
                            [current_app.config["AWS_BUCKET"], file.aws_untouched_key]
                        )

                        # ...and queue for restore if it doesn't exist locally

                        if not os.path.isfile(file_path):
                            current_app.logger.info(
                                f"'{file.aws_untouched_key}' does not exist in the local library"
                            )
                            aws_restore(file.aws_untouched_key, tier="Bulk")

                # ...is not in S3 and does not exist in the filesystem...
                elif file.aws_untouched_key not in s3_keys and not os.path.isfile(
                    file_path
                ):

                    # ...then flag as orphaned file

                    current_app.logger.info(
                        f"'{file.aws_untouched_key}' has no associated files"
                    )
                    orphaned_files.append([file.id, file.untouched_basename])

            # Persist any backfilled AWS object sizes

            db.session.commit()

            current_app.logger.info(f"Orphaned files: {orphaned_files}")

            # Create a CSV of the best files and upload to the S3 bucket;
            # if we should ever need to do a bulk restoration of our library, we can
            # use this file to perform a restore of all our best files via
            # S3 Bulk Operation

            if inventory_export:
                f = io.StringIO()
                inventory_writer = csv.writer(f, quoting=csv.QUOTE_ALL)
                for file_object in inventory_export:
                    inventory_writer.writerow(file_object)
                inventory_file = bytes(f.getvalue(), encoding="utf-8")
                f.close()
                client = aws_s3_client(with_retries=True)
                client.put_object(
                    Body=inventory_file,
                    Bucket=current_app.config["AWS_BUCKET"],
                    Key="inventory/rank_1.csv",
                )

            # Delete remote S3 files that aren't in Fitzflix

            aws_untouched_keys = {
                aws_untouched_key
                for (aws_untouched_key,) in db.session.query(
                    File.aws_untouched_key
                ).all()
            }

            # The rename-skew tripwire: an ACTIVE claim with no
            # matching object means a restore would 404 — the class of
            # silent damage the Aug 17 audit found 1,184 deep. Report
            # it loudly here every week so it can never accumulate

            s3_key_set = set(s3_keys)
            dangling_claims = sorted(
                key
                for (key,) in db.session.query(File.aws_untouched_key)
                .filter(File.aws_untouched_key.isnot(None))
                .filter(File.aws_untouched_date_deleted.is_(None))
                if key not in s3_key_set
            )
            if dangling_claims:
                admin_user = User.query.filter(User.admin == True).first()
                send_email(
                    "Fitzflix - Archive keys missing from AWS S3!",
                    sender=("Fitzflix", current_app.config["SERVER_EMAIL"]),
                    recipients=[admin_user.email],
                    text_body=(
                        "These file records claim untouched archive keys "
                        "that don't exist in S3 — a restore would fail:\n\n"
                        + "\n".join(dangling_claims)
                    ),
                    html_body=(
                        "<p>These file records claim untouched archive "
                        "keys that don't exist in S3 — a restore would "
                        "fail:</p><ul>"
                        + "".join(f"<li>{key}</li>" for key in dangling_claims)
                        + "</ul>"
                    ),
                )

            for i, remote_key in enumerate(s3_keys):
                if job:
                    job.meta["description"] = "Pruning extra files from AWS S3 storage"
                    job.meta["progress"] = int((i / len(s3_keys)) * 100)
                    job.save_meta()

                if (
                    remote_key not in aws_untouched_keys
                    and remote_key != f"{app.config['AWS_UNTOUCHED_PREFIX']}/"
                ):
                    unreferenced_files.append(remote_key)
                    aws_delete(remote_key)

            if unreferenced_files:
                admin_user = User.query.filter(User.admin == True).first()
                send_email(
                    "Fitzflix - Deleted unreferenced AWS files",
                    sender=("Fitzflix", current_app.config["SERVER_EMAIL"]),
                    recipients=[admin_user.email],
                    text_body=render_template(
                        "email/unreferenced_files.txt",
                        user=admin_user.email,
                        unreferenced_files=unreferenced_files,
                    ),
                    html_body=render_template(
                        "email/unreferenced_files.html",
                        user=admin_user.email,
                        unreferenced_files=unreferenced_files,
                    ),
                )

            if orphaned_files:
                admin_user = User.query.filter(User.admin == True).first()
                send_email(
                    "Fitzflix - Orphaned file records found!",
                    sender=("Fitzflix", current_app.config["SERVER_EMAIL"]),
                    recipients=[admin_user.email],
                    text_body=render_template(
                        "email/orphaned_files.txt",
                        user=admin_user.email,
                        orphaned_files=orphaned_files,
                    ),
                    html_body=render_template(
                        "email/orphaned_files.html",
                        user=admin_user.email,
                        orphaned_files=orphaned_files,
                    ),
                )

            # Queue local files in the library folders but aren't in Fitzflix for importing

            library = []
            for path, subdirs, local_files in os.walk(
                current_app.config["MOVIE_LIBRARY"]
            ):
                for name in local_files:
                    if name.startswith(
                        ("cover", "default", "folder", "movie", "poster")
                    ) and name.endswith(("jpg", "jpeg", "png", "tbn")):
                        continue

                    if not name.startswith(".") and "@eaDir" not in path:
                        library_file = os.path.join(path, name)
                        file_path = os.path.relpath(
                            library_file, current_app.config["LIBRARY_DIR"]
                        )
                        library.append((library_file, file_path))

            for path, subdirs, local_files in os.walk(current_app.config["TV_LIBRARY"]):
                for name in local_files:
                    if name.startswith(
                        ("cover", "default", "folder", "movie", "poster")
                    ) and name.endswith(("jpg", "jpeg", "png", "tbn")):
                        continue

                    if not name.startswith(".") and "@eaDir" not in path:
                        library_file = os.path.join(path, name)
                        file_path = os.path.relpath(
                            library_file, current_app.config["LIBRARY_DIR"]
                        )
                        library.append((library_file, file_path))

            for library_file, file_path in library:
                if not File.query.filter_by(
                    file_path=file_path
                ).first() and os.path.isfile(library_file):
                    job = current_app.import_queue.enqueue(
                        "app.videos.localization_task",
                        args=(library_file,),
                        job_timeout=current_app.config["LOCALIZATION_TASK_TIMEOUT"],
                        description=f"'{os.path.basename(library_file)}'",
                        job_id=safe_job_id(os.path.basename(library_file)),
                    )
                    current_app.logger.info(
                        f"'{library_file}' isn't in library; added to import queue"
                    )

        except Exception:
            app.logger.error(traceback.format_exc())

        else:
            return True


def download_task(key, basename, sqs_receipt_handle=None, transient_retries=0):
    """Download a file from AWS S3 storage."""

    # Retry plumbing still lives in app.videos; imported lazily so the
    # module import direction stays videos → aws_storage

    from app.videos import MAX_TRANSIENT_RETRIES, TRANSIENT_COPY_ERRNOS

    with app.app_context():
        job = get_current_job()

        file = File.query.filter_by(aws_untouched_key=key).first()
        if file:
            basename = file.untouched_basename

        if job:
            job.meta["description"] = f"'{basename}' — Downloading from AWS"
            job.save_meta()

        try:
            current_app.logger.info(
                f"Starting download of '{basename}' from AWS S3 storage"
            )
            downloaded = aws_download(key, basename, sqs_receipt_handle)

        except OSError as e:
            if (
                e.errno in TRANSIENT_COPY_ERRNOS
                and transient_retries < MAX_TRANSIENT_RETRIES
            ):
                # The import volume hiccuped mid-download or mid-rename; the
                # S3 object and the SQS message are unaffected, so retry
                # once the mount settles

                current_app.logger.warning(
                    f"'{basename}' Download failed with a transient I/O "
                    f"error ({e}), returning to queue to try again in 5 "
                    f"minutes (attempt {transient_retries + 1} of "
                    f"{MAX_TRANSIENT_RETRIES})"
                )
                current_app.file_queue.enqueue_in(
                    timedelta(minutes=5),
                    "app.videos.download_task",
                    key,
                    basename,
                    sqs_receipt_handle,
                    transient_retries=transient_retries + 1,
                    job_timeout=current_app.config["TRANSCODE_TASK_TIMEOUT"],
                    job_id=retry_job_id(
                        "download_task", f"'{basename}'", transient_retries + 1
                    ),
                    result_ttl=86400,
                    description=f"'{basename}' — Downloading from AWS",
                )
                return False
            current_app.logger.error(traceback.format_exc())

        except Exception:
            current_app.logger.error(traceback.format_exc())

        else:
            # Any truthy status means the SQS message was handled: the file
            # landed (DOWNLOAD_COMPLETE), or there was nothing to download
            # (object missing, restore pending). False means the retry budget
            # was exhausted or the message couldn't be cleaned up.

            if not downloaded:
                current_app.logger.error(
                    f"'{basename}' download from AWS S3 storage failed"
                )
                return False
            return True


def sqs_retrieve_task():
    """Poll AWS SQS for possible files ready to download."""

    with app.app_context():
        sqs_client = aws_sqs_client()
        s3_client = aws_s3_client(with_retries=True)

        # Extend timeout and restoration period for messages whose downloads
        # are running or waiting in the download queue

        file_operations = StartedJobRegistry(
            "fitzflix-file-operation", connection=current_app.redis
        )
        download_job_ids = file_operations.get_job_ids() + list(
            current_app.file_queue.job_ids
        )
        for job_id in download_job_ids:
            job = current_app.file_queue.fetch_job(job_id)
            if job:
                if job.meta.get("sqs_receipt_handle"):
                    response = sqs_client.change_message_visibility(
                        QueueUrl=current_app.config["AWS_SQS_URL"],
                        ReceiptHandle=job.meta.get("sqs_receipt_handle"),
                        VisibilityTimeout=600,
                    )
                    job_description = job.meta.get("description", job.description)
                    current_app.logger.info(
                        f"'{job_description}' Extending SQS message timeout by 600 seconds"
                    )
                    response = s3_client.restore_object(
                        Bucket=current_app.config["AWS_BUCKET"],
                        Key=job.args[0],
                        RestoreRequest={
                            "Days": 1,
                            "GlacierJobParameters": {"Tier": "Standard"},
                        },
                    )
                    current_app.logger.info(
                        f"'{job.args[0]}' Extending restoration period by 1 day"
                    )

        response = sqs_client.receive_message(
            QueueUrl=current_app.config["AWS_SQS_URL"],
            AttributeNames=["SentTimestamp"],
            MaxNumberOfMessages=1,
            MessageAttributeNames=["All"],
            VisibilityTimeout=600,
            WaitTimeSeconds=0,
        )

        while response.get("Messages"):
            response_body = json.loads(response["Messages"][0]["Body"])

            receipt_handle = response["Messages"][0]["ReceiptHandle"]
            key = urllib.parse.unquote_plus(
                response_body["Records"][0]["s3"]["object"]["key"]
            )

            current_app.file_queue.enqueue(
                "app.videos.download_task",
                args=(key, os.path.basename(key), receipt_handle),
                job_timeout=current_app.config["TRANSCODE_TASK_TIMEOUT"],
                description=f"'{os.path.basename(key)}' — Downloading from AWS",
                meta={"sqs_receipt_handle": receipt_handle},
            )

            response = sqs_client.receive_message(
                QueueUrl=current_app.config["AWS_SQS_URL"],
                AttributeNames=["SentTimestamp"],
                MaxNumberOfMessages=1,
                MessageAttributeNames=["All"],
                VisibilityTimeout=600,
                WaitTimeSeconds=0,
            )

        return True


def upload_task(
    file_id,
    key_prefix="",
    force_upload=False,
    ignore_etag=False,
    storage_class="STANDARD",
):
    """Upload a file to AWS S3 storage."""

    with app.app_context():
        try:
            # Get the record of the file to be uploaded to AWS S3 storage

            file = File.query.filter_by(id=file_id).first()
            file_path = os.path.join(app.config["LIBRARY_DIR"], file.file_path)

            # Pass to the aws_upload() function for uploading.
            # Update the File record with the remote key and date it was uploaded.

            if file.aws_untouched_key:
                (
                    file.aws_untouched_key,
                    file.aws_untouched_date_uploaded,
                    file.aws_untouched_filesize_bytes,
                ) = aws_upload(
                    file_path=file_path,
                    key_name=file.aws_untouched_key,
                    force_upload=force_upload,
                    ignore_etag=ignore_etag,
                    storage_class=storage_class,
                )

            else:
                (
                    file.aws_untouched_key,
                    file.aws_untouched_date_uploaded,
                    file.aws_untouched_filesize_bytes,
                ) = aws_upload(
                    file_path=file_path,
                    key_prefix=key_prefix,
                    force_upload=force_upload,
                    ignore_etag=ignore_etag,
                    storage_class=storage_class,
                )

            file.date_updated = file.aws_untouched_date_uploaded
            file.aws_untouched_stale = False

            db.session.commit()

        except Exception:
            current_app.logger.error(traceback.format_exc())
            db.session.rollback()

        else:
            return True


# Supporting functions


def untouched_key_still_claimed(key):
    """Whether any surviving file record still claims this untouched
    S3 key. Distinct records can share a key — a replaced file whose
    key was repointed after a rename, or a re-import landing on
    the same basename — and deleting a claimed key would strand the
    survivor's archive behind a delete marker (the Bambi II incident,
    Aug 2026). Callers check AFTER their own deletes commit, so the
    rows being purged no longer count."""

    return (
        db.session.query(File.id)
        .filter(
            File.aws_untouched_key == key,
            File.aws_untouched_date_deleted.is_(None),
        )
        .first()
        is not None
    )


def rename_untouched_object(file, new_key):
    """Move a file's untouched S3 archive when its derived key changes,
    keeping the invariant that aws_untouched_key only ever names a REAL
    object (the old flow rewrote the database field without
    moving anything, stranding 1,184 keys found by the Aug 17 audit).

    STANDARD (or restored) objects are copied server-side
    (multipart-capable), verified, and the old key deleted; only then
    does the field change. When the object CAN'T be copied — Deep
    Archive without a completed restore, or missing outright — the
    LOCAL library file force-uploads under the new key instead
    (Glenn's call, Aug 18: close the invariant now rather than hope a
    future re-upload heals it; the archive-replace convention already
    trades the pristine original for the current library file on every
    remux, and the original survives as a noncurrent version).
    Returns True when the field now matches new_key.
    """

    old_key = file.aws_untouched_key
    if not old_key or old_key == new_key:
        return old_key == new_key
    basename = file.basename
    local_path = os.path.join(current_app.config["LIBRARY_DIR"], file.file_path)
    s3_client = aws_s3_client(with_retries=True)
    bucket = current_app.config["AWS_BUCKET"]

    head = None
    try:
        head = s3_client.head_object(Bucket=bucket, Key=old_key)
    except Exception:
        current_app.logger.warning(
            f"'{basename}' archive object '{old_key}' not found; "
            f"re-archiving the library copy under '{new_key}'"
        )

    storage_class = (head or {}).get("StorageClass") or "STANDARD"
    restored = 'ongoing-request="false"' in ((head or {}).get("Restore") or "")
    copyable = head is not None and (
        storage_class not in ("DEEP_ARCHIVE", "GLACIER") or restored
    )

    if copyable:
        current_app.logger.info(
            f"'{basename}' moving archive '{old_key}' -> '{new_key}'"
        )
        s3_client.copy({"Bucket": bucket, "Key": old_key}, bucket, new_key)
        verify = s3_client.head_object(Bucket=bucket, Key=new_key)
        if verify["ContentLength"] != head["ContentLength"]:
            raise RuntimeError(
                f"'{basename}' archive copy size mismatch: "
                f"{verify['ContentLength']} vs {head['ContentLength']}"
            )
        s3_client.delete_object(Bucket=bucket, Key=old_key)
        file.aws_untouched_key = new_key
        return True

    current_app.logger.info(
        f"'{basename}' archive can't be copied "
        f"({storage_class if head else 'missing'}); force-uploading the "
        f"library copy as '{new_key}'"
    )
    (
        file.aws_untouched_key,
        file.aws_untouched_date_uploaded,
        file.aws_untouched_filesize_bytes,
    ) = aws_upload(
        local_path,
        current_app.config["AWS_UNTOUCHED_PREFIX"],
        key_name=os.path.basename(new_key),
        force_upload=True,
        ignore_etag=True,
    )
    if head is not None:
        s3_client.delete_object(Bucket=bucket, Key=old_key)
    return True


def aws_delete(key):
    """Delete an object from AWS S3 storage."""

    # Needs app.app_context() in order for user to call directly from web application
    with app.app_context():
        current_app.logger.info(f"Preparing to delete '{key}' from AWS...")
        s3_client = aws_s3_client()
        s3_client.delete_object(Bucket=current_app.config["AWS_BUCKET"], Key=key)
        current_app.logger.info(f"'{key}' deleted from AWS S3 storage")
        return datetime.now(timezone.utc)


# aws_download outcomes. Failure is a plain False; every success status is
# truthy, so callers that only care whether the SQS message was handled can
# boolean-test the result, while callers that need to know whether a file
# actually landed compare against DOWNLOAD_COMPLETE

DOWNLOAD_COMPLETE = "complete"
DOWNLOAD_OBJECT_MISSING = "object-missing"
DOWNLOAD_RESTORE_PENDING = "restore-pending"

MAX_DOWNLOAD_RETRIES = 10

# Client errors that fail identically on every attempt (credentials,
# permissions, a missing bucket), so retrying only delays the failure report

NON_RETRYABLE_DOWNLOAD_ERRORS = (
    "AccessDenied",
    "InvalidAccessKeyId",
    "SignatureDoesNotMatch",
    "NoSuchBucket",
    "403",
)

# Seam for tests: retry backoff sleeps through this module attribute

DOWNLOAD_RETRY_SLEEP = time.sleep


def _spend_download_retry(retry):
    """Spend one in-place download retry, backing off exponentially
    (1s, 2s, 4s, ... capped at 60s) before the next attempt."""

    retry -= 1
    if retry > 0:
        DOWNLOAD_RETRY_SLEEP(min(2 ** (MAX_DOWNLOAD_RETRIES - 1 - retry), 60))
    return retry


def aws_download(key, basename, sqs_receipt_handle=None):
    """Download an object from AWS S3 storage.

    Returns DOWNLOAD_COMPLETE when the file landed in the import directory,
    DOWNLOAD_OBJECT_MISSING when there is no such object at AWS, and
    DOWNLOAD_RESTORE_PENDING when the object is in cold storage and a
    restore's completion notification will re-trigger the download; all three
    are truthy and mean the SQS message was handled. Returns False when the
    retry budget was exhausted or the SQS message couldn't be deleted.
    """

    # Retry plumbing still lives in app.videos; imported lazily so the
    # module import direction stays videos → aws_storage

    from app.videos import TRANSIENT_COPY_ERRNOS

    retry = MAX_DOWNLOAD_RETRIES

    # Rename "(edition-foo bar baz)" to "{edition-foo bar baz}"
    if "(edition-" in basename:
        basename = re.sub(
            r"\(edition\-(?P<edition>.+)\)", "{edition-\\g<edition>}", basename
        )

    current_app.logger.info(f"'{basename}' downloading from AWS S3 storage")

    s3_client = aws_s3_client()
    sqs_client = aws_sqs_client()

    while retry > 0:
        try:
            s3_client.download_file(
                current_app.config["AWS_BUCKET"],
                key,
                os.path.join(current_app.config["IMPORT_DIR"], f".{basename}"),
                Callback=DownloadProgressPercentage(
                    s3_client,
                    current_app.config["AWS_BUCKET"],
                    key,
                    basename,
                ),
            )

        # Don't resume if the file doesn't exist in AWS!
        except botocore.exceptions.ClientError as error:
            # boto3 signals a missing object via Error.Code ("404"/"NoSuchKey");
            # keep the HTTP status code check as a fallback
            error_code = str(error.response.get("Error", {}).get("Code", ""))
            status_code = error.response.get("ResponseMetadata", {}).get(
                "HTTPStatusCode"
            )
            if error_code in ("404", "NoSuchKey") or status_code == 404:
                current_app.logger.info(f"'{basename}' doesn't exist in AWS S3")
                if sqs_receipt_handle:
                    if not delete_sqs_message(sqs_client, sqs_receipt_handle):
                        return False
                return DOWNLOAD_OBJECT_MISSING

            elif error_code == "InvalidObjectState":
                # The restored copy expired before it could be downloaded, so
                # the object is back in cold storage and this SQS message is
                # stale. Request a new restore unless one is already underway;
                # its completion notification will re-trigger the download.

                try:
                    head_response = s3_client.head_object(
                        Bucket=current_app.config["AWS_BUCKET"], Key=key
                    )
                except Exception:
                    # A failed status check spends a retry like any other
                    # error instead of escaping the loop

                    current_app.logger.error(traceback.format_exc())
                    retry = _spend_download_retry(retry)
                    continue
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
                    aws_restore(key)

                if sqs_receipt_handle:
                    if not delete_sqs_message(
                        sqs_client, sqs_receipt_handle, note="stale message"
                    ):
                        return False
                return DOWNLOAD_RESTORE_PENDING

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

        except OSError as e:
            if e.errno in TRANSIENT_COPY_ERRNOS:
                # A dead import volume fails instantly, so burning the whole
                # in-place retry budget on it is pointless: drop the partial
                # download and let the caller defer until the mount settles

                try:
                    os.remove(
                        os.path.join(current_app.config["IMPORT_DIR"], f".{basename}")
                    )
                except OSError:
                    pass
                raise
            current_app.logger.error(traceback.format_exc())
            retry = _spend_download_retry(retry)

        except Exception:
            current_app.logger.error(traceback.format_exc())
            retry = _spend_download_retry(retry)

        else:
            current_app.logger.info(f"'{basename}' downloaded from AWS S3 storage")

            os.rename(
                os.path.join(current_app.config["IMPORT_DIR"], f".{basename}"),
                os.path.join(current_app.config["IMPORT_DIR"], f"{basename}"),
            )

            if sqs_receipt_handle:
                if not delete_sqs_message(sqs_client, sqs_receipt_handle):
                    return False

            return DOWNLOAD_COMPLETE

    current_app.logger.error(
        f"'{basename}' could not be downloaded from AWS S3 storage; giving up"
    )

    # Import scans skip dotfiles, so an abandoned partial download would
    # otherwise sit invisibly in the import directory forever

    try:
        os.remove(os.path.join(current_app.config["IMPORT_DIR"], f".{basename}"))
    except OSError:
        pass
    return False


def aws_restore(key, days=2, tier="Standard"):
    """Request a file at AWS to be restored from Glacier status for download."""

    with app.app_context():
        try:
            s3_client = aws_s3_client(with_retries=True)

            # Make sure the key exists in the AWS bucket

            response = s3_client.list_objects(
                Bucket=current_app.config["AWS_BUCKET"], Prefix=key, MaxKeys=1
            )

            # The listing has no "Contents" key at all when nothing matches

            contents = response.get("Contents")

            # If the key exists

            if contents and contents[0].get("Key"):
                head_response = s3_client.head_object(
                    Bucket=current_app.config["AWS_BUCKET"], Key=key
                )

                if contents[0].get(
                    "StorageClass"
                ) == "STANDARD" or 'ongoing-request="false"' in head_response.get(
                    "Restore", 'ongoing-request="true"'
                ):
                    current_app.logger.info(
                        f"'{key}' doesn't need to be restored; attempting to download"
                    )
                    current_app.file_queue.enqueue(
                        "app.videos.download_task",
                        args=(key, os.path.basename(key)),
                        job_timeout=current_app.config["LOCALIZATION_TASK_TIMEOUT"],
                        description=f"'{os.path.basename(key)}'",
                        at_front=True,
                    )
                    return

                else:
                    response = s3_client.restore_object(
                        Bucket=current_app.config["AWS_BUCKET"],
                        Key=key,
                        RestoreRequest={
                            "Days": days,
                            "GlacierJobParameters": {"Tier": tier},
                        },
                    )
                    current_app.logger.info(
                        f"Requested '{key}' to be restored for {days} day(s) using tier '{tier}'"
                    )

            else:
                current_app.logger.warning(
                    f"'{key}' does not exist in AWS S3 storage, cannot restore"
                )

        # Only botocore ClientError instances have a .response attribute;
        # let any other exception propagate unmasked

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
    """Search for a file in AWS S3, and upload if it doesn't exist or if it differs."""

    from app.videos import TRANSIENT_COPY_ERRNOS, move_to_rejects

    if not os.path.isfile(file_path):
        current_app.logger.error(
            f"'{file_path}' can't be uploaded to AWS since it's not a file!"
        )

        # Raise instead of returning None: every caller unpacks the return value
        # as a (key, date_uploaded, filesize_bytes) tuple

        raise FileNotFoundError(
            f"'{file_path}' can't be uploaded to AWS since it's not a file"
        )

    if key_name:
        key = sanitize_s3_key(key_name)

    else:
        key = sanitize_s3_key(os.path.basename(file_path))

    key = os.path.join(key_prefix, key)

    s3_client = aws_s3_client(with_retries=True)

    # See if the key already exists in the AWS bucket

    response = s3_client.list_objects(
        Bucket=current_app.config["AWS_BUCKET"], Prefix=key, MaxKeys=1
    )

    # If the key already exists, check to see if the local and remote ETags match.
    # If the ETags match, then the files are the same and there's no need to re-upload.
    # If the IGNORE_ETAGS flag is set, only compare the file/key names, not their data.

    if not force_upload and not current_app.config["FORCE_UPLOAD"]:
        # Look for an object with this exact key: since the listing is a Prefix
        # search, it can return a different, longer key instead

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

    # Upload the file to AWS S3 storage

    # Thanks to https://codeflex.co/python-s3-multipart-file-upload-with-metadata-and-progress-indicator/
    # for the logic on how to handle failures; I couldn't figure out that
    # botocore.exceptions.ClientError and boto3.exceptions.S3UploadFailedError
    # returned different error formats until I saw this post.

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
                move_to_rejects(file_path, "upload error")
                current_app.logger.error(e)
                raise

        except OSError as e:
            # A mount that dropped out is not a bad file. Rejecting it
            # would move a library file out of the library over a problem
            # that clears on its own — and this is exactly how the NAS's
            # lost-handle state arrives, from inside s3transfer's close.

            if e.errno in TRANSIENT_COPY_ERRNOS:
                current_app.logger.error(
                    f"'{file_path}' upload failed on a transient filesystem "
                    f"error ({e.strerror}); leaving the file where it is"
                )
                raise

            move_to_rejects(file_path, "upload error")
            raise

        except:
            move_to_rejects(file_path, "upload error")
            raise

        else:
            current_app.logger.info(f"Uploaded '{file_path}' to AWS")
            return key, datetime.now(timezone.utc), os.path.getsize(file_path)

    current_app.logger.error(
        f"Tried to upload '{file_path}' {str(MAX_RETRY_COUNT)} times but couldn't!"
    )
    move_to_rejects(file_path, "upload error")

    # Raise instead of falling off the end returning None: every caller unpacks
    # the return value as a (key, date_uploaded, filesize_bytes) tuple

    raise RuntimeError(
        f"Unable to upload '{file_path}' to AWS after {MAX_RETRY_COUNT} attempts"
    )


def mark_archive_stale(file_id, reason=""):
    """Record that a file's S3 archive is older than its local copy.

    Committed in its own transaction because every caller is a failure
    path that rolls back, and a marker discarded by that rollback would
    leave the loss exactly as undiscoverable as it was before: the key
    still exists and its date is the previous upload's, so nothing that
    inspects the row can tell the archive is behind.
    """

    from app.models import File

    try:
        file = File.query.filter_by(id=file_id).first()
        if file is None:
            return False

        file.aws_untouched_stale = True
        db.session.commit()

    except Exception:
        db.session.rollback()
        current_app.logger.error(traceback.format_exc())
        return False

    current_app.logger.warning(
        f"'{file.basename}' its S3 archive is now stale ({reason}); "
        f"queued for repair, which will run once the file is readable again"
    )
    return True


def calculate_etag(file_path):
    """Calculate the unique ETag for a local file."""

    basename = os.path.basename(file_path)
    current_app.logger.info(f"'{basename}' Calculating ETag")
    job = get_current_job()

    file_size = os.path.getsize(file_path)
    if file_size < EIGHT_MEGABYTES:
        # The file is less than 8 MB, so read the file in one go, and return its MD5 hash

        with open(file_path, "rb") as f:
            md5_hash = hashlib.md5(f.read())

        return md5_hash.hexdigest()

    else:
        md5_digests = []

        # Read a file in 8 MB chunks, and get the MD5 hash of each chunk

        with open(file_path, "rb") as f:
            previous_percent = None
            for chunk in iter(lambda: f.read(EIGHT_MEGABYTES), b""):
                # Concatenate all of the MD5 hashes together
                md5_digests.append(hashlib.md5(chunk).digest())
                percent = int((f.tell() / file_size) * 100)
                if previous_percent != percent:
                    current_app.logger.info(
                        f"'{basename}' Calculating ETag: {percent}%"
                    )
                    previous_percent = percent
                if job:
                    job.meta["description"] = f"'{basename}' — Calculating ETag"
                    job.meta["progress"] = percent
                    job.save_meta()

        # Get an MD5 hash of the concatenated hashes, and append the number of parts
        # e.g. "c7c2300fd47954c421d5fe0bc7910ca3-64"
        # c7c2300fd47954c421d5fe0bc7910ca3 is the hash of the concatenated MD5 hashes,
        # and there were 64 parts/individual MD5 hashes for the uploaded file

        return (
            hashlib.md5(b"".join(md5_digests)).hexdigest() + "-" + str(len(md5_digests))
        )


def get_matching_s3_objects(bucket, prefix="", suffix=""):
    """Iterate through objects in S3 storage.

    https://alexwlchan.net/2019/07/listing-s3-keys/

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
    """Sanitize the key name to remove problematic characters.

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


# This process's app instance, resolved lazily so importing this module from
# a process that already has an application doesn't build a second one

app = LocalProxy(get_app)
