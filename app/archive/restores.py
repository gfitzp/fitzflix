"""The restore of an archive from cold storage: the request, the SQS message, and the route of the answer."""

import json
import os
import traceback
import urllib.parse
from datetime import datetime, timedelta, timezone
import botocore
from flask import current_app
from werkzeug.local import LocalProxy
from app.tasks import enqueue_task, live_jobs
from app import db, get_app
from app.models import ArchivedOriginal, File
from app.archive import archive_keys, aws_clients, moves

app = LocalProxy(get_app)


# A second receive of a message gives it a new receipt handle. SQS can
# ignore a delete with an older handle. A restore message is about 1 S3
# object. Thus, the poll records the newest handle under the S3 key, on
# every receive (#617). Only the poll writes it. Whoever deletes the
# message deletes the entry.

SQS_HANDLE_KEY = "fitzflix:sqs:handle:{key}"
SQS_HANDLE_SECONDS = 14 * 86400


def record_receipt_handle(key, receipt_handle):
    """Record the newest receipt handle of the restore message of key (#617)."""

    current_app.redis.set(
        SQS_HANDLE_KEY.format(key=key), receipt_handle, ex=SQS_HANDLE_SECONDS
    )


def _latest_receipt_handle(receipt_handle, key=None):
    """Return the newest receipt handle of the restore message of key.

    With no record, the given handle stays."""

    if key is None:
        return receipt_handle
    latest = current_app.redis.get(SQS_HANDLE_KEY.format(key=key))
    return latest.decode() if latest else receipt_handle


def _message_comes_back(receipt_handle, key):
    """Return True if a restore message of key exists that SQS delivers again (#658).

    A download that a page queued for a readable object has no message.
    A Redis fault counts as a message. Then the 14-day sweep ends the
    state."""

    try:
        return bool(_latest_receipt_handle(receipt_handle, key))
    except Exception:
        return True


def delete_download_message(receipt_handle, key, note="message", sqs_client=None):
    """Delete the restore message of key, if there is one (#572, #617).

    A job that started with no handle can get one later through the
    record of the poll. Thus, the newest handle decides. Return True if
    there is no message, else the result of delete_sqs_message."""

    return delete_sqs_message(sqs_client, receipt_handle, note=note, key=key)


def delete_sqs_message(sqs_client, receipt_handle, note="message", key=None):
    """Delete a message from the SQS queue. Return False if the delete fails.

    key is the S3 key of a restore message. The newest recorded handle
    of key replaces receipt_handle. A good delete removes the record.
    With no handle, there is no message. Then this returns True. With
    no sqs_client, this builds one."""

    receipt_handle = _latest_receipt_handle(receipt_handle, key)
    if not receipt_handle:
        return True
    sqs_client = sqs_client or aws_clients.aws_sqs_client()
    try:
        response = sqs_client.delete_message(
            QueueUrl=current_app.config["AWS_SQS_URL"],
            ReceiptHandle=receipt_handle,
        )
        current_app.logger.debug(f"SQS delete_message response: {response}")

    except Exception:
        current_app.logger.warning(
            f"Unable to delete message '{receipt_handle}' from SQS"
        )
        return False

    current_app.logger.info(f"Deleted {note} '{receipt_handle}' from SQS")
    if key is not None:
        current_app.redis.delete(SQS_HANDLE_KEY.format(key=key))
    return True


# A pending restore is a state of the File row (#610). The request sets
# File.restore_requested_at. While it is set, the file page disables its
# edit and download forms, and each caller of
# refuse_while_restore_pending refuses. These are the track edits, the
# forms of the triage pages and the removal of empty tracks by a scan.
# The Atmos job, the uploads of the library file and the move of an
# archive to modified/ refuse too. move_archive_object, a rename and
# Delete File do not refuse. The end of the download and the 14-day sweep clear the state.
# Thus, a request cannot lose its object to an edit during the hours of
# a thaw.

RESTORE_PENDING_MESSAGE = (
    "A restore of this file from AWS is pending. Fitzflix does not change "
    "the file or its archive until the restore ends."
)


def mark_restore_pending(key):
    """Record a restore request of key on each File row that claims it (#610).

    The archive or the transcoded/ copy of a row can be key. Each
    request records its own time. A key with no File row (an
    ArchivedOriginal) gets nothing.
    This commits."""

    rows = File.query.filter(
        db.or_(File.aws_key == key, File.aws_transcoded_key == key)
    )
    requested = datetime.now(timezone.utc)
    for row in rows.all():
        row.restore_requested_at = requested
    db.session.commit()


def end_restore_request(file_id):
    """Clear the pending restore of the File row file_id (#610).

    download_task calls this when the download ends with no retry, no
    new restore and no SQS message that comes back (#658). Thus, a form
    never stays off for a restore that does not come. The row id holds
    when an archive move changes the key during the download. A fault of the clear goes to the log. The 14-day sweep
    then clears the row. This commits."""

    if file_id is None:
        return
    try:
        cleared = File.query.filter(
            File.id == file_id, File.restore_requested_at.isnot(None)
        ).update({"restore_requested_at": None}, synchronize_session=False)
        if cleared:
            db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.warning(
            f"File {file_id} Could not clear the pending restore: "
            + traceback.format_exc()
        )


