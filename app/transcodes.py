"""Track the transcoded copies as derived files linked to their source.

Every Handbrake output gets a DerivedFile row. The row points at the
library original that the output came from. finalize_transcoding creates
the row for a new transcode. The adoption sweep creates the row for an
untracked copy that is already on the transcoded tree. The rows cascade
away with their source File. The delete sites collect the physical paths
before the row delete. They enqueue the removal after the commit. This is
the same procedure as for the S3 deletes.

Derived rows live in their own table, never in File. File.file_path is
LIBRARY_DIR-relative and unique. The ranking, the shopping list, and the
import-replace all treat File rows as originals. See the docstring of
DerivedFile. The conversion legs (4K HDR to 1080p SDR, DV Profile 7 to
8.1) stay parked until HDR enters the library (census 2026-08: zero
targets). When they arrive, they will create rows here with their own
kinds.
"""

import os
import traceback

from datetime import datetime, timezone

from flask import current_app
from werkzeug.local import LocalProxy

from app import db, get_app
from app.models import DerivedFile, File

app = LocalProxy(get_app)

VIDEO_EXTENSIONS = {".avi", ".m4v", ".mkv", ".mov", ".mp4"}


def record_transcode(file, output_file, kind="handbrake"):
    """Create or refresh the DerivedFile row for one transcode output.

    This function receives the absolute output path. It stores the path
    relative to TRANSCODES_DIR. It is idempotent by file_path. Thus, a
    second transcode of the same source updates the existing row. It does
    session work only. The caller must commit the session."""

    relative = os.path.relpath(output_file, current_app.config["TRANSCODES_DIR"])
    row = DerivedFile.query.filter_by(file_path=relative).first()
    if row is None:
        row = DerivedFile(source_file_id=file.id, file_path=relative)
        db.session.add(row)
    row.source_file_id = file.id
    row.kind = kind
    row.basename = os.path.basename(relative)
    try:
        row.filesize_bytes = os.path.getsize(output_file)
    except OSError:
        row.filesize_bytes = None
    row.date_created = datetime.now(timezone.utc)
    return row


def derived_paths_for(file):
    """Return the absolute paths of the derived copies of a file.

    Collect these paths BEFORE you delete the File row. The derived rows
    cascade away with it. Then give the paths to purge_derived_paths after
    the commit."""

    return [
        os.path.join(current_app.config["TRANSCODES_DIR"], row.file_path)
        for row in file.derived_files
    ]


def purge_derived_paths(paths):
    """Enqueue the physical removal of derived copies on the file queue.

    Call this function AFTER the row delete commits. This is the same
    rule as for the S3 deletes. Thus, a failed commit cannot remove the
    copies of rows that rolled back. An empty list does nothing."""

    if not paths:
        return
    plural = "copies" if len(paths) != 1 else "copy"
    current_app.file_queue.enqueue(
        "app.transcodes.remove_derived_paths",
        args=(paths,),
        job_timeout=current_app.config["SQL_TASK_TIMEOUT"],
        description=f"Removing {len(paths)} transcoded {plural}",
    )


def remove_derived_paths(paths):
    """Delete derived copies from the transcoded tree (a queue task).

    The task also removes each directory that the removal leaves empty.
    A missing file counts as success. The goal state is 'not there'."""

    with app.app_context():
        for path in paths:
            try:
                os.remove(path)
                current_app.logger.info(f"Removed derived copy '{path}'")
            except FileNotFoundError:
                pass
            except OSError:
                current_app.logger.warning(traceback.format_exc())
                continue
            try:
                os.rmdir(os.path.dirname(path))
            except OSError:
                # The directory is not empty, or it is already gone. Both are fine.
                pass
        return True


def adopt_transcodes_task():
    """Walk TRANSCODES_DIR and adopt every untracked transcode (a queue task).

    The task adopts a transcode only when it can identify the source. The
    file must be under the dirname of its original. Its stem must be the
    plex_title of the original. This is how finalize_transcoding names
    the outputs. The task logs all other files and leaves them alone.
    It returns {adopted, already, unmatched}."""

    with app.app_context():
        root = current_app.config["TRANSCODES_DIR"]
        adopted = already = unmatched = 0
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith((".", "@"))]
            for name in sorted(filenames):
                if name.startswith("."):
                    continue
                stem, ext = os.path.splitext(name)
                if ext.lower() not in VIDEO_EXTENSIONS:
                    continue
                absolute = os.path.join(dirpath, name)
                relative = os.path.relpath(absolute, root)
                if DerivedFile.query.filter_by(file_path=relative).first():
                    already += 1
                    continue
                source = File.query.filter_by(
                    dirname=os.path.dirname(relative), plex_title=stem
                ).first()
                if source is None:
                    unmatched += 1
                    current_app.logger.info(
                        f"No source record for transcode '{relative}'"
                    )
                    continue
                record_transcode(source, absolute)
                adopted += 1
        db.session.commit()
        current_app.logger.info(
            f"Transcode adoption: {adopted} adopted, {already} already "
            f"tracked, {unmatched} unmatched"
        )
        return {"adopted": adopted, "already": already, "unmatched": unmatched}
