"""The move and the delete of an archive object."""

import os
import random
import time
import traceback
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import botocore
from rq import get_current_job
from rq.registry import StartedJobRegistry
from flask import current_app
from sqlalchemy import event as sa_event
from sqlalchemy.orm import Session as OrmSession
from werkzeug.local import LocalProxy
from app.tasks import enqueue_task, live_jobs, report_progress, requeue
from app import db, get_app
from app.archive import s3_layout
from app.models import File
from app.media import videos
from app.archive import archive_keys, aws_clients, restores, uploads
from app.pipeline import retry_now
from app.tasks import TASKS
from app.tasks import resolve_queue

app = LocalProxy(get_app)


# The session info key of the moves that wait for the commit of the
# session. A move job that starts before the commit reads no target.

_MOVES_AFTER_COMMIT = "fitzflix_archive_moves"

# The session info key of the moves that a holder of a title lock
# requested. The move job takes the title lock. Thus, a move that
# starts before the unlock finds the lock and waits 5 to 15 minutes.

_MOVES_HELD = "fitzflix_archive_moves_held"

# A held move is booked this far ahead. The end of the hold moves it into
# the queue at once. A holder that stops before that leaves the booking.
# Then the move runs at its time.

HELD_MOVE_DELAY = timedelta(minutes=15)


def clear_archive_target(file):
    """Clear the pending target of a file and its request time. The caller commits."""

    file.aws_key_target = None
    file.archive_move_requested_at = None


def _set_archive_target(file, target):
    """Set the pending target of a file. The first request keeps its time."""

    file.aws_key_target = target
    if file.archive_move_requested_at is None:
        file.archive_move_requested_at = datetime.now(timezone.utc)


def request_archive_move(file, claims=None, column=None):
    """Request the move of the archive of a file to the key of its untouched name.

    Set aws_key_target, and queue move_archive_object after the next
    commit of the session. The caller commits. A pending target is
    replaced. The job reads the current target when it runs. A file
    whose archives have their keys loses its pending target. A name
    that differs from the key only by letter case keeps the key, and a
    pending target goes. column limits the request to the
    archive of that key column (archive_drift). Return True if a move
    is pending."""

    drift = archive_keys.archive_drift(file, claims, column)
    if drift is None:
        if file.aws_key_target:
            clear_archive_target(file)
        return False
    source, target = drift
    if file.aws_key_target != target:
        current_app.logger.info(
            f"'{file.basename}' The archive '{source}' moves to '{target}'"
        )
    _set_archive_target(file, target)
    # The target is a claim for each later file of the same run.
    # A replaced or cleared target stays in claims. That stale claim can
    # only add an id to the key of a later file. It never gives 2 files
    # the same target.

    if claims is not None:
        claims.setdefault(archive_keys._claim_key(target), set()).add(file.id)
    db.session.info.setdefault(_MOVES_AFTER_COMMIT, {})[file.id] = file.basename
    return True


@sa_event.listens_for(OrmSession, "after_commit")
def _enqueue_requested_moves(session):
    """Queue the archive moves that request_archive_move recorded in this session."""

    pending = session.info.pop(_MOVES_AFTER_COMMIT, None)
    held = session.info.get(_MOVES_HELD)
    for file_id, basename in sorted((pending or {}).items()):
        try:
            if held is None:
                _enqueue_move_once(file_id, basename)
            elif file_id not in held:
                held[file_id] = _enqueue_move_once(
                    file_id, basename, delay=HELD_MOVE_DELAY
                )
        except Exception:
            current_app.logger.error(traceback.format_exc())


@contextmanager
def archive_moves_after_unlock():
    """Hold back the archive moves that the session commits in the block.

    The code in the block holds a title lock, and it releases the lock
    in the block. A move that a commit requests is booked
    HELD_MOVE_DELAY ahead. Thus, the request stays when the worker
    stops. The end of the block moves each booking into the queue."""

    session = db.session()
    if _MOVES_HELD in session.info:
        yield
        return
    held = session.info[_MOVES_HELD] = {}
    try:
        yield
    finally:
        session.info.pop(_MOVES_HELD, None)
        _queue_held_moves(held)