def end_restore_requests_of_key(key):
    """Clear the pending restore of each row that claims key (#613).

    aws_restore calls this when a request that a page recorded cannot
    be made: the key is gone from S3, or S3 refuses it. Thus, a form
    never stays off for a restore that never started. This commits."""

    try:
        cleared = File.query.filter(
            db.or_(File.aws_key == key, File.aws_transcoded_key == key),
            File.restore_requested_at.isnot(None),
        ).update({"restore_requested_at": None}, synchronize_session=False)
        if cleared:
            db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.warning(
            f"'{key}' Could not clear the pending restore: " + traceback.format_exc()
        )


def request_restore(key, tier="Standard", description=None):
    """Record a restore request of key on its rows, then queue the request (#613).

    A page calls this. The record comes before the queue. Thus, the
    page that the request redirects to shows the forms disabled at
    once, not after the worker runs. The worker clears the record if
    the request cannot be made."""

    mark_restore_pending(key)
    enqueue_task(
        "app.archive.restores.aws_restore",
        key,
        tier=tier,
        description=description or f"'{os.path.basename(key)}'",
    )


def refuse_while_restore_pending(file):
    """Return RESTORE_PENDING_MESSAGE if a restore of the file is pending, else None (#610).

    Each writer calls this after it takes the title lock. A refusal
    changes nothing. The value comes from the database, not from the
    loaded row. The request does not take the title lock, and a writer
    can hold an old copy of the row."""

    if file is None or file.id is None:
        return None
    requested = (
        db.session.query(File.restore_requested_at).filter(File.id == file.id).scalar()
    )
    return RESTORE_PENDING_MESSAGE if requested is not None else None


def expire_restore_requests(max_age_days):
    """Clear each pending restore that is older than max_age_days (#610).

    A chain of a download waits for a volume for 1 day at most. A request
    that is older than the cap has no download any more. Return the
    number of cleared rows. This commits."""

    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    rows = File.query.filter(File.restore_requested_at < cutoff).all()
    for row in rows:
        current_app.logger.info(
            f"'{row.basename}' The restore request from "
            f"{row.restore_requested_at.isoformat()} has no result after "
            f"{max_age_days} days. Its pending state ends"
        )
        row.restore_requested_at = None
    db.session.commit()
    return len(rows)


