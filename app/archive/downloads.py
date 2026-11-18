"""The download of an archive into the library."""

import os
import re
import time
import traceback
from datetime import timedelta
import botocore
import requests
from rq import get_current_job
from flask import current_app
from werkzeug.local import LocalProxy
from app.tasks import enqueue_task, report_progress, requeue
from app import db, get_app
from app.media import naming
from app.models import ArchivedOriginal, File
from app.media import videos
from app.archive import aws_clients, cdn, restores
from app.plex.plex_library import enqueue_plex_analyze

app = LocalProxy(get_app)


class PlacementDeferred(Exception):
    """A volume of a direct restore is not available at the placement (#533).

    The first argument is the list of the dead volumes. reason replaces
    the volume text of the log line for a transient fault of a check
    (#598)."""

    def __init__(self, dead, reason=None):
        super().__init__(dead)
        self.reason = reason


def _transient_placement_fault(error):
    """Return True if a fault of a placement check can clear by itself (#598).

    These are a throttle or a server error of S3, a lost connection to
    AWS, and a lost connection to the database. A lost connection to
    Redis and a Redis timeout at the title lock are transient too (#598
    follow-up, #610). A missing object and an access fault are not
    transient."""

    import redis.exceptions
    import sqlalchemy.exc

    if isinstance(error, botocore.exceptions.ClientError):
        code = str(error.response.get("Error", {}).get("Code", ""))
        status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        return not (
            aws_clients._is_missing(error)
            or code in NON_RETRYABLE_DOWNLOAD_ERRORS
            or status == 403
        )
    return isinstance(
        error,
        (
            botocore.exceptions.ConnectionError,
            botocore.exceptions.HTTPClientError,
            sqlalchemy.exc.OperationalError,
            redis.exceptions.ConnectionError,
            redis.exceptions.TimeoutError,
        ),
    )


class PlacementRefused(Exception):
    """A direct restore must not replace the library file any more (#533)."""


# A direct restore waits for a dead volume at most this many times, 5
# minutes each. 288 waits are 1 day. The SQS message then comes back.

MAX_VOLUME_WAITS = 288


def download_task(
    key,
    basename,
    sqs_receipt_handle=None,
    transient_retries=0,
    volume_waits=0,
):
    """Download a file from AWS S3 storage.

    A direct restore writes into the library. A dropped share leaves its
    mountpoint as a plain directory on the boot disk, and a write there
    succeeds with no error. Thus, the task waits while a volume is dead,
    before the download and again before the placement (#533). The
    newer-file guards also run again before the placement. They read the
    row again, and the placement goes to its fresh path (#563).

    The row of the key holds the pending restore (#610). While it is
    set, each caller of refuse_while_restore_pending refuses. The
    comment at that function has the list. The title
    lock covers only the final copy. A rename can still run during the
    download. The placement reads the path of the row under the lock.
    The end of the task clears the state by the row id, after the lock
    is released. A writer that comes first sees the state and refuses.
    A booked retry and a new restore of the object keep the state. A
    give-up that leaves the SQS message keeps the state too (#658). The
    next delivery of the message then downloads again.

    The newest receipt handle of the message is under the key (#617)."""

    # A modified/ or transcoded/ object is a library file (#299). It goes
    # straight to the library path of its record, with no import. The id
    # stays readable after a rollback. The rollback expires the row, and
    # a deleted row then raises (#588).

    with app.app_context():
        file = File.query.filter(File.aws_key == key).first()
        if file is None:
            file = File.query.filter(File.aws_transcoded_key == key).first()
        file_id = file.id if file is not None else None

    # A fault that escapes the task leaves the SQS message in the queue.
    # Thus, the state stays unless a status says otherwise. A download
    # that started with no message has nothing that comes back. Its
    # give-up ends the state, and the person can request again.

    booked = []
    outcome = DOWNLOAD_GAVE_UP
    try:
        outcome = _download_task(
            key,
            basename,
            sqs_receipt_handle,
            transient_retries,
            volume_waits,
            booked,
            file_id,
        )
        # A give-up is a failure of the job. Each other status of
        # aws_download is a handled message. The job returns True.
        if outcome == DOWNLOAD_GAVE_UP:
            return False
        return True if isinstance(outcome, str) else outcome
    finally:
        with app.app_context():
            kept = outcome == DOWNLOAD_RESTORE_PENDING or (
                outcome == DOWNLOAD_GAVE_UP
                and restores._message_comes_back(sqs_receipt_handle, key)
            )
            if not booked and not kept:
                restores.end_restore_request(file_id)