def _queue_held_moves(held):
    """Move each held booking into its queue now. A fault only logs."""

    queue = resolve_queue(TASKS["app.archive.moves.move_archive_object"].queue)
    for file_id, job in sorted(held.items()):
        if job is None:
            continue
        try:
            retry_now(queue, job.id, front=False)
        except Exception:
            current_app.logger.warning(
                f"File {file_id} Could not queue the held archive move. It "
                f"runs at its booked time: " + traceback.format_exc()
            )


def _enqueue_move_once(file_id, basename, delay=None):
    """Queue the move job of a file, unless a move of the file waits in the queue.

    The id is "archive-move-", the file id, and a random tag. A queued
    move reads the target of the file when it runs. Thus, a second
    request adds nothing. A running move does not count. It can hold an
    older target. Each id is new, so no job takes the expiry of an old
    finished job.

    A known and accepted gap stays. A move that
    waits as a scheduled retry, or a move that a restore notification
    queued with its receipt handle, does not count. A new request (a
    converge-keys run with --again) can add 1 more job for such a
    file. That job finds the
    restore in progress or the move done, and it changes nothing.

    delay books the job ahead."""

    name = "app.archive.moves.move_archive_object"
    prefix = f"archive-move-{int(file_id)}-"
    queue = resolve_queue(TASKS[name].queue)
    if any(job_id.startswith(prefix) for job_id in queue.job_ids):
        return None
    return enqueue_task(
        name,
        file_id,
        delay=delay,
        description=f"'{basename}' — Moving the archive",
        job_id=prefix + uuid.uuid4().hex[:12],
    )


@sa_event.listens_for(OrmSession, "after_rollback")
def _forget_requested_moves(session):
    """Drop the archive moves of a transaction that rolled back."""

    session.info.pop(_MOVES_AFTER_COMMIT, None)


def pending_archive_moves():
    """Return the count of the pending archive moves and the oldest request time."""

    count, oldest = (
        db.session.query(
            db.func.count(File.id), db.func.min(File.archive_move_requested_at)
        )
        .filter(File.aws_key_target.isnot(None))
        .one()
    )
    return {"count": count, "oldest": oldest}


def archive_drifts(claims=None):
    """Return (file, key, rule key, bytes) for each archive that has the wrong key.

    This is the census of the backfill (flask aws converge-keys). Each
    target of the census is a claim for the later files, as in
    request_archive_move. Thus, the list that the admin reads
    shows the targets that the moves get."""

    claims = claims if claims is not None else archive_keys.key_claims()
    drifted = []
    for file in (
        File.query.filter(
            db.or_(File.aws_key.isnot(None), File.aws_transcoded_key.isnot(None))
        )
        .order_by(File.id)
        .all()
    ):
        drift = archive_keys.archive_drift(file, claims)
        if drift is None:
            continue
        source, target = drift
        claims.setdefault(archive_keys._claim_key(target), set()).add(file.id)
        size = (
            file.aws_transcoded_filesize_bytes
            if source == file.aws_transcoded_key
            else file.aws_filesize_bytes
        )
        drifted.append((file, source, target, size or 0))
    return drifted


# The status of a move that waits for a restore of its object. It is
# truthy, as the pending status of a download is. The job ran, and
# nothing failed. The restore notification starts the move again.

MOVE_WAITING = "waiting"


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


def _download_job_for(key):
    """Return True if a download of key waits, runs, or waits for a retry."""

    return any(
        job.func_name == "app.archive.downloads.download_task"
        and job.args
        and job.args[0] == key
        for job in live_jobs(["file"])
    )


def _download_after_move(file, source, target):
    """Download the moved archive if a restore for a download preceded the move.

    The restore notification of the old key went to the move. A request
    for a download sets the pending restore of the row. The row
    keeps it through the move. If the library file is still missing, the
    download of the new key starts now. The copy is new, and a new object
    is in STANDARD storage. Thus, it needs no restore now. A lifecycle
    rule moves it to Deep Archive later. A present file gets no download.
    Then the pending restore ends here."""

    if file.restore_requested_at is None:
        return
    if os.path.isfile(os.path.join(current_app.config["LIBRARY_DIR"], file.file_path)):
        current_app.logger.info(
            f"'{file.basename}' The library file is present after the archive "
            f"move. The restore of '{source}' ends with no download"
        )
        restores.end_restore_request(file.id)
        return
    enqueue_task(
        "app.archive.downloads.download_task",
        target,
        os.path.basename(target),
        description=f"'{os.path.basename(target)}' — Downloading from AWS",
    )


