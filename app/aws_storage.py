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

from datetime import datetime, timedelta, timezone

import boto3
import botocore
import requests
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

# A re-archive whose record path has no file waits this many times for
# the path commit of the refresh that queued it.
MAX_REARCHIVE_PATH_RETRIES = 2


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
    """Delete a message from the SQS queue. Return False if the delete fails."""

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
            if self._job:
                self._job.meta["description"] = (
                    f"'{os.path.basename(self._file_path)}' — Uploading to AWS"
                )
                self._job.meta["progress"] = percent
                self._job.save_meta()


class DownloadProgressPercentage(object):
    """Report the download progress as a callback during a download from S3."""

    def __init__(self, client, bucket, key, basename):
        self._file_path = basename
        self.size = client.head_object(Bucket=bucket, Key=key).get("ContentLength", 0)
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
            if self._job:
                self._job.meta["description"] = (
                    f"'{os.path.basename(self._file_path)}' — Downloading from AWS"
                )
                self._job.meta["progress"] = percent
                self._job.save_meta()


def sync_aws_s3_storage_task():
    """Add the files to AWS, and remove the files that are not in the library."""

    with app.app_context():
        # Sync only when each queue is idle. A file can be in an import, in
        # an upload, or in a wait for database writes. Then it can exist at
        # AWS without its final database record. The prune below would see
        # it as an extra file and delete it.

        job = get_current_job()
        busy = []
        for queue_name, queue in (
            ("fitzflix-import", current_app.import_queue),
            ("fitzflix-transcode", current_app.transcode_queue),
            ("fitzflix-file-operation", current_app.file_queue),
            ("fitzflix-sql", current_app.sql_queue),
            ("fitzflix-user-request", current_app.request_queue),
            # The maintenance queue runs the hourly import sweep. That sweep
            # can feed new files into the import pipeline during the sync.
            ("fitzflix-maintenance", current_app.maintenance_queue),
        ):
            started = StartedJobRegistry(
                queue_name, connection=current_app.redis
            ).get_job_ids()

            # This task itself is in the started registry of user-request.

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

            # Map each remote key to its object size. Thus, the file records
            # can get the exact size that AWS bills for a restore.

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

                # The next branches test the state of the file.

                # The file is not in S3 but exists in the filesystem.
                if (
                    file.aws_untouched_key not in s3_keys
                    or file.aws_untouched_date_uploaded == None
                    or file.aws_untouched_stale
                ) and os.path.isfile(file_path):

                    # Queue the file for an upload to S3.

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

                # The file exists in S3.
                elif file.aws_untouched_key in s3_keys:

                    # Add the file to the inventory.

                    current_app.logger.info(
                        f"'{file.aws_untouched_key}' Exists in AWS S3; rank {rank}"
                    )

                    # Record the real size of the object if the record does
                    # not have it, or if the size changed after the record.

                    remote_size = s3_objects[file.aws_untouched_key]
                    if file.aws_untouched_filesize_bytes != remote_size:
                        file.aws_untouched_filesize_bytes = remote_size

                    if rank == 1:
                        inventory_export.append(
                            [current_app.config["AWS_BUCKET"], file.aws_untouched_key]
                        )

                        # Queue the file for a restore if it does not exist
                        # locally.

                        if not os.path.isfile(file_path):
                            current_app.logger.info(
                                f"'{file.aws_untouched_key}' does not exist in the local library"
                            )
                            aws_restore(file.aws_untouched_key, tier="Bulk")

                # The file is not in S3 and does not exist in the filesystem.
                elif file.aws_untouched_key not in s3_keys and not os.path.isfile(
                    file_path
                ):

                    # Flag the file as an orphaned file.

                    current_app.logger.info(
                        f"'{file.aws_untouched_key}' has no associated files"
                    )
                    orphaned_files.append([file.id, file.untouched_basename])

            # Store the AWS object sizes that this filled in.

            db.session.commit()

            current_app.logger.info(f"Orphaned files: {orphaned_files}")

            # Make a CSV of the best files and upload it to the S3 bucket. If
            # a bulk restore of the library is necessary, this file can drive
            # a restore of all the best files through an S3 Batch Operation.

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

            # Delete the remote S3 files that are not in Fitzflix.

            aws_untouched_keys = {
                aws_untouched_key
                for (aws_untouched_key,) in db.session.query(
                    File.aws_untouched_key
                ).all()
            }

            # This is the rename-skew tripwire. An ACTIVE claim with no
            # matching object means that a restore would return a 404.
            # The 2026-08-17 audit found 1,184 cases of this silent
            # damage. Report it loudly here each week. Thus, it can never
            # accumulate.

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

            # Queue the local files that are in the library folders but not
            # in Fitzflix for an import.

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

    # The retry constants still live in app.videos. Import them lazily.
    # Thus, the module import direction stays videos to aws_storage.

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
                # The import volume had a fault during the download or the
                # rename. The S3 object and the SQS message are not affected.
                # Thus, retry after the mount settles.

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
        # download runs or waits in the download queue.

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
            # Get the record of the file for the upload to AWS S3 storage.

            file = File.query.filter_by(id=file_id).first()
            file_path = os.path.join(app.config["LIBRARY_DIR"], file.file_path)

            # Pass the file to aws_upload() for the upload. Then update the
            # File record with the remote key and the upload date.

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
    """Return True if a surviving file record still claims this S3 key.

    This applies to the untouched key. Different records can share a
    key. For example, a replaced file can get a new key after a rename,
    or a second import can arrive on the same basename. A delete of a
    claimed key would strand the archive of the survivor behind a
    delete marker (the Bambi II incident, 2026-08). The caller must
    check AFTER its own deletes commit. Thus, the purged rows no longer
    count."""

    return (
        db.session.query(File.id)
        .filter(
            File.aws_untouched_key == key,
            File.aws_untouched_date_deleted.is_(None),
        )
        .first()
        is not None
    )