def _download_task(
    key,
    basename,
    sqs_receipt_handle,
    transient_retries,
    volume_waits,
    booked,
    file_id,
):
    """Run download_task. Return the status of aws_download, True or False.

    file_id is the row of the key, or None. A booked retry appends True
    to booked. A give-up that leaves the SQS message returns
    DOWNLOAD_GAVE_UP (#658)."""

    with app.app_context():
        job = get_current_job()
        file = db.session.get(File, file_id) if file_id is not None else None
        place_at = None
        if file is not None and restores.restores_directly(key):
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

        def discard_kept_download():
            """Remove a finished download that an earlier round kept (#588).

            A PlacementDeferred round keeps the copy and its sidecar. The
            orphan sweep keeps a complete copy. Thus, a round that stops
            removes it."""

            if place_at:
                cdn._discard_partial(restore_staging_path(key))

        if place_at and restores.restore_would_replace_newer(file, key):
            current_app.logger.warning(
                f"'{basename}' The library file is newer than its archive "
                f"'{key}'. The restore does not replace it. Archive the file "
                f"again first"
            )
            discard_kept_download()
            return restores.delete_download_message(
                sqs_receipt_handle, key, note="stale restore"
            )

        # The pending restore of the row ended (#620). An import replaced
        # the file, or the 14-day sweep cleared the state. The download
        # would replace the new file with the old one. Thus, drop the
        # message and download nothing. A key with no File row (an
        # archive-only original) has no state.

        if file is not None and file.restore_requested_at is None:
            current_app.logger.info(
                f"'{basename}' has no pending restore any more, skipping the download"
            )
            discard_kept_download()
            return restores.delete_download_message(
                sqs_receipt_handle, key, note="ended restore"
            )

        def dead_volumes():
            paths = [current_app.config["IMPORT_DIR"]]
            if place_at:
                paths.append(os.path.dirname(place_at))
            return videos._dead_volumes(paths)

        def wait_for_volumes(dead, reason=None):
            """Book the download again after 5 minutes, or give up.

            reason replaces the volume text for a transient fault of a
            placement check (#598)."""

            reason = (
                reason or f"Volumes unavailable ({', '.join(dead)}) for the restore"
            )
            if volume_waits >= MAX_VOLUME_WAITS:
                current_app.logger.error(
                    f"'{basename}' {reason} after {volume_waits} waits; giving up"
                )
                discard_kept_download()
                return DOWNLOAD_GAVE_UP
            handle = restores._latest_receipt_handle(sqs_receipt_handle, key)
            counters = {
                "transient_retries": transient_retries,
                "volume_waits": volume_waits + 1,
            }
            requeue(
                "app.archive.downloads.download_task",
                key=key,
                delay=timedelta(minutes=5),
                reason=reason,
                counters=counters,
                args=(key, basename, handle),
                description=f"'{basename}' — Downloading from AWS",
                meta={"sqs_receipt_handle": handle},
            )
            booked.append(True)
            return False

        dead = dead_volumes()
        if dead:
            return wait_for_volumes(dead)

        # The title lock of the placement (#610). before_place takes it,
        # and the finally block below releases it.

        held = []

        def release_title_lock():
            """Release the title lock. A fault only logs, and the lock expires.

            The release runs after the placement. A fault then must not
            fail a restore that is done."""

            while held:
                try:
                    current_app.lock_manager.unlock(held.pop())
                except Exception:
                    current_app.logger.warning(
                        f"'{basename}' Could not release the title lock: "
                        + traceback.format_exc()
                    )

        def before_place(etag=None):
            """Take the title lock and run the placement checks. Defer on a transient fault (#598).

            A throttle of an S3 HEAD or a restart of the database is not a
            reason to discard a finished download. A busy title lock is
            not a reason either (#610). A PlacementDeferred keeps the copy
            and its sidecar."""

            try:
                return check_placement(etag)
            except (PlacementDeferred, PlacementRefused):
                raise
            except Exception as error:
                if not _transient_placement_fault(error):
                    raise
                raise PlacementDeferred(
                    [],
                    reason=(
                        f"A placement check of the restore failed with a "
                        f"transient fault ({type(error).__name__}: {error})"
                    ),
                ) from error

        def fresh_row():
            """Read the row again in a new transaction. Refuse if it has no path."""

            db.session.rollback()
            fresh = db.session.get(File, file_id)
            if fresh is None or not fresh.file_path:
                raise PlacementRefused(
                    f"'{basename}' The record has no library path any more. "
                    f"The restore does not place the file"
                )
            return fresh

        def check_placement(etag=None):
            """Take the title lock, and check the volumes and the newer-file guards again (#533).

            Return the path. A rename during the download moves the file
            of the row (#563). Thus, the placement goes to the path of
            the row under the lock, and the volume probe checks that
            folder. etag is the ETag of the object at the start of the
            download."""

            lock = current_app.lock_manager.lock(
                fresh_row().file_identifier(),
                current_app.config["MOVE_TASK_TIMEOUT"] * 1000,
            )
            if not lock:
                raise PlacementDeferred(
                    [],
                    reason=(
                        "Another task holds the title lock. The placement of "
                        "the restore waits"
                    ),
                )
            held.append(lock)
            fresh = fresh_row()
            target = os.path.join(current_app.config["LIBRARY_DIR"], fresh.file_path)
            dead = videos._dead_volumes(
                [current_app.config["IMPORT_DIR"], os.path.dirname(target)]
            )
            if dead:
                raise PlacementDeferred(dead)
            if restores.restore_would_replace_newer(fresh, key):
                raise PlacementRefused(
                    f"'{basename}' The library file changed during the download. "
                    f"The restore does not replace it"
                )
            if fresh.restore_requested_at is None:
                raise PlacementRefused(
                    f"'{basename}' The pending restore ended during the download. "
                    f"The restore does not replace the file"
                )
            if etag is not None:
                s3_client = aws_clients.aws_s3_client()
                head = aws_clients._head_or_none(s3_client, key)
                if head is None:
                    # A move waits for a download of its source. It asks
                    # before the copy and again before the row changes
                    # (#621, #728). Thus, a gone key is a refusal, and the
                    # person requests the restore again.
                    raise PlacementRefused(
                        f"'{basename}' The archive '{key}' is gone. The "
                        f"restore does not place the file"
                    )
                elif head.get("ETag") != etag:
                    raise PlacementRefused(
                        f"'{basename}' The archive '{key}' changed during the "
                        f"download. The restore does not replace the file"
                    )
            if target != place_at:
                current_app.logger.info(
                    f"'{basename}' The file was renamed during the download. "
                    f"The restore places it at '{fresh.file_path}'"
                )
            return target

        report_progress(job, basename, "Downloading from AWS", percent=None)

        try:
            current_app.logger.info(
                f"Starting download of '{basename}' from AWS S3 storage"
            )
            downloaded = aws_download(
                key,
                basename,
                sqs_receipt_handle,
                place_at=place_at,
                before_place=before_place if place_at else None,
            )

        except PlacementDeferred as e:
            return wait_for_volumes(e.args[0], getattr(e, "reason", None))

        except PlacementRefused as e:
            current_app.logger.warning(str(e))
            return restores.delete_download_message(
                sqs_receipt_handle, key, note="stale restore"
            )

        except OSError as e:
            if (
                e.errno in videos.TRANSIENT_COPY_ERRNOS
                and transient_retries < videos.MAX_TRANSIENT_RETRIES
            ):
                # The import volume had a fault during the download or the
                # rename. The S3 object and the SQS message are not affected.
                # Thus, retry after the mount settles.

                # The retry is a new job. It carries the newest handle.

                sqs_receipt_handle = restores._latest_receipt_handle(
                    sqs_receipt_handle, key
                )
                counters = {
                    "transient_retries": transient_retries + 1,
                    "volume_waits": volume_waits,
                }
                requeue(
                    "app.archive.downloads.download_task",
                    key=key,
                    delay=timedelta(minutes=5),
                    reason=(
                        f"Download failed with a transient I/O error ({e}) "
                        f"(attempt {transient_retries + 1} of {videos.MAX_TRANSIENT_RETRIES})"
                    ),
                    counters=counters,
                    args=(key, basename, sqs_receipt_handle),
                    description=f"'{basename}' — Downloading from AWS",
                    meta={"sqs_receipt_handle": sqs_receipt_handle},
                )
                booked.append(True)
                return False
            current_app.logger.error(traceback.format_exc())
            return DOWNLOAD_GAVE_UP

        except Exception:
            current_app.logger.error(traceback.format_exc())
            return DOWNLOAD_GAVE_UP

        else:
            # download_task reads the status. It keeps the pending restore
            # of DOWNLOAD_RESTORE_PENDING and of DOWNLOAD_GAVE_UP. In both
            # cases the message or a new restore comes back to the task.

            if not downloaded or downloaded == DOWNLOAD_GAVE_UP:
                current_app.logger.error(
                    f"'{basename}' download from AWS S3 storage failed"
                )
            return downloaded

        finally:
            release_title_lock()