def move_archive_object(file_id, sqs_receipt_handle=None):
    """Move the archive of a file to its pending target key.

    The job holds the lock of the title. It reads the row and computes
    the key again. A rename after the request changes the wanted key.
    Then the newest key applies. A name that changed back clears the
    target, and nothing moves.

    - The old object is missing: the target is cleared. The S3 sync
      reports the key. Return False.
    - The old object is in Deep Archive with no restored copy, or the
      copy expired: a Bulk restore is requested. Its notification starts
      this job again. Return MOVE_WAITING.
    - A download of the old key is queued, or the weekly S3 sync runs: the
      job waits 25 to 35 minutes. Return MOVE_WAITING.
    - The old object is readable: a server-side copy goes to the target.
      The size and the ETag of the copy are verified. Then the row names
      the new key, and the old key is deleted. Return True.
    - A download of the old key came during the copy: the copy is
      deleted, and the job waits as above.

    aws_key names a real object during the whole move. sqs_receipt_handle
    is the restore notification that started the job. The job deletes it
    when the notification is handled."""

    with app.app_context():
        config = current_app.config

        # The message is about the source key of the move. The poll gives
        # it to the job, or the job reads it from the row. The early
        # returns below delete that message too.

        job = get_current_job()
        message_key = (job.meta or {}).get("archive_key") if job else None
        file = db.session.get(File, int(file_id))
        if file is None:
            current_app.logger.warning(
                f"File id {file_id} no longer exists. Its archive move stops"
            )
            restores.delete_download_message(
                sqs_receipt_handle, message_key, note="archive move of a deleted record"
            )
            return False
        message_key = message_key or archive_keys.move_source(file) or file.aws_key
        # A row with no target has no move to do. A key that lags its
        # name is not a reason to move. Only a converge run moves such a
        # key.

        if not file.aws_key_target:
            current_app.logger.info(
                f"'{file.basename}' No archive move is pending. The job stops"
            )
            restores.delete_download_message(
                sqs_receipt_handle, message_key, note="archive move with no target"
            )
            return True

        lock = videos.acquire_lock_or_defer(
            file.file_identifier(),
            config["UPLOAD_TASK_TIMEOUT"] * 1000,
            current_app.file_queue,
            "app.archive.moves.move_archive_object",
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
            return _move_archive(file, sqs_receipt_handle, message_key)
        finally:
            current_app.lock_manager.unlock(lock)


def _sync_is_running():
    """Return True if the weekly S3 sync job runs on the maintenance queue."""

    name = "app.archive.sync.sync_aws_s3_storage_task"
    queue = current_app.maintenance_queue
    for job_id in StartedJobRegistry(queue=queue).get_job_ids():
        job = queue.fetch_job(job_id)
        if job is not None and job.func_name == name:
            return True
    return False


# A move that waits books its retry at a random time in this range, in
# minutes. The sync books its own retry 5 minutes after it sees a move.
# With 2 fixed periods, the 2 retries can fall due in the same tick of
# the scheduler again and again. Then each sees the other and waits.
# The random delay breaks that alignment.

MOVE_RETRY_MINUTES = (25, 35)

# A sync that sees a move in the registries books a retry and stops in
# less than a second. A move that sees the sync waits this many seconds
# and checks again before it books its own retry.

SYNC_RECHECK_SECONDS = 10


def _move_retry_delay():
    """Return the random delay before the retry of a move that waits."""

    low, high = MOVE_RETRY_MINUTES
    return timedelta(seconds=random.uniform(low * 60, high * 60))


def _wait_move(file, source, sqs_receipt_handle, waits_for):
    """Book the archive move again in 25 to 35 minutes, and return MOVE_WAITING.

    The retry keeps the receipt handle and the key of the restore
    notification. Then the poll can extend the restore and the message,
    and the retry deletes the message when the move is done."""

    requeue(
        "app.archive.moves.move_archive_object",
        key=file.id,
        delay=_move_retry_delay(),
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


# _archive_lost_upload returns this when the library volume is dead.

LOST_UPLOAD_WAIT = "wait"

# The keys of the records whose rip archive is lost. Each needs a
# re-rip. The dangling-claims mail names them in a section of their own.

ARCHIVE_LOST_KEY = "fitzflix:archive:lost"


def mark_archive_lost(file, source):
    """Record that the pending upload of a record is lost, for the weekly report."""

    current_app.logger.warning(
        f"'{file.basename}' The pending upload '{source}' is lost. The record "
        f"needs a re-rip"
    )
    try:
        current_app.redis.hset(ARCHIVE_LOST_KEY, source, file.basename)
    except Exception:
        current_app.logger.warning(traceback.format_exc())


def _archive_lost_upload(file, source, column):
    """Archive the library file of a record whose run-scoped upload is gone.

    A lifecycle expiry or a prune can remove the pending upload of an
    import before its move. The untouched rip is then lost. The library
    file is the best copy that remains. It goes to modified/, and the
    record names that key. Return True after the try of that upload. A
    pending restore refuses the upload, and the result is still True. A
    failed upload raises.
    Return LOST_UPLOAD_WAIT if the library volume does not respond. Then
    the move waits, because the file can come back.
    Return False otherwise."""

    if column != "aws_key" or not archive_keys.is_pending_archive(source):
        return False
    path = os.path.join(current_app.config["LIBRARY_DIR"], file.file_path)
    if videos._dead_volumes([path]):
        return LOST_UPLOAD_WAIT
    if file.converted_from_quality:
        mark_archive_lost(file, source)
        return False
    if not os.path.isfile(path):
        mark_archive_lost(file, source)
        return False
    current_app.logger.warning(
        f"'{file.basename}' The pending upload '{source}' is not in S3. The "
        f"library file is archived instead"
    )
    clear_archive_target(file)
    db.session.commit()
    uploads.archive_library_file(file, path, delete_replaced=False)
    return True


def _move_archive(file, sqs_receipt_handle, message_key):
    """Do the work of move_archive_object under the lock of the title.

    message_key is the key of the restore message of the job. Each drop
    uses it, also after the switch of the row to the new key."""

    config = current_app.config
    db.session.refresh(file)

    # The job moves only the archive of the column of its target. A
    # lagging key in the other column waits for a converge run.
    # A target that went during the wait leaves nothing to move.

    column = archive_keys._target_column(file) if file.aws_key_target else None
    drift = archive_keys.archive_drift(file, column=column) if column else None
    if drift is None:
        if file.aws_key_target:
            clear_archive_target(file)
            db.session.commit()
        current_app.logger.info(
            f"'{file.basename}' The archive of the move has the key of its "
            f"name already, or no move is pending"
        )
        restores.delete_download_message(
            sqs_receipt_handle, message_key, note="archive move with no work"
        )
        return True
    source, target = drift
    if file.aws_key_target != target:
        current_app.logger.info(
            f"'{file.basename}' The archive '{source}' now moves to '{target}'"
        )
        _set_archive_target(file, target)

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
    # the idle check of the sync waits for the sync in return. A sync
    # that started in the same tick as this move sees the move and stops
    # at once. Thus, a short pause and a second check let the move go on.

    if _sync_is_running():
        time.sleep(SYNC_RECHECK_SECONDS)
        if _sync_is_running():
            return _wait_move(file, source, sqs_receipt_handle, "the S3 sync")

    s3_client = aws_clients.aws_s3_client(with_retries=True)
    bucket = config["AWS_BUCKET"]
    try:
        head = s3_client.head_object(Bucket=bucket, Key=source)
    except botocore.exceptions.ClientError as error:
        if not aws_clients._is_missing(error):
            raise
        lost = _archive_lost_upload(file, source, column)
        if lost == LOST_UPLOAD_WAIT:
            return _wait_move(
                file, source, sqs_receipt_handle, "the library volume to return"
            )
        if lost:
            restores.delete_download_message(
                sqs_receipt_handle, message_key, note="archive move of a missing object"
            )
            return True
        current_app.logger.warning(
            f"'{file.basename}' The archive '{source}' is not in S3. The move to "
            f"'{target}' stops. The S3 sync reports the key"
        )
        clear_archive_target(file)
        db.session.commit()
        restores.delete_download_message(
            sqs_receipt_handle, message_key, note="archive move of a missing object"
        )
        return False

    if not aws_clients._object_is_readable(head):
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
            restores.aws_restore(source, tier="Bulk", record_request=False)
        restores.delete_download_message(
            sqs_receipt_handle, message_key, note="stale message"
        )
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
        # System page show it, with a Requeue button. The target stays.
        # Thus, the same job makes the move again. The System page shows
        # the first 200 characters of the last line, so the repair is the
        # first sentence. After Forget, only flask aws
        # converge-keys --apply --again requests the move. With no
        # --limit, that command requests each move of its list. The
        # weekly sync requests again only a move from pending-untouched/.
        s3_layout.delete(s3_client, bucket, target, copied.get("VersionId"))
        raise RuntimeError(
            f"The copy of an archive failed. Requeue this task on the System "
            f"page to copy again. '{file.basename}': the copy at the new key "
            f"{problem}. The archive keeps its key '{source}', and the copy at "
            f"'{target}' is deleted"
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
        if copied.get("VersionId") or not archive_keys.untouched_key_still_claimed(
            target
        ):
            s3_layout.delete(s3_client, bucket, target, copied.get("VersionId"))
        return False

    # Invariant: a move never takes a key away from its download.
    # The copy can take minutes, and a person can request a restore in
    # that time. A readable object gets its download at once. That job
    # finds its record by the old key when it starts, and it reads the
    # old key. Thus, the move asks for the job again here, before the row
    # changes. Then the row and the old key stay, and the own copy goes.
    # The move waits, as it does for a download that was there before the
    # copy.

    if _download_job_for(source):
        if copied.get("VersionId") or not archive_keys.untouched_key_still_claimed(
            target
        ):
            s3_layout.delete(s3_client, bucket, target, copied.get("VersionId"))
        return _wait_move(
            file, source, sqs_receipt_handle, f"the download of '{source}'"
        )

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

    if not archive_keys.untouched_key_still_claimed(source):
        # The run-scoped upload of an import was never an archive
        # of its own. Its version goes permanently.
        s3_layout.delete(
            s3_client,
            bucket,
            source,
            head.get("VersionId") if archive_keys.is_pending_archive(source) else None,
        )
    _download_after_move(file, source, target)
    restores.delete_download_message(
        sqs_receipt_handle, message_key, note="archive move"
    )

    # A conversion has a second archive. After the move of the original,
    # the move of the transcoded/ copy comes next. The move of a
    # transcoded/ copy never requests the move of the original.

    if column == "aws_key" and request_archive_move(file, column="aws_transcoded_key"):
        db.session.commit()
    return True


def aws_delete(key, only_if_present=False, only_if_unclaimed=False):
    """Delete an object from AWS S3 storage.

    only_if_present reads the head first. A key with no object is left
    alone. The purge of a pending move named its target.
    On the versioned bucket, a plain delete wrote a delete marker at a
    key that did not exist yet.

    only_if_unclaimed asks untouched_key_still_claimed immediately before
    the delete. A key that a record claims now is left alone. A
    queued delete can wait for hours, and an import can claim the key
    in that time. A job that the older code queued has no such argument.
    It deletes as before."""

    # This needs app.app_context(). Thus, the web application can call it
    # directly.
    with app.app_context():
        if only_if_unclaimed and archive_keys.untouched_key_still_claimed(key):
            current_app.logger.info(
                f"Keeping '{key}' in AWS. A file record claims it now"
            )
            return None
        current_app.logger.info(f"Preparing to delete '{key}' from AWS...")
        s3_client = aws_clients.aws_s3_client()
        if only_if_present:
            try:
                s3_client.head_object(Bucket=current_app.config["AWS_BUCKET"], Key=key)
            except botocore.exceptions.ClientError as error:
                if str(error.response.get("Error", {}).get("Code")) in (
                    "404",
                    "NoSuchKey",
                    "NotFound",
                ):
                    current_app.logger.info(
                        f"'{key}' has no object in AWS S3 storage. Nothing to delete"
                    )
                    return None
                raise
        s3_layout.delete(s3_client, current_app.config["AWS_BUCKET"], key)
        current_app.logger.info(f"'{key}' deleted from AWS S3 storage")
        return datetime.now(timezone.utc)
