"""Derived files: the transcoded copies, tracked and source-linked.

Every Handbrake output gets a DerivedFile row pointing at the library
original it came from — created by finalize_transcoding for new
transcodes, and by the adoption sweep for the untracked copies already
on the transcoded tree. Rows cascade away with their source File; the
delete sites collect the physical paths before the row delete and
enqueue the removal after the commit, exactly like the S3 deletes.

Derived rows live in their own table, never in File: File.file_path is
LIBRARY_DIR-relative and unique, and ranking/shopping/import-replace
all treat File rows as originals — see DerivedFile's docstring. The
conversion legs (4K HDR → 1080p SDR, DV Profile 7 → 8.1) stay parked
until HDR actually enters the library (census Aug 2026: zero targets);
they'll create rows here with their own kinds when they arrive.
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

    Takes the absolute output path and stores it TRANSCODES_DIR-relative;
    idempotent by file_path, so re-transcoding the same source updates
    the existing row. Session work only — the caller commits."""

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
    """The absolute paths of a file's derived copies — collect these
    BEFORE deleting the File row (the rows cascade away with it), then
    hand them to purge_derived_paths after the commit."""

    return [
        os.path.join(current_app.config["TRANSCODES_DIR"], row.file_path)
        for row in file.derived_files
    ]


def purge_derived_paths(paths):
    """Enqueue the physical removal of derived copies on the
    file-operation queue. Call AFTER the row delete commits — same
    posture as the S3 deletes, so a failed commit can't cost the
    copies of rows that rolled back. No-op for an empty list."""

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
    """Task: delete derived copies from the transcoded tree, pruning
    any directory the removal leaves empty. A missing file is success —
    the goal state is 'not there'."""

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
                # Not empty (or already gone) — both fine
                pass
        return True


def adopt_transcodes_task():
    """Task: walk TRANSCODES_DIR and adopt every untracked transcode
    whose source is identifiable — the file sits under its original's
    dirname with the original's plex_title as its stem, exactly how
    finalize_transcoding names outputs. Anything else is logged and
    left alone. Returns {adopted, already, unmatched}."""

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
