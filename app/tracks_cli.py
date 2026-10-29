"""Hold the `flask tracks` commands: the backfill of the Matroska track UIDs (#611).

app/cli.py calls register_cli. The commands import app.tracks when they
run. Thus, the CLI registration imports no task module. The body of the
command is a module function, and register_cli only attaches the
decorators. Thus, a test can call the body with no CliRunner.
"""

import os
import time

import click

# The files of 1 commit. The backfill holds the title lock of each file
# of a batch until the commit. Thus, a small batch keeps the locks short.

BATCH_SIZE = 25

# The time to live of a title lock of the backfill, in milliseconds. A
# batch reads only the headers of its files. Thus, 5 minutes is plenty.

LOCK_TTL_MS = 5 * 60 * 1000

# The mkvmerge -J track type of each track row model.

TRACK_TYPES = {"FileAudioTrack": "audio", "FileSubtitleTrack": "subtitles"}


def _pending_file_ids():
    """Return the ids of the Matroska files that have a track row with no UID."""

    from app import db
    from app.models import File, FileAudioTrack, FileSubtitleTrack

    pending = db.or_(
        File.id.in_(
            db.select(FileAudioTrack.file_id).where(FileAudioTrack.uid.is_(None))
        ),
        File.id.in_(
            db.select(FileSubtitleTrack.file_id).where(FileSubtitleTrack.uid.is_(None))
        ),
    )
    return list(
        db.session.execute(
            db.select(File.id)
            .where(File.container == "Matroska", pending)
            .order_by(File.id)
        ).scalars()
    )


# The mkvmerge codec ids of each MediaInfo format that a track row
# stores. A codec id matches if it is one of these or starts with one
# and a slash. These are the formats of the live rows on 2026-10-29. A
# format that is not here never matches. Then the file needs a rescan.

CODEC_IDS = {
    "AAC": ("A_AAC",),
    "AC-3": ("A_AC3",),
    "DTS": ("A_DTS",),
    "DTS LBR": ("A_DTS",),
    "E-AC-3": ("A_EAC3",),
    "FLAC": ("A_FLAC",),
    "MLP FBA": ("A_TRUEHD", "A_MLP"),
    "MPEG Audio": ("A_MPEG",),
    "Opus": ("A_OPUS",),
    "PCM": ("A_PCM", "A_MS/ACM"),
    "Vorbis": ("A_VORBIS",),
    "ASS": ("S_TEXT/ASS",),
    "D_WEBVTT/SUBTITLES": ("D_WEBVTT",),
    "DVB Subtitle": ("S_DVBSUB",),
    "PGS": ("S_HDMV/PGS",),
    "TEXTST": ("S_HDMV/TEXTST",),
    "UTF-8": ("S_TEXT/UTF8",),
    "VobSub": ("S_VOBSUB",),
}


def same_codec(row_format, codec_id):
    """Return True if a MediaInfo format and an mkvmerge codec id name 1 codec."""

    codec_id = str(codec_id or "")
    return any(
        codec_id == known or codec_id.startswith(known + "/")
        for known in CODEC_IDS.get(row_format, ())
    )


def _row_uid(row, track, kind):
    """Return the UID of track for row, or None if track is not the track of row.

    The track must have the type of the row, a UID, the language of the
    row and the codec of the row. A row that is older than the file can
    have the stream order of a different track. Then the language or the
    codec is different."""

    from app.tracks import same_language

    if track is None or track.get("type") != kind:
        return None
    properties = track.get("properties") or {}
    if properties.get("uid") is None:
        return None
    if not same_language(row.language, properties.get("language") or "und"):
        return None
    if not same_codec(row.format, properties.get("codec_id")):
        return None
    return str(properties["uid"])


