"""The checks before and after a task rewrites a library file."""

import os
import traceback
from flask import current_app
from app.tasks import requeue
from app import db
from app.models import File
from app.settings import get_setting


def replaced_from_outside(filesize_bytes, stale, file_path):
    """Return True if the library file at file_path is not the file of its record.

    Each writer of Fitzflix stores the new size on the record. When
    Fitzflix archives the library files, the writer sets the stale flag
    before its write (confirm_rewrite), and the upload after its commit
    clears the flag. A record that takes the upload of a failed import
    stores the size of the new file when it clears the flag
    (_adopt_upload). Thus, a file with an other size than its record
    and with no stale flag came from Radarr, from Sonarr or from a
    person. Its import did not run. Only an import makes the record of
    such a file: new tracks, a new size and a new archive.

    One file of Fitzflix reads as a file from outside: the file of an
    edit that died after its write, when a later upload cleared the
    flag. The upload does not store the size. The import of that file
    loses nothing: its archive in modified/ has the same bytes, and the
    import stores the record again.

    The rule takes a stale record to hold a file of Fitzflix. An edit
    that stops because Radarr or Sonarr changed the file
    (LibraryFileChanged) puts the flag back to its value before the
    edit. A stale record can still hold a file from outside in 4
    cases. It was stale before that change, for example after an edit
    that died. The put-back failed, because it is best effort. An edit
    started on a file that an other program put there before, and its
    import did not run yet. The file changed between the track read
    and confirm_rewrite of an in-place mkvpropedit, and the edit then
    failed with an other error. Then a scan stores the size of that
    file, and its import skips it (already_cataloged).

    A record with no size makes no claim. When Fitzflix does not
    archive, no flag is set, and an edit that died after its write looks
    the same as an outside file. Its import then reads the file of the
    edit and stores the same rows."""

    if filesize_bytes is None or stale:
        return False
    return os.path.getsize(file_path) != filesize_bytes


def record_filesize(file, size_bytes):
    """Store the size of a file on its record.

    Each task that rewrites a library file must call this for its row.
    The supplement and remux paths change the size of the file on
    disk. A row with the old size reports the library incorrectly and
    does not agree with the size of the archived copy.

    This is 1 line, but it has a name. The tests of the in-place
    rewrite contract assert against this call. It is the single place
    that writes a size. The MB and GB columns that it kept in step
    were removed. Four write sites had to remember 3 fields. Three of
    them had their own copy of the arithmetic. Only 1 line of 1
    template read the 2 derived columns. Fitzflix now formats the
    sizes where it shows them.
    """

    file.filesize_bytes = size_bytes


def _fresh_file(file_id):
    """Return the File row of a task in a new transaction, after slow file work.

    The task read the row before the work. MariaDB refuses an UPDATE of a
    row that a different worker changed after that read (error 1020).
    The rollback ends the old read view. The caller must have no pending
    changes."""

    db.session.rollback()
    return db.session.get(File, file_id)


class RecordChanged(RuntimeError):
    """The record of a library file went or moved during a rewrite of the file."""


def confirm_rewrite(file_id, file_path):
    """Confirm the record and mark the archive stale before a tool writes the file.

    Each writer of a library file calls this before its first byte, or
    before it puts its result at file_path.

    Invariant: a rewrite writes only a path that the record names at
    that moment. A rewrite can take 30 minutes. In that time, an
    import of a wider episode file can delete the record, and a rename
    can move the file. The title lock of the rewrite stops neither. A
    result that went to the old path would be a file with no record.
    Thus, a record that is gone or that names an other path raises
    RecordChanged. Then the rewrite does not start, or its result is
    removed.

    The stale flag is for a failure after the write and before the
    commit. Such a failure leaves the new bytes on disk and the old row.
    Without the flag, the weekly sync never uploads the file again, and
    a restore would bring back the older bytes. The flag is set only
    when Fitzflix archives the library files. The cataloging of an
    import, a good upload of the library file and the move of an archive
    to modified/ clear it. So does a record that takes the upload of a
    failed import. An edit that stops on a file that an other program
    changed puts the flag back to its value before the edit.

    A failed read or a failed flag raises after the rollback. The
    caller read the row before its slow work. Thus, a new transaction
    makes the read and the change (MariaDB error 1020)."""

    try:
        db.session.rollback()
        named = db.session.query(File.file_path).filter_by(id=file_id).scalar()
        library = current_app.config["LIBRARY_DIR"]
        if named is None or os.path.abspath(
            os.path.join(library, named)
        ) != os.path.abspath(file_path):
            raise RecordChanged(
                f"'{file_path}' The record of the file is gone, or it names an "
                f"other path. The rewrite stops"
            )
        if get_setting("ARCHIVE_ORIGINAL_MEDIA"):
            File.query.filter_by(id=file_id).update({"aws_stale": True})
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise


def put_stale_flag_back(file_id, stale_before, name):
    """Give a record the stale flag that it had before an edit stopped on a changed file.

    Invariant: a record that the edit found with no stale flag has no
    stale flag after LibraryFileChanged. The edit did not write the file
    that is at the path now, or no file is at the path now. An other
    program or an other task of Fitzflix changed the path. The archive
    of a new file is the job of its import. With the flag, the next
    scan takes that file to be a file of Fitzflix and stores it.
    A record that was stale before the edit keeps its flag. stale_before
    is the flag that the caller read before the edit. Each caller knows
    it. This is best effort. A fault only logs."""

    if stale_before:
        return
    try:
        db.session.rollback()
        File.query.filter_by(id=file_id).update({"aws_stale": False})
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.warning(
            f"{name} Cannot remove the stale flag after the file changed: "
            + traceback.format_exc()
        )


def _rescan_after_a_failed_edit(file_id, name, reason="Track edit failed"):
    """Queue a track scan of a file whose edit failed.

    The file can have the new layout, but the track rows and the size
    keep the old layout. No periodic scan repairs them. The scan reads
    the rows again from the file. The job id is the id of the lock-busy
    retry of the scan. Thus, a repeat replaces the scheduled scan. This
    is best effort. The caller raises the original error.

    An edit that finds a posted track UID missing from the file also
    calls this. Then the rows are older than the file."""

    try:
        requeue(
            "app.media.tracks.scan.track_metadata_scan_task",
            key=int(file_id),
            delay=(5, 15),
            reason=reason,
            kwargs={"file_id": file_id},
            description=name,
        )
    except Exception:
        current_app.logger.warning(traceback.format_exc())


# A track edit that stops on a changed file leaves a note for the file
# page. The route already said that the edit was queued. The
# next render of the file page shows the note once.

STOPPED_EDIT_KEY = "fitzflix:track-edit:stopped:{file_id}"
STOPPED_EDIT_SECONDS = 86400
STOPPED_EDIT_MESSAGE = (
    "The last track edit did not run. The file changed after the page was "
    "loaded. Fitzflix scans the file again. Reload this page after the "
    "scan, then make the edit again."
)


def note_stopped_edit(file_id):
    """Record for the file page that an edit of file_id stopped.

    This is best effort. A fault only logs."""

    try:
        current_app.redis.set(
            STOPPED_EDIT_KEY.format(file_id=int(file_id)),
            STOPPED_EDIT_MESSAGE,
            ex=STOPPED_EDIT_SECONDS,
        )
    except Exception:
        current_app.logger.warning(traceback.format_exc())


def pop_stopped_edit(file_id):
    """Return and delete the note of a stopped edit of file_id, or None."""

    try:
        note = current_app.redis.getdel(STOPPED_EDIT_KEY.format(file_id=int(file_id)))
    except Exception:
        current_app.logger.warning(traceback.format_exc())
        return None
    return note.decode() if note else None


def _stop_on_changed_file(file_id, name, missing, edit):
    """Stop an edit whose posted track UIDs are gone from the file.

    The file changed after the page was loaded. The edit writes nothing.
    A scan reads the rows again, and the file page shows a note.
    edit names the edit for the log. Return False."""

    current_app.logger.warning(
        f"{name} The file has no track with UID {', '.join(missing)}. The "
        f"file changed after the page was loaded. The {edit} stops"
    )
    _rescan_after_a_failed_edit(
        file_id, name, reason=f"Track {edit} found a changed file"
    )
    note_stopped_edit(file_id)
    return False