# These are the aws_download results. A failed delete of the message is
# a plain False. A give-up is DOWNLOAD_GAVE_UP. Each other status means
# that the SQS message was handled. A caller that needs to know if a
# file arrived compares the result with DOWNLOAD_COMPLETE.

DOWNLOAD_COMPLETE = "complete"
DOWNLOAD_OBJECT_MISSING = "object-missing"
# CloudFront refused the signed URL for an object that S3 can read. Only
# a change to the key pair, the key group, or the WAF allowlist clears
# it. The SQS message is dropped, so the fault cannot loop.
DOWNLOAD_ACCESS_DENIED = "access-denied"
DOWNLOAD_RESTORE_PENDING = "restore-pending"
# The download gave up and left the SQS message in the queue. SQS
# delivers it again after its visibility timeout, and that delivery
# tries again. Thus, the pending restore of the row stays (#658). This
# status is a failure. download_task returns False for it.
DOWNLOAD_GAVE_UP = "gave-up"

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


def _spend_download_retry(retry, basename, detail=None):
    """Spend 1 in-place download retry, and wait before the next attempt.

    Return (retry, fault). fault is the text of the fault, for the
    give-up message. The wait grows exponentially (1s, 2s, 4s, ...)
    with a cap at 60s. This writes a WARNING with the attempt count. A
    retried fault is a transient. Only the give-up path of the loop
    writes an ERROR, which sends the failure mail."""

    fault = str(detail) if detail is not None else traceback.format_exc().strip()
    attempt = MAX_DOWNLOAD_RETRIES - retry + 1
    retry -= 1
    outcome = "retrying" if retry > 0 else "no retries left"
    current_app.logger.warning(
        f"'{basename}' download attempt {attempt} of {MAX_DOWNLOAD_RETRIES} "
        f"failed, {outcome}: {fault}"
    )
    if retry > 0:
        DOWNLOAD_RETRY_SLEEP(min(2 ** (MAX_DOWNLOAD_RETRIES - 1 - retry), 60))
    return retry, fault