def _fill_file(file, tracks):
    """Stage the UID of each track row of file that has none.

    Return the count of the filled rows, or None if the file needs a
    rescan. tracks is the result of mkvmerge_identify for the file. The
    stream order of a row is the mkvmerge track id. Each row must match
    the type, the language and the codec of that track, and no 2 rows
    can get 1 UID. On a mismatch, every row of the file stays empty. A
    rescan then fills them from the file. The UPDATE matches the row id,
    the stream order and an empty UID. Thus, a row that changed since
    the read keeps what it has."""

    from app import db
    from app.models import FileAudioTrack, FileSubtitleTrack

    by_id = {track.get("id"): track for track in tracks}
    staged = []
    for model in (FileAudioTrack, FileSubtitleTrack):
        kind = TRACK_TYPES[model.__name__]
        rows = model.query.filter(model.file_id == file.id, model.uid.is_(None)).all()
        for row in rows:
            uid = _row_uid(row, by_id.get(row.streamorder), kind)
            if uid is None:
                return None
            staged.append((model, row, uid))
    if len({uid for _, _, uid in staged}) != len(staged):
        return None

    filled = 0
    for model, row, uid in staged:
        result = db.session.execute(
            db.update(model)
            .where(
                model.id == row.id,
                model.streamorder == row.streamorder,
                model.uid.is_(None),
            )
            .values(uid=uid)
            .execution_options(synchronize_session=False)
        )
        filled += result.rowcount
    return filled


def backfill_track_uids(batch_size=BATCH_SIZE):
    """Fill the Matroska track UID of each track row that has none (#611).

    The track edits address a track by UID. The rows that were scanned
    before the UID column existed have none. This reads the headers of
    each such file with mkvmerge -J. Each file costs well under 1 second.
    The command writes only the empty UID columns. Thus, it is safe to
    run again, and a second run finds nothing to do.

    The backfill takes the title lock of each file, as the scan does.
    Then no edit or scan rewrites the file between the read and the
    commit. A file whose lock is busy, or that is not on disk, is
    skipped. The next run takes it."""

    from flask import current_app

    from app import db
    from app.models import File
    from app.tracks import mkvmerge_identify

    file_ids = _pending_file_ids()
    click.echo(f"{len(file_ids)} Matroska file(s) have track rows with no UID")
    counts = {"files": 0, "rows": 0, "left": 0, "missing": 0, "busy": 0, "failed": 0}
    started = time.monotonic()

    for offset in range(0, len(file_ids), batch_size):
        locks = []
        try:
            for file_id in file_ids[offset : offset + batch_size]:
                file = db.session.get(File, file_id)
                if file is None:
                    continue
                path = os.path.join(current_app.config["LIBRARY_DIR"], file.file_path)
                if not os.path.isfile(path):
                    counts["missing"] += 1
                    continue
                lock = current_app.lock_manager.lock(
                    file.file_identifier(), LOCK_TTL_MS
                )
                if not lock:
                    counts["busy"] += 1
                    continue
                locks.append(lock)
                try:
                    tracks = mkvmerge_identify(path)
                except Exception as error:
                    counts["failed"] += 1
                    current_app.logger.warning(
                        f"'{file.basename}' mkvmerge -J failed: {error}"
                    )
                    continue
                filled = _fill_file(file, tracks)
                if filled is None:
                    counts["left"] += 1
                    current_app.logger.warning(
                        f"'{file.basename}' The track rows do not match the "
                        f"file. The rows keep no UID until a rescan"
                    )
                    continue
                counts["rows"] += filled
                counts["files"] += 1
            db.session.commit()
        except BaseException:
            db.session.rollback()
            raise
        finally:
            for lock in locks:
                current_app.lock_manager.unlock(lock)

        done = min(offset + batch_size, len(file_ids))
        click.echo(
            f"{done} of {len(file_ids)} file(s) read, {counts['rows']} row(s) "
            f"filled, {time.monotonic() - started:.0f} s"
        )

    click.echo(
        f"Filled {counts['rows']} track row(s) in {counts['files']} file(s). "
        f"{counts['left']} file(s) have rows that do not match the file and "
        f"need a rescan. "
        f"Skipped {counts['missing']} missing, {counts['busy']} busy and "
        f"{counts['failed']} unreadable file(s)"
    )
    return counts


def register_cli(app):
    """Attach the `flask tracks` group to the Flask app."""

    @app.cli.group()
    def tracks():
        """Manage the track rows of the library files."""
        pass

    @tracks.command("backfill-uids")
    @click.option(
        "--batch-size",
        default=BATCH_SIZE,
        show_default=True,
        type=click.IntRange(min=1),
        help="The files of 1 commit.",
    )
    def backfill_uids(batch_size):
        """Fill the Matroska track UIDs of the track rows that have none (#611)."""

        backfill_track_uids(batch_size)
