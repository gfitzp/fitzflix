"""The upload of a file to its archive."""

import os
import traceback
from datetime import datetime, timezone
import boto3
from rq import get_current_job
from flask import current_app
from werkzeug.local import LocalProxy
from app.tasks import enqueue_task
from app import db, get_app
from app.archive import s3_layout
from app.models import File
from app.media import videos
from app.archive import archive_keys, aws_clients, moves, restores

app = LocalProxy(get_app)


def upload_task(file_id):
    """Upload the library file of a record to its AWS S3 archive.

    The task holds the title lock of the file (#615). Thus, no writer
    changes the file during the upload, and the clear of the stale flag
    is true. A failure leaves the flag as it is.

    The invariant of the chip (#766): only an upload that stored its
    archive ends "done" in the stamp of the worker. An upload that raises
    rolls back, releases the lock and raises again. Thus, its job fails,
    and the worker reports it (#759). A refused upload returns False with
    a "skipped" chip. A job that meets the title lock books the lock
    wait, and the retry chip stands for the stage. That job also marks
    itself "skipped", because a wait that is booked already adds no new
    chip. The trail hides such a chip behind the chip of the wait. Only
    the lock wait job itself does not mark itself. A job whose record is
    gone marks itself too.

    The trail can heal a chip whose stamp was lost. It reads the mark in
    the job hash while that hash exists."""

    from app.pipeline import clear_job_skipped, mark_job_skipped

    clear_job_skipped()
    with app.app_context():
        file = db.session.get(File, int(file_id))
        if file is None:
            # The record went away while the job waited. No archive was
            # stored, so the chip must not heal to "done".
            mark_job_skipped()
            return False
        config = current_app.config
        description = f"'{file.basename}'"
        lock = videos.acquire_lock_or_defer(
            file.file_identifier(),
            config["UPLOAD_TASK_TIMEOUT"] * 1000,
            current_app.file_queue,
            "app.archive.uploads.upload_task",
            minutes=(5, 15),
            timeout=config["UPLOAD_TASK_TIMEOUT"],
            description=description,
            args=(file_id,),
            key=int(file_id),
        )
        if not lock:
            # A second upload job of the file can meet the lock while the
            # wait is booked. Then no new chip takes the place of its chip,
            # and that chip must not end "done" (#766 review). The wait
            # job books itself under its own id, and rq keeps the meta of
            # that hash for the booking. Thus, the wait job does not mark
            # itself.

            job = get_current_job()
            wait_id = videos.lock_wait_job_id(
                "app.archive.uploads.upload_task",
                description,
                (file_id,),
                key=int(file_id),
            )
            if job is not None and job.id != wait_id:
                mark_job_skipped()
            return False

        try:
            # The row can have changed while the task waited for the lock.

            db.session.refresh(file)
            file_path = os.path.join(config["LIBRARY_DIR"], file.file_path)

            # The library file goes to the archive of the record: modified/
            # for a normal file, transcoded/ for a conversion (#299). A rip
            # in untouched/ is never overwritten.

            if archive_library_file(file, file_path) is None:
                mark_job_skipped()
                return False
            file.date_updated = (
                file.aws_transcoded_date_uploaded
                if file.converted_from_quality
                else file.aws_date_uploaded
            )
            db.session.commit()

        except Exception:
            # The worker reports the failure with the task name (#759). It
            # adds the traceback when aws_upload logged no error of its
            # own.

            db.session.rollback()
            raise

        finally:
            current_app.lock_manager.unlock(lock)

        return True


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
    is cleared. Thus, an edit completes the move. A pending rip of an
    import first goes to its untouched/ key. A failed copy returns None
    and changes no row (#659).

    A pending restore of the file refuses the upload (#610). The upload
    can delete the key under restore. The refusal returns None. A writer
    marks the archive stale before it writes (#615). Thus, the flag of a
    changed file stays for a later upload.

    The caller holds the title lock. This function clears the stale
    flag (#615). The cataloging of an import and the move of an archive
    to modified/ clear it too. So does a record that takes the upload of
    a failed import (_adopt_upload)."""

    refused = restores.refuse_while_restore_pending(file)
    if refused:
        current_app.logger.warning(
            f"'{file.basename}' {refused} The archive upload does not run"
        )
        return None

    config = current_app.config
    converted = bool(file.converted_from_quality)
    if converted:
        prefix = config["AWS_TRANSCODED_PREFIX"]
        existing = file.aws_transcoded_key
        column = "aws_transcoded_key"
    else:
        prefix = config["AWS_MODIFIED_PREFIX"]
        existing = (
            file.aws_key if archive_keys._in_prefix(file.aws_key, prefix) else None
        )
        column = "aws_key"
    pending = bool(file.aws_key_target) and archive_keys._target_column(file) == column
    if existing and not pending:
        key_prefix, key_name = os.path.split(existing)
    else:
        key_prefix, key_name = prefix, os.path.basename(
            archive_keys.archive_key_for(file, prefix)
        )

    key, uploaded, size = aws_upload(
        path, key_prefix, key_name=key_name, force_upload=True, ignore_etag=True
    )

    # The upload can take an hour. The transaction of this session started
    # before it. MariaDB refuses an UPDATE of a row that a different worker
    # changed after that start (error 1020). Thus, end the old transaction
    # here. The record then loads again from a new snapshot. Each caller
    # commits its own changes before this function.

    db.session.commit()

    # The row gives up a pending key only after the rip has its untouched/
    # key (#659). A delete of the pending key removes each version, and
    # the prune deletes a pending key that no row claims. Thus, the copy
    # comes before the commit that changes the key. A failed copy changes
    # no row. The stale flag stays, and a later upload tries again.

    rip = None
    replaced = getattr(file, column)
    if replaced and delete_replaced and archive_keys.is_pending_archive(replaced):
        # The archive move of the import did not run yet (#495). The rip
        # goes to its untouched/ key, as the move would do. Then the
        # delete of that key below leaves the noncurrent version for the
        # lifecycle window, as for the rip of any edited file.

        rip = _keep_pending_rip(file, replaced)
        if rip is None:
            return None

        # The copy of a large rip takes minutes. End the transaction
        # again. The record then loads from a new snapshot. An archive
        # that changed during the copy stays on the row.

        db.session.commit()
        if getattr(file, column) != replaced:
            current_app.logger.warning(
                f"'{file.basename}' The archive of the record changed during "
                f"the copy of '{replaced}'. A later archive upload tries again"
            )
            return None

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
    # The upload took time. A converge run can have set a newer target
    # meanwhile. The commit above loaded it. Thus, the target is computed
    # again, not cleared. A target that the upload completed goes.

    if file.aws_key_target and archive_keys._target_column(file) == column:
        moves.request_archive_move(file, column=column)
    db.session.commit()
    current_app.logger.info(f"'{file.basename}' Archived the library file as '{key}'")

    doomed = [rip, replaced] if rip else [replaced] if replaced else []
    # A row that the sync uploads because it has no upload date can name
    # a key with no object. A plain delete then writes a marker at a key
    # that never existed (#588).

    for doomed_key in doomed:
        if delete_replaced and not archive_keys.untouched_key_still_claimed(doomed_key):
            enqueue_task(
                "app.archive.moves.aws_delete",
                doomed_key,
                only_if_present=True,
                only_if_unclaimed=True,
                description=f"Deleting '{doomed_key}' from AWS",
            )
    return key


def _keep_pending_rip(file, pending):
    """Copy a pending upload to its untouched/ key. Return that key, or None (#495).

    The pending prefix is STANDARD. Thus, the copy needs no restore. A
    failed copy returns None. Then the caller keeps the pending key on
    the row (#659)."""

    target = archive_keys.archive_key_for(
        file, current_app.config["AWS_UNTOUCHED_PREFIX"]
    )
    s3_client = aws_clients.aws_s3_client(with_retries=True)
    bucket = current_app.config["AWS_BUCKET"]
    try:
        head = s3_client.head_object(Bucket=bucket, Key=pending)
        s3_client.copy(
            {"Bucket": bucket, "Key": pending},
            bucket,
            target,
            Config=moves._copy_config(s3_client, bucket, pending, head),
        )
        problem = moves._copy_problem(
            head, s3_client.head_object(Bucket=bucket, Key=target)
        )
    except Exception as error:
        problem = f"failed ({error})"
    if problem:
        current_app.logger.error(
            f"'{file.basename}' The copy of the pending upload '{pending}' to "
            f"'{target}' {problem}. The record keeps the pending upload. A "
            f"later archive upload tries again"
        )
        return None
    current_app.logger.info(
        f"'{file.basename}' Copied the pending upload '{pending}' to '{target}'"
    )
    return target


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

    with app.app_context():
        config = current_app.config
        file = db.session.get(File, int(file_id))
        if file is None or file.converted_from_quality:
            return False
        if not archive_keys._in_prefix(file.aws_key, config["AWS_UNTOUCHED_PREFIX"]):
            return archive_keys._in_prefix(file.aws_key, config["AWS_MODIFIED_PREFIX"])

        lock = videos.acquire_lock_or_defer(
            file.file_identifier(),
            config["UPLOAD_TASK_TIMEOUT"] * 1000,
            current_app.file_queue,
            "app.archive.uploads.move_archive_to_modified_task",
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
            refused = restores.refuse_while_restore_pending(file)
            if refused:
                current_app.logger.warning(
                    f"'{file.basename}' {refused} The archive stays in untouched/"
                )
                return False
            old_key = file.aws_key
            if not archive_keys._in_prefix(old_key, config["AWS_UNTOUCHED_PREFIX"]):
                return archive_keys._in_prefix(old_key, config["AWS_MODIFIED_PREFIX"])
            path = os.path.join(config["LIBRARY_DIR"], file.file_path)
            if not os.path.isfile(path):
                current_app.logger.warning(
                    f"'{file.basename}' No local file. The archive stays in "
                    f"'{old_key}'"
                )
                return False

            s3_client = aws_clients.aws_s3_client(with_retries=True)
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
                key_name=os.path.basename(archive_keys.archive_key_for(file, prefix)),
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
            if file.aws_key_target and archive_keys._target_column(file) == "aws_key":
                moves.clear_archive_target(file)
            db.session.commit()
            if not archive_keys.untouched_key_still_claimed(old_key):
                # The old version goes permanently. The table entry of
                # untouched/ in app/archive/s3_layout.py records why.
                s3_layout.delete(s3_client, bucket, old_key, old.get("VersionId"))
            current_app.logger.info(
                f"'{file.basename}' Moved the archive to '{key}' and deleted "
                f"'{old_key}'. The ETags are equal"
            )
            return True
        finally:
            current_app.lock_manager.unlock(lock)


def aws_upload(
    file_path,
    key_prefix="",
    key_name=None,
    force_upload=False,
    ignore_etag=False,
    storage_class="STANDARD",
):
    """Search for a file in AWS S3. Upload it if it is missing or different."""

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
        key = aws_clients.sanitize_s3_key(key_name)

    else:
        key = aws_clients.sanitize_s3_key(os.path.basename(file_path))

    key = os.path.join(key_prefix, key)

    s3_client = aws_clients.aws_s3_client(with_retries=True)

    # If the key already exists, compare the local and the remote ETags. If
    # the ETags match, the files are the same, and a new upload is not
    # necessary. If the IGNORE_ETAGS flag is set, compare only the file and
    # key names, not their data.

    if not force_upload and not current_app.config["FORCE_UPLOAD"]:
        # A HEAD of this exact key (#572). A listing of the prefix with
        # MaxKeys=1 answered a longer sibling for a missing key. Then
        # the upload compared the file with the wrong object.

        remote_etag = None
        date_uploaded = None
        remote_size = None
        head = aws_clients._head_or_none(s3_client, key)
        if head is not None:
            remote_etag = (head.get("ETag") or "").replace('"', "")
            date_uploaded = head.get("LastModified")
            remote_size = head.get("ContentLength")

        if remote_etag is not None:
            if ignore_etag or current_app.config["IGNORE_ETAGS"]:
                current_app.logger.info(
                    f"'{file_path}' matches '{key}' and ETags are ignored, "
                    f"no need to re-upload"
                )
                return key, date_uploaded, remote_size

            local_etag = aws_clients.calculate_etag(file_path)
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
            s3_client.upload_file(
                file_path,
                current_app.config["AWS_BUCKET"],
                key,
                ExtraArgs={"StorageClass": storage_class},
                Callback=aws_clients.UploadProgressPercentage(file_path),
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
            if e.errno in videos.TRANSIENT_COPY_ERRNOS:
                current_app.logger.error(
                    f"'{file_path}' The upload failed on a transient "
                    f"filesystem error ({e.strerror}). The upload does not "
                    f"move the file. An import rejects its source after "
                    f"this error"
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


def upload_untouched(file_path, other_key):
    """Upload an untouched rip to untouched/. Never replace a claimed archive (#430).

    Return (key, date_uploaded, filesize_bytes). The normal key is the
    prefix and the sanitized basename. A surviving record can claim that
    key for different bytes, for example a re-rip with the same name.
    Then the upload goes to other_key, and the claimed object stays. A
    failed import cannot leave foreign bytes under the key of a good
    rip. other_key is a function of the normal key that returns the
    other key.

    When no record claims the normal key, a renamed record can hold
    these bytes under its older key. The function returns that key, and
    it uploads nothing (#678)."""

    config = current_app.config
    prefix = config["AWS_UNTOUCHED_PREFIX"]
    key = os.path.join(prefix, aws_clients.sanitize_s3_key(os.path.basename(file_path)))
    if config["IGNORE_ETAGS"]:
        return aws_upload(file_path, prefix)
    if not archive_keys.untouched_key_still_claimed(key):
        return _lagging_archive_of(file_path, key) or aws_upload(file_path, prefix)

    # The claim can be a key that differs only by letter case. The key
    # columns fold case, and S3 does not (#612). Then the normal key has
    # no object, and the record holds the bytes under its own spelling.

    head = aws_clients._head_or_none(aws_clients.aws_s3_client(with_retries=True), key)
    if head is None:
        return _lagging_archive_of(file_path, key) or aws_upload(file_path, prefix)
    if not config["FORCE_UPLOAD"] and _holds_bytes_of(head, file_path, {}):
        current_app.logger.info(
            f"'{file_path}' is the same as '{key}', no need to re-upload"
        )
        return key, head.get("LastModified"), head.get("ContentLength")

    target = other_key(key)
    current_app.logger.warning(
        f"'{file_path}' A record claims '{key}' for different bytes. The "
        f"upload goes to '{target}', and the claimed archive stays"
    )
    return aws_upload(
        file_path,
        os.path.dirname(target),
        key_name=os.path.basename(target),
        force_upload=True,
    )


def _holds_bytes_of(head, file_path, etags):
    """Return True if the object of a HEAD holds the bytes of a local file.

    The sizes must agree, and then the ETags. etags is a dict that keeps
    the ETag of the file. Thus, a second object costs no second read."""

    if head is None or head.get("ContentLength") != os.path.getsize(file_path):
        return False
    if "etag" not in etags:
        etags["etag"] = aws_clients.calculate_etag(file_path)
    return etags["etag"] == (head.get("ETag") or "").replace('"', "")


def _lagging_archive_of(file_path, key):
    """Return the archive of these bytes under an older untouched/ key, or None (#678).

    A key moves to the key of its name only in a converge run that a
    person starts (#668). Thus, a renamed row keeps its old key. A
    restore puts its rip in the import directory under the new name.
    The import then takes the old key of that row. It uploads nothing,
    and it replaces nothing.

    Only an equal size and an equal ETag prove the bytes. A missing
    object, other bytes, or a pending move gives None. The return value
    is (key, date_uploaded, filesize_bytes)."""

    if current_app.config["FORCE_UPLOAD"]:
        return None
    prefix = current_app.config["AWS_UNTOUCHED_PREFIX"]
    # The key columns fold case. Thus, the exact compare with key is in
    # Python. A key that differs only by case is an older key too (#612).

    rows = File.query.filter(
        File.untouched_basename == os.path.basename(file_path),
        File.aws_date_uploaded.isnot(None),
        File.aws_key_target.is_(None),
    ).all()
    candidates = sorted(
        {
            row.aws_key
            for row in rows
            if row.aws_key != key and archive_keys._in_prefix(row.aws_key, prefix)
        }
    )
    etags = {}
    for candidate in candidates:
        head = aws_clients._head_or_none(
            aws_clients.aws_s3_client(with_retries=True), candidate
        )
        if _holds_bytes_of(head, file_path, etags):
            current_app.logger.info(
                f"'{file_path}' is the same as '{candidate}', the archive of a "
                f"record with this untouched name. The import keeps that key"
            )
            return candidate, head.get("LastModified"), head.get("ContentLength")
    return None


def ensure_pending_upload(file, source_path):
    """Make sure that the run-scoped upload of an import exists (#494).

    Return True if the source can go. The object can be gone after a
    long wait (the 60-day expiry, or a prune). Then the source goes up
    again to the same key. Return False if the head fails, or if the
    upload fails. Then the source is the only copy of the rip."""

    key = file.aws_key
    try:
        if (
            aws_clients._head_or_none(aws_clients.aws_s3_client(with_retries=True), key)
            is not None
        ):
            return True
    except Exception as e:
        current_app.logger.warning(
            f"'{key}' Cannot read the head of the pending upload ({e}). "
            f"Keeping the source"
        )
        return False
    current_app.logger.warning(
        f"'{key}' The pending upload is gone. Uploading '{source_path}' again"
    )
    try:
        (
            file.aws_key,
            file.aws_date_uploaded,
            file.aws_filesize_bytes,
        ) = aws_upload(
            source_path,
            os.path.dirname(key),
            key_name=os.path.basename(key),
            force_upload=True,
        )
    except Exception as e:
        current_app.logger.error(
            f"'{key}' The pending upload is gone, and the new upload of "
            f"'{source_path}' failed ({e}). Keeping the source"
        )
        return False
    return True


def discard_pending_archive(key):
    """Delete each version of a run-scoped import upload. Ignore a key that is not one.

    A failed import calls this. The upload never became the archive of a
    record. Thus, no version of it must stay."""

    if not key or not archive_keys.is_pending_archive(key):
        return
    s3_client = aws_clients.aws_s3_client(with_retries=True)
    bucket = current_app.config["AWS_BUCKET"]
    if not s3_layout.delete_every_version(s3_client, bucket, key):
        s3_client.delete_object(Bucket=bucket, Key=key)
    current_app.logger.info(f"'{key}' The upload of a failed import is deleted")


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
        f"The S3 archive for '{file.basename}' is now stale ({reason}). "
        f"The weekly S3 sync uploads it again"
    )
    return True