def _resolve_missing_object(sqs_client, key, basename, sqs_receipt_handle):
    """Report an object that is not at AWS, and drop its SQS message.

    Return DOWNLOAD_OBJECT_MISSING, or False if the delete failed."""

    current_app.logger.info(f"'{basename}' doesn't exist in AWS S3")
    if not restores.delete_download_message(
        sqs_receipt_handle, key, sqs_client=sqs_client
    ):
        return False
    return DOWNLOAD_OBJECT_MISSING


def _refuse_access_fault(sqs_client, key, basename, sqs_receipt_handle):
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
    if not restores.delete_download_message(
        sqs_receipt_handle, key, note="CloudFront access fault", sqs_client=sqs_client
    ):
        return False
    return DOWNLOAD_ACCESS_DENIED


def _defer_to_restore(sqs_client, key, basename, sqs_receipt_handle, head_response):
    """Hand the download to the next restore-completed notification.

    The restored copy expired before the download. Thus, the object is
    back in cold storage, and this SQS message is stale. Request a new
    restore, unless one is already in progress. Its completion
    notification triggers the download again. The request again is for
    the same rows. Thus, their pending restore stays (#610). Return
    DOWNLOAD_RESTORE_PENDING, also if the message delete failed. The
    restore is on its way in both cases. The failed delete has its own
    warning, and the message comes back to this function."""

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
        restores.aws_restore(key, record_request=False)

    restores.delete_download_message(
        sqs_receipt_handle, key, note="stale message", sqs_client=sqs_client
    )
    return DOWNLOAD_RESTORE_PENDING