def rename_untouched_object(file, new_key, defer_upload=False):
    """Move the untouched S3 archive of a file when its derived key changes.

    This keeps the invariant that aws_untouched_key only names a REAL
    object. The old flow rewrote the database field and moved nothing.
    That stranded the 1,184 keys that the 2026-08-17 audit found.

    A STANDARD (or restored) object is copied on the server side, with
    multipart support. Then the copy is verified, and the old key is
    deleted. Only then does the field change. Some objects CANNOT be
    copied: a Deep Archive object without a completed restore, or a
    missing object. Then the LOCAL library file force-uploads under the
    new key instead (decided by Glenn, 2026-08-18). This closes the
    invariant now. It does not hope that a future upload repairs it.
    The archive-replace convention already trades the pristine original
    for the current library file on each remux. The original survives
    as a noncurrent version.

    That force-upload is multi-gigabyte. Thus, a caller on a queue with
    a short budget passes defer_upload. Then rearchive_untouched_object
    on the file queue does the upload instead. In #231, a 43.9 GB upload
    died at SQL_TASK_TIMEOUT. That left the movie record renamed, and
    its archive key not renamed.

    Return True if the field now matches new_key. A deferred upload
    returns False, because the field only changes after the object
    really arrives.
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
    copyable = head is not None and _object_is_readable(head)

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

    if defer_upload:
        current_app.logger.info(
            f"'{basename}' archive can't be copied "
            f"({storage_class if head else 'missing'}); queuing a re-archive "
            f"of the library copy as '{new_key}'"
        )
        current_app.file_queue.enqueue(
            "app.videos.rearchive_untouched_object",
            args=(file.id, new_key),
            job_timeout=current_app.config["UPLOAD_TASK_TIMEOUT"],
            description=f"'{basename}'",
        )
        return False

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


def rearchive_untouched_object(file_id, new_key, path_retries=0):
    """Force-upload the local library copy of a file under the new key.

    This is the file-queue half of an archive rename that could not be
    copied on the server side. It is deferred off the sql queue because
    the upload is multi-gigabyte. The 10-minute budget of the sql queue
    is sized for database work. A 43.9 GB re-archive was killed at 84%
    during a refresh, silently. That left the movie record pointed at
    one film, and its archive key at a different film (#231). The
    budget of the file queue is 6 hours.

    This reads the File record again. Thus, the uploaded path includes
    a disk rename that the refresh did after the enqueue. If the path
    on the record has no file, the record may be behind a rename
    that is not committed yet. Then the job waits 5 minutes and
    reads the record again, up to MAX_REARCHIVE_PATH_RETRIES times.
    """

    with app.app_context():
        file = File.query.filter_by(id=file_id).first()
        if file is None:
            current_app.logger.warning(
                f"File id {file_id} no longer exists, skipping the "
                f"re-archive as '{new_key}'"
            )
            return False

        if file.aws_untouched_key == new_key:
            current_app.logger.info(f"'{file.basename}' archive is already '{new_key}'")
            return True

        # This is the WEBDL-rebuild scaffolding (#158). A WEBRip row
        # keeps its WEBDL-named archive key until a real WEB-DL replaces
        # it. Never trade the scaffold key for a new multi-gigabyte
        # upload. This is the same as the guard in apply_tmdb_refresh. It
        # catches the jobs that were queued before that guard shipped.

        if "[WEBDL-" in (file.aws_untouched_key or "") and "[WEBRip-" in new_key:
            current_app.logger.info(
                f"'{file.basename}' keeps its WEBDL-named archive key "
                f"(rebuild scaffolding, #158), skipping the re-archive"
            )
            return False

        # This is the key that the record wants NOW. It differs if the
        # refresh that queued this job rolled back, or if a later refresh
        # queued a newer key behind this one. In the two cases, an upload
        # of tens of gigabytes under an old key is waste.
        expected_key = os.path.join(
            current_app.config["AWS_UNTOUCHED_PREFIX"],
            sanitize_s3_key(file.untouched_basename or ""),
        )
        if expected_key != new_key:
            current_app.logger.warning(
                f"'{file.basename}' skipping the re-archive as '{new_key}': "
                f"the record now wants '{expected_key}'"
            )
            return False

        local_path = os.path.join(current_app.config["LIBRARY_DIR"], file.file_path)
        if not os.path.isfile(local_path):
            if path_retries < MAX_REARCHIVE_PATH_RETRIES:
                # The refresh that queued this job may still hold the
                # path rename in an open transaction. Read the record
                # again after the rename has time to land.
                current_app.logger.warning(
                    f"'{file.basename}' '{local_path}' isn't present locally, "
                    f"returning the re-archive as '{new_key}' to the queue "
                    f"to try again in 5 minutes (attempt "
                    f"{path_retries + 1} of {MAX_REARCHIVE_PATH_RETRIES})"
                )
                current_app.file_queue.enqueue_in(
                    timedelta(minutes=5),
                    "app.videos.rearchive_untouched_object",
                    file_id,
                    new_key,
                    path_retries=path_retries + 1,
                    job_timeout=current_app.config["UPLOAD_TASK_TIMEOUT"],
                    job_id=retry_job_id(
                        "rearchive_untouched_object",
                        f"'{file.basename}'",
                        path_retries + 1,
                    ),
                    result_ttl=86400,
                    description=f"'{file.basename}'",
                )
                return False

            # There is nothing to upload again. The record keeps the old
            # key. That key still names a real object. Thus, the invariant
            # holds.
            current_app.logger.error(
                f"'{file.basename}' can't be re-archived as '{new_key}': "
                f"'{local_path}' isn't present locally"
            )
            return False

        try:
            renamed = rename_untouched_object(file, new_key)
            db.session.commit()

        except Exception:
            # Let the job fail loudly. A half-applied rename is exactly the
            # problem of #231. FailedJobRegistry is the trace.
            db.session.rollback()
            raise

        return renamed


def aws_delete(key):
    """Delete an object from AWS S3 storage."""

    # This needs app.app_context(). Thus, the web application can call it
    # directly.
    with app.app_context():
        current_app.logger.info(f"Preparing to delete '{key}' from AWS...")
        s3_client = aws_s3_client()
        s3_client.delete_object(Bucket=current_app.config["AWS_BUCKET"], Key=key)
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

    return bool(current_app.config.get("AWS_DOWNLOAD_VIA_CDN"))


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


def cdn_download(key, destination, progress, expected_size=0):
    """Fetch one object through CloudFront, and write it to destination.

    The progress callback gets the size of each chunk, as the boto3
    transfer callback does. A partial file at the destination from an
    earlier attempt is resumed with a Range request when the S3 size is
    known. Thus, a transport fault does not restart a large object from
    byte zero. Raise CdnDownloadError for an HTTP error status. Raise
    RuntimeError if the body length differs from the Content-Length
    header or from expected_size (the object size from the S3 API).
    Raise RuntimeError if neither size is known. The requests transport
    errors escape as they are.
    """

    url = cdn_signed_url(key)
    have = _partial_size(destination)
    headers = {}
    if have and expected_size and have < expected_size:
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


def _partial_size(destination):
    """Return the size of a partial download at the destination, or 0."""

    try:
        return os.path.getsize(destination)
    except OSError:
        return 0


def _discard_partial(destination):
    """Remove a partial download. The import scan skips dotfiles, so an
    abandoned partial would stay invisible in the import directory."""

    try:
        os.remove(destination)
    except OSError:
        pass


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
        aws_restore(key)

    if sqs_receipt_handle:
        if not delete_sqs_message(sqs_client, sqs_receipt_handle, note="stale message"):
            return False
    return DOWNLOAD_RESTORE_PENDING


def aws_download(key, basename, sqs_receipt_handle=None):
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
    destination = os.path.join(current_app.config["IMPORT_DIR"], f".{basename}")
    s3_client = aws_s3_client()
    sqs_client = aws_sqs_client()

    while retry > 0:
        try:
            progress = DownloadProgressPercentage(s3_client, bucket, key, basename)
            if via_cdn:
                cdn_download(key, destination, progress, expected_size=progress.size)
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


def aws_restore(key, days=2, tier="Standard"):
    """Request a restore of a file at AWS from Glacier status for a download."""

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

    from app.videos import TRANSIENT_COPY_ERRNOS, move_to_rejects

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
                move_to_rejects(file_path, "upload error")
                current_app.logger.error(e)
                raise

        except OSError as e:
            # A mount that dropped out is not a bad file. A reject would
            # move a library file out of the library for a problem that
            # clears by itself. This is exactly how the lost-handle state
            # of the NAS occurs: inside the close() call of s3transfer.

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

        file.aws_untouched_stale = True
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

        # Read the file in 8 MB chunks. Get the MD5 hash of each chunk.

        with open(file_path, "rb") as f:
            previous_percent = None
            for chunk in iter(lambda: f.read(EIGHT_MEGABYTES), b""):
                # Collect the MD5 hashes.
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