def sqs_retrieve_task():
    """Poll AWS SQS for files that can be ready for download."""

    with app.app_context():
        sqs_client = aws_clients.aws_sqs_client()
        s3_client = aws_clients.aws_s3_client(with_retries=True)

        # Extend the timeout and the restore period of a message whose
        # download or archive move (#369) runs or waits in the file queue.

        # The download job of each key that waits, runs, or waits for a
        # retry. A message that comes back for such a key gets no second
        # download. Its new receipt handle goes under the key (#617). The
        # move jobs are keyed the same way, by the key that they move.

        downloads_by_key = {}
        moves_by_key = {}
        # The message stays hidden for SQS_VISIBILITY_SECONDS. That is
        # longer than the hourly poll. Thus, a message is still in flight
        # here, and the change succeeds. A failure on 1 message must not
        # stop the poll (MessageNotInflight, an expired receipt handle).

        for job in live_jobs(["file"]):
            restore_key = None
            if job.func_name == "app.archive.downloads.download_task" and job.args:
                downloads_by_key[job.args[0]] = job
                restore_key = job.args[0]
            elif job.func_name == "app.archive.moves.move_archive_object" and job.args:
                restore_key = job.meta.get("archive_key")
                if restore_key is None:
                    moving = db.session.get(File, int(job.args[0]))
                    restore_key = archive_keys.move_source(moving) if moving else None
                if restore_key:
                    moves_by_key[restore_key] = job
            handle = restore_key and _latest_receipt_handle(
                job.meta.get("sqs_receipt_handle"), restore_key
            )
            if handle:
                job_description = job.meta.get("description", job.description)
                try:
                    sqs_client.change_message_visibility(
                        QueueUrl=current_app.config["AWS_SQS_URL"],
                        ReceiptHandle=handle,
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
                record_receipt_handle(key, receipt_handle)
                current_app.logger.info(
                    f"'{key}' Already has a job. Recorded the new receipt "
                    f"handle of its message"
                )
            else:
                record_receipt_handle(key, receipt_handle)
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
    """Return the File row whose archive or transcoded/ copy is key, or None.

    2 rows can share a key. A row with a pending move of the key comes
    first (#385). Else the restore of the move goes to a download."""

    claimants = File.query.filter(
        db.or_(File.aws_key == key, File.aws_transcoded_key == key)
    ).order_by(File.id)
    moving = claimants.filter(File.aws_key_target.isnot(None)).all()
    for file in moving:
        if archive_keys.move_source(file) == key:
            return file
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
    message is deleted. Each other key gets a download.

    2 rows can share a key. A user can request the download of the key
    while a move of it waits (#385 follow-up). Then the key gets both
    jobs. The download goes first and has no receipt handle. The move
    waits for the download, and then it deletes the message."""

    file = _file_claiming(key)
    if (
        file is not None
        and file.aws_key_target
        and archive_keys.move_source(file) == key
    ):
        if file.restore_requested_at is not None and not moves._download_job_for(key):
            downloads_by_key[key] = enqueue_task(
                "app.archive.downloads.download_task",
                key,
                os.path.basename(key),
                None,
                description=f"'{os.path.basename(key)}' — Downloading from AWS",
            )
            current_app.logger.info(
                f"'{key}' Restored for a requested download and for an archive move"
            )
        moves_by_key[key] = enqueue_task(
            "app.archive.moves.move_archive_object",
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
        delete_sqs_message(sqs_client, receipt_handle, note="message", key=key)
        return
    downloads_by_key[key] = enqueue_task(
        "app.archive.downloads.download_task",
        key,
        os.path.basename(key),
        receipt_handle,
        description=f"'{os.path.basename(key)}' — Downloading from AWS",
        meta={"sqs_receipt_handle": receipt_handle},
    )


def restore_would_replace_newer(file, key=None):
    """Return True if a direct restore would replace a newer library file (#496).

    A stale row has a library file that changed after its archive. A
    modified/ or transcoded/ restore over it would bring back the older
    bytes."""

    key = key or file.restore_key
    return bool(
        file.aws_stale
        and restores_directly(key)
        and os.path.isfile(
            os.path.join(current_app.config["LIBRARY_DIR"], file.file_path)
        )
    )


def restores_directly(key):
    """Return True if a restore of this key goes straight into the library (#299).

    A modified/ or transcoded/ object is a library file. An untouched/
    object is a rip, and it goes through the import."""

    config = current_app.config
    return archive_keys._in_prefix(
        key, config["AWS_MODIFIED_PREFIX"]
    ) or archive_keys._in_prefix(key, config["AWS_TRANSCODED_PREFIX"])


# The hourly SQS poll hides a restore message for 2 hours, and each poll
# extends it while its download waits or runs. With 10 minutes, the
# message was visible again before the next poll. Then the extension
# failed (MessageNotInflight), and the poll received it a second time.

SQS_VISIBILITY_SECONDS = 7200

# The days that a restored copy stays in S3 Standard. The hourly SQS poll
# renews a pending copy for the same days.

RESTORE_DAYS = 2


def aws_restore(key, tier="Standard", record_request=True):
    """Request a restore of a file at AWS from Glacier status for a download.

    The request sets the pending restore of each File row that claims
    the key (#610). Then each caller of refuse_while_restore_pending
    refuses until the download ends. An import that replaces the file
    ends the state, and the download then drops (#620). A page sets the
    state before it queues this (#613). The stamp here is for the
    requests no page asks for.
    A second request during a thaw is a new request, with its own
    time. A request that cannot be made clears the state.
    record_request=False is a restore for an archive move or a request
    again for the same download. It does not change the rows.
    """

    with app.app_context():
        try:
            s3_client = aws_clients.aws_s3_client(with_retries=True)

            # Make sure that the key exists in the AWS bucket. A HEAD of
            # the key, not a listing of the prefix (#572). For a missing
            # key, a listing of 1 key answered a longer sibling such as
            # "stem.mkv.etag". The restore then asked for a key that does
            # not exist.

            head_response = aws_clients._head_or_none(s3_client, key)

            # The key exists.

            if head_response is not None:
                # S3 keeps the Restore header for a while after the
                # restored copy expires. A test of the header alone sent
                # an expired copy to a download that failed and asked for
                # a restore again, without a delay. S3 leaves StorageClass
                # out of the HEAD of a STANDARD object. _object_is_readable
                # reads that as STANDARD.

                if aws_clients._object_is_readable(head_response):
                    current_app.logger.info(
                        f"'{key}' doesn't need to be restored; attempting to download"
                    )
                    if record_request:
                        mark_restore_pending(key)
                    enqueue_task(
                        "app.archive.downloads.download_task",
                        key,
                        os.path.basename(key),
                        description=f"'{os.path.basename(key)}'",
                        at_front=True,
                    )
                    return

                else:
                    s3_client.restore_object(
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
                        mark_restore_pending(key)

            else:
                current_app.logger.warning(
                    f"'{key}' does not exist in AWS S3 storage, cannot restore"
                )
                if record_request:
                    end_restore_requests_of_key(key)

        # Only a botocore ClientError instance has a .response attribute.
        # Each other exception clears the record and propagates.

        except botocore.exceptions.ClientError as e:
            if e.response["Error"]["Code"] == "RestoreAlreadyInProgress":
                current_app.logger.info(
                    f"'{key}' is already in process of being restored"
                )

                # An archive move can have asked for this restore with no
                # record. A restore that a person asks for needs the
                # record, or the notification queues only the move, and
                # the download never occurs (#532).

                if record_request:
                    mark_restore_pending(key)

            else:
                current_app.logger.error(e)
                if record_request:
                    end_restore_requests_of_key(key)
                raise

        except Exception:
            if record_request:
                end_restore_requests_of_key(key)
            raise

        else:
            return