def place_in_library(downloaded, library_path, expected_size, name):
    """Move a restored library file from the import directory into the library (#299).

    The download is a modified/ or transcoded/ object: the library file
    itself. It needs no import. Its size must match the S3 object. The
    copy goes to a hidden name beside the library file. Then 1 rename
    puts it in place. The track rows and the triage verdicts of the
    record stay."""

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
        videos.copy_with_progress(
            downloaded, hidden, None, name, "Moving into the library"
        )
        os.replace(hidden, library_path)
    except BaseException:
        if os.path.exists(hidden):
            os.remove(hidden)
        raise
    os.remove(downloaded)
    current_app.logger.info(f"'{name}' Restored straight into the library")
    enqueue_plex_analyze(library_path)


# A restored rip waits this long when a file with its untouched name
# waits in the import directory (#433). It tries again up to
# MAX_RESTORED_IMPORT_WAITS times.

RESTORED_IMPORT_WAIT = timedelta(minutes=10)
MAX_RESTORED_IMPORT_WAITS = 36


def brace_edition_tags(basename):
    """Rewrite each "(edition-foo bar)" of a name as "{edition-foo bar}".

    The match stops at the first closing parenthesis (#534). The greedy
    form ran to the last one. Thus, "(edition-X) - Scenes - Apostrophe(')
    Ad" became "{edition-X) - Scenes - Apostrophe('} Ad"."""

    if "(edition-" not in basename:
        return basename
    return re.sub(r"\(edition-(?P<edition>[^)]+)\)", "{edition-\\g<edition>}", basename)


def _rename_without_replace(source, target):
    """Rename source to target. Return False, and change nothing, if target exists.

    A hard link fails when the target exists. Thus, 2 renames to 1 name
    cannot both succeed. A volume with no hard links falls back to a
    check and a rename."""

    try:
        os.link(source, target)
    except FileExistsError:
        return False
    except OSError:
        if os.path.lexists(target):
            return False
        os.rename(source, target)
        return True
    os.remove(source)
    return True


def place_restored_import(staged, basename):
    """Give a restored rip its untouched name in the import directory. Return False if the name is taken.

    Another restore with the same untouched name can wait there for its
    import (#433). That file stays. The caller tries again later."""

    target = os.path.join(current_app.config["IMPORT_DIR"], basename)
    if _rename_without_replace(staged, target):
        return True
    current_app.logger.warning(
        f"'{basename}' A file with this name waits in the import directory. "
        f"The restored copy waits as '{os.path.basename(staged)}'"
    )
    return False


def place_restored_import_task(staged, basename, waits=0):
    """Try again to give a restored rip its untouched name (#433)."""

    with app.app_context():
        if not os.path.exists(staged):
            current_app.logger.warning(f"'{staged}' is gone. Nothing to place")
            return False
        if place_restored_import(staged, basename):
            current_app.logger.info(f"'{basename}' The restored copy is in place")
            return True
        if waits + 1 >= MAX_RESTORED_IMPORT_WAITS:
            current_app.logger.error(
                f"'{basename}' The import name stayed taken. The restored copy "
                f"stays as '{staged}' for the admin"
            )
            return False
        enqueue_task(
            "app.archive.downloads.place_restored_import_task",
            staged,
            basename,
            waits=waits + 1,
            delay=RESTORED_IMPORT_WAIT,
            description=f"'{basename}' — Waiting for the import name",
        )
        return True


def restore_staging_path(key):
    """Return the staging path of the download of an S3 key in the import directory.

    A library basename is not unique: many films have a "Restoration
    Demonstration.mkv". The untouched name is not sufficient either. The
    2 archives of a conversion share it, and 2 keys can share 1
    untouched name (#433). Thus, the staging name is the downloaded key,
    with its prefix: 1 file for each object."""

    return os.path.join(
        current_app.config["IMPORT_DIR"], f".restore-{key.replace('/', '-')}"
    )


def aws_download(
    key,
    basename,
    sqs_receipt_handle=None,
    place_at=None,
    before_place=None,
):
    """Download an object from AWS S3 storage.

    Return DOWNLOAD_COMPLETE if the file arrived in the import directory.
    Return DOWNLOAD_OBJECT_MISSING if there is no such object at AWS.
    Return DOWNLOAD_RESTORE_PENDING if the object is in cold storage.
    Then the completion notification of the restore triggers the
    download again. Return DOWNLOAD_ACCESS_DENIED if CloudFront refused
    an object that S3 can read. These 4 values mean that the SQS
    message was handled. Return DOWNLOAD_GAVE_UP if the retry budget was
    used up or the CloudFront settings have a problem. Then the message
    stays for a later delivery. Return False if the SQS message could not
    be deleted. download_task ends the pending restore of the row after
    each status but DOWNLOAD_RESTORE_PENDING and DOWNLOAD_GAVE_UP (#610,
    #658).

    When AWS_DOWNLOAD_VIA_CDN is set, the bytes come through CloudFront
    with a signed URL. The object size check and the restore-state
    checks still use the S3 API. A stale CloudFront copy changes the
    rest of the attempts to the S3 transfer (#607).

    before_place runs after the download and before a direct placement.
    It gets the ETag of the object at the start of the download. A
    PlacementDeferred keeps the finished download and its sidecar for
    the next try (#533). Any other error deletes the download and
    propagates. The caller decides about the SQS message. If it returns
    a path, the placement goes there, not to place_at (#563).
    """

    retry = MAX_DOWNLOAD_RETRIES

    basename = brace_edition_tags(basename)

    via_cdn = cdn.cdn_download_enabled()
    if via_cdn:
        problem = cdn.cdn_settings_problem()
        if problem:
            # A silent fallback to S3 egress would spend money that the
            # operator chose not to spend. The SQS message stays in the
            # queue for a later delivery.

            current_app.logger.error(
                f"'{basename}' CloudFront download is enabled, but "
                f"{problem}; giving up"
            )
            return DOWNLOAD_GAVE_UP

    source = "AWS CloudFront" if via_cdn else "AWS S3 storage"
    current_app.logger.info(f"'{basename}' downloading from {source}")

    bucket = current_app.config["AWS_BUCKET"]

    # An import download takes its untouched name after the download,
    # because the import parses that name. The rename never replaces a
    # file of that name. The rename starts the import.

    destination = restore_staging_path(key)
    s3_client = aws_clients.aws_s3_client()
    sqs_client = aws_clients.aws_sqs_client()

    # The give-up ERROR names the last fault. The failure mail then says
    # why, not only which file.

    last_fault = None
    while retry > 0:
        try:
            progress = aws_clients.DownloadProgressPercentage(
                s3_client, bucket, key, basename
            )
            if place_at and cdn._finished_download(destination, progress):
                # A placement that waited for a volume kept its finished
                # download (follow-up #533). Each wait downloaded the
                # whole object again.
                current_app.logger.info(
                    f"'{basename}' The finished download waits in staging. "
                    f"It is not downloaded again"
                )
            elif via_cdn:
                cdn.cdn_download(
                    key,
                    destination,
                    progress,
                    expected_size=progress.size,
                    etag=progress.etag,
                )
            else:
                # The S3 transfer writes the whole object again.
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
                cdn._discard_partial(destination)
                return _resolve_missing_object(
                    sqs_client, key, basename, sqs_receipt_handle
                )

            elif error_code == "InvalidObjectState":
                try:
                    head_response = s3_client.head_object(Bucket=bucket, Key=key)
                except Exception:
                    # A failed status check spends a retry, as each other
                    # error does. It does not exit the loop.

                    retry, last_fault = _spend_download_retry(retry, basename)
                    continue
                cdn._discard_partial(destination)
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
                retry, last_fault = _spend_download_retry(retry, basename)

        except cdn.CdnDownloadError as error:
            if error.status_code == 404:
                cdn._discard_partial(destination)
                return _resolve_missing_object(
                    sqs_client, key, basename, sqs_receipt_handle
                )

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
                        cdn._discard_partial(destination)
                        return _resolve_missing_object(
                            sqs_client, key, basename, sqs_receipt_handle
                        )
                    retry, last_fault = _spend_download_retry(retry, basename)
                    continue
                except Exception:
                    retry, last_fault = _spend_download_retry(retry, basename)
                    continue
                if aws_clients._object_is_readable(head_response):
                    cdn._discard_partial(destination)
                    return _refuse_access_fault(
                        sqs_client, key, basename, sqs_receipt_handle
                    )
                else:
                    cdn._discard_partial(destination)
                    return _defer_to_restore(
                        sqs_client, key, basename, sqs_receipt_handle, head_response
                    )

            elif error.status_code in cdn.RETRYABLE_CDN_STATUS_CODES:
                retry, last_fault = _spend_download_retry(retry, basename, error)

            else:
                current_app.logger.error(f"'{basename}' {error}; giving up")
                retry = 0

        except cdn.CdnStaleObject as error:
            # The signature is not part of the cache key. Each new signed
            # URL reaches the same cached copy. Thus, the rest of the
            # chain uses the S3 transfer, and spends no retry on the
            # cache (#607). The warning makes the egress cost visible.

            current_app.logger.warning(
                f"'{basename}' {error}. The download changes to the S3 "
                f"transfer, which costs S3 egress"
            )
            via_cdn = False
            source = "AWS S3 storage"

        except requests.exceptions.RequestException:
            # A transport fault (connection, timeout, a broken body) spends
            # a retry. The next attempt signs a new URL.

            retry, last_fault = _spend_download_retry(retry, basename)

        except OSError as e:
            if e.errno in videos.TRANSIENT_COPY_ERRNOS:
                # A dead import volume fails immediately. Thus, the whole
                # in-place retry budget would be wasted on it. Delete the
                # partial download. Let the caller defer until the mount
                # settles.

                try:
                    os.remove(destination)
                except OSError:
                    pass
                raise
            retry, last_fault = _spend_download_retry(retry, basename)

        except Exception:
            retry, last_fault = _spend_download_retry(retry, basename)

        else:
            current_app.logger.info(f"'{basename}' downloaded from {source}")

            if place_at:
                target = place_at

                # The download is complete. The sidecar marks it for the
                # next run and for the orphan sweep. A PlacementDeferred
                # or a transient fault of the placement then does not
                # download the object again (#588).

                cdn._write_partial_etag(destination, progress.etag, progress.size)
                if before_place:
                    try:
                        target = before_place(progress.etag) or place_at
                    except PlacementDeferred:
                        raise
                    except BaseException:
                        cdn._discard_partial(destination)
                        raise
                place_in_library(destination, target, progress.size, basename)
                cdn._remove_partial_etag(destination)
            elif not place_restored_import(destination, basename):
                enqueue_task(
                    "app.archive.downloads.place_restored_import_task",
                    destination,
                    basename,
                    delay=RESTORED_IMPORT_WAIT,
                    description=f"'{basename}' — Waiting for the import name",
                )

            if not restores.delete_download_message(
                sqs_receipt_handle, key, sqs_client=sqs_client
            ):
                return False
            return DOWNLOAD_COMPLETE

    current_app.logger.error(
        f"'{basename}' could not be downloaded from {source}; giving up"
        + (f". Last fault: {last_fault}" if last_fault else "")
    )

    cdn._discard_partial(destination)
    return DOWNLOAD_GAVE_UP
